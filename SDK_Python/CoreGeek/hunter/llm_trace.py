"""Bounded metadata for the actual prompt and model reply, never raw credentials."""
import ast
import hashlib
import json
import re
from urllib.parse import urlsplit
from .task_protocol import normalize as normalize_decision


def redact(text):
    """Keep protocol wording, never credential or checker TOKEN values."""
    text = str(text)
    text = re.sub(r'(?i)(\b(?:Bearer|Basic)\s+)(?![<{])[A-Za-z0-9_./+=-]+', r'\1<redacted>', text)
    text = re.sub(r'''(?ix)((?:api[_-]?key|access[_-]?token|password|secret|credential|x-api-key|token|密钥|令牌)
        [`"']*\s*(?:[:=]|为|是)\s*)[^\s,;|]+''', r'\1<redacted>', text)
    return text


def rejection_stage(reason):
    reason = str(reason).lower()
    if 'checker' in reason:return 'checker_completion'
    if 'selector' in reason or 'composition' in reason or 'extract' in reason:return 'answer_extraction'
    if 'evidence' in reason or 'documentation' in reason:return 'evidence_binding'
    if 'answer' in reason or 'task requires' in reason:return 'answer_contract'
    if 'request' in reason or 'nonce' in reason or 'version' in reason:return 'reply_identity'
    if 'json' in reason or 'expecting' in reason or 'delimiter' in reason:return 'reply_json'
    return 'command_plan'


def short_ref(value):
    # Keep the complete ordinary evidence ID; exceptional lengths are explicit.
    return str(value)[:96]


def result_shape(data):
    text = data.get('text') or ''
    tokens = re.findall(r'(?m)^\s*TOKEN:\s*(\S+)\s*$', text) if isinstance(text,str) else []
    exits = {}
    for event in data.get('runtime_events',[]):
        if not isinstance(event,dict):continue
        if event.get('kind') == 'checker_exits':exits.update(event.get('results',{}))
        elif event.get('kind') == 'checker_exit':exits[event.get('path')]=event.get('returncode')
    if data.get('operation') == 'run_tool':exits[data.get('path')]=data.get('tool_exit_code')
    value = data.get('data')
    return {'data_type':type(value).__name__ if 'data' in data else 'absent',
            'data_keys':list(value)[:6] if isinstance(value,dict) else [],
            'token_n':len(tokens), 'token_hash':digest(tokens[-1]) if tokens else None,
            'checker_exits':dict(list(exits.items())[:4])}


def decision(raw):
    if not isinstance(raw,str) or len(raw)>32768:return {}
    try:
        try:value=json.loads(raw)
        except ValueError:
            blocks=re.findall(r'```(?:json)?[ \t]*\n(.*?)\n```',raw,re.S)
            if len(blocks)!=1:return {}
            value=json.loads(blocks[0])
        return value if isinstance(value,dict) else {}
    except (ValueError,TypeError,RecursionError):return {}


def rejection_detail(data, evidence, required=()):
    """Resolve the requested references against SDK evidence, not model claims."""
    try:data=normalize_decision(data,evidence)
    except ValueError:pass
    spec=data.get('answer_candidate') or data.get('command_plan') or data
    if not isinstance(spec,dict):return {}
    result={}
    refs=spec.get('evidence_refs',[])
    if isinstance(refs,list):
        result['refs_n']=len(refs);result['refs']=[]
        for ref in refs[:2]:
            record=evidence.get(ref,{}) if isinstance(ref,str) else {}
            item=record.get('data',{})
            entry={'id':short_ref(ref),'op':item.get('operation'),'path':item.get('path'),
                   'usable':record.get('usable'),'answer_usable':record.get('answer_usable'),
                   'complete':item.get('completeness')}
            if not record:entry={'id':short_ref(ref),'missing':True}
            elif item.get('operation') in ('run_python','run_tool'):
                entry.update(result_shape(item))
            result['refs'].append(entry)
    extract=spec.get('extract')
    if isinstance(extract,dict):
        result['extract']={'evidence':short_ref(extract.get('evidence')),
                           'selector':extract.get('selector')}
    if 'value' in spec:
        value=spec['value'];result['value_type']=type(value).__name__
        result['value_keys']=list(value)[:6] if isinstance(value,dict) else []
        result['value_hash']=digest(json.dumps(value,ensure_ascii=False,sort_keys=True))
        if isinstance(value,dict) and isinstance(value.get('token'),str):
            result['value_token_hash']=digest(value['token'])
    if required:
        result['required_checkers']=list(required)[:4]
        if len(required)>4:result['required_checkers_n']=len(required)
    return result


def document_context(evidence):
    """A few wording lines explain path/auth detection; no full manuals."""
    docs=[]
    for record in evidence.values():
        data=record.get('data',{});text=data.get('text');path=str(data.get('path',''))
        if not (record.get('usable') and data.get('operation')=='read_slice' and isinstance(text,str)):continue
        lines=[]
        for number,line in enumerate(text.splitlines(),1):
            if re.search(r'Authorization|Bearer|认证|鉴权|工作目录|工作区|Work\s+in|cwd|\./check|检查器',line,re.I):
                excerpt=redact(line)
                lines.append({'line':number,'text':excerpt[:150]})
                if len(excerpt)>150:lines[-1]['cut']=True
            if len(lines)==3:break
        if lines:docs.append({'path':path[:80],'hash':digest(text),'lines':lines})
        if len(docs)==2:break
    return docs


def bounded(value, width=96, items=4, depth=0):
    if depth>8:return '<depth limit>'
    if isinstance(value,str):
        value=redact(value);encoded=value.encode('utf-8')
        return value if len(encoded)<=width else encoded[:width].decode('utf-8','ignore')+'…'
    if isinstance(value,list):return [bounded(v,width,items,depth+1) for v in value[:items]]
    if isinstance(value,dict):return {str(k)[:96]:bounded(v,width,items,depth+1) for k,v in list(value.items())[:24]}
    return value


def fit(record, budget=1200):
    record=bounded(record,240,8)
    for width,items in ((160,4),(96,3),(64,2),(40,1)):
        if len(json.dumps(record,ensure_ascii=False,separators=(',',':')).encode())<=budget:break
        record=bounded(record,width,items);record['cut']=True
    # Pathological nested selectors/keys must not turn one diagnostic into a
    # full transcript. Keep the rejection identity even when details won't fit.
    essential={'task','rid','reason','stage','verdict','kind','left','cut'}
    while len(json.dumps(record,ensure_ascii=False,separators=(',',':')).encode())>budget:
        optional=[k for k in record if k not in essential]
        if not optional:break
        key=max(optional,key=lambda k:len(json.dumps(record[k],ensure_ascii=False).encode()))
        record.pop(key);record['cut']=True
    return record


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]


def payload(prompt):
    start=prompt.find('{"request_id":')
    try:
        value=json.loads(prompt[start:]) if start>=0 else {}
        return value if isinstance(value,dict) else {}
    except (ValueError,TypeError):return {}


def sent(prompt):
    data=payload(prompt)
    docs=[];texts=[]
    for record in data.get('evidence',[]):
        item=record.get('data',{})
        if item.get('operation')!='read_slice':continue
        text=item.get('text')
        docs.append({'file':str(item.get('path',''))[-60:],
                     'complete':item.get('completeness')=='complete',
                     'in_prompt':isinstance(text,str),
                     'chars':len(text) if isinstance(text,str) else 0,
                     'hash':digest(text) if isinstance(text,str) else None})
        if isinstance(text,str):texts.append(text)
    return {'rid':data.get('request_id'), 'left':data.get('rounds_left'),
            'allowed':data.get('allowed_actions',data.get('allowed_intents')),
            'template_intent':('cmd' if 'cmd' in (data.get('reply_template') or {}) else
                               'submit' if 'submit' in (data.get('reply_template') or {}) else
                               (data.get('reply_template') or {}).get('intent')),
            'prompt_chars':len(prompt),'prompt_hash':digest(prompt),
            'docs':docs[:3],'docs_n':len(docs),'omitted':data.get('omitted_evidence'),
            '_documents':'\n'.join(texts)}


def program(code,documents):
    out={'code_hash':digest(code),'code_chars':len(code)}
    if len(code)>16384:return {**out,'syntax':'not_inspected_size_limit'}
    try:tree=ast.parse(code)
    except (SyntaxError,ValueError,RecursionError):return {**out,'syntax':'invalid'}
    calls=[];headers=[];auth=[];urls=[];imports=[]
    for node in ast.walk(tree):
        if isinstance(node,(ast.Import,ast.ImportFrom)):
            imports.extend([a.name for a in node.names] if isinstance(node,ast.Import) else [node.module or ''])
        if isinstance(node,ast.Constant) and isinstance(node.value,str) and node.value.startswith(('http://','https://')):
            try:
                parts=urlsplit(node.value)
                urls.append((parts.hostname or '')+(':'+str(parts.port) if parts.port else '')+parts.path[:60])  # No query values/userinfo.
            except ValueError:pass
        if isinstance(node,ast.Assign):
            names=[t.id for t in node.targets if isinstance(t,ast.Name)]
            for name in names:
                if re.search(r'key|token|password|secret|authorization',name,re.I):
                    literal=node.value.value if isinstance(node.value,ast.Constant) and isinstance(node.value.value,str) else None
                    auth.append({'var':name[:32],'source':'literal' if literal is not None else 'expression',
                                 'document_match':bool(literal and literal in documents) if literal is not None and documents else None})
        if isinstance(node,ast.Dict):
            for key,value in zip(node.keys,node.values):
                if (isinstance(key,ast.Constant) and isinstance(key.value,str) and re.search(r'authorization|api.?key|token',key.value,re.I)
                        and isinstance(value,ast.Constant) and isinstance(value.value,str)):
                    auth.append({'var':key.value[:32],'source':'literal_header',
                                 'document_match':bool(value.value and value.value in documents) if documents else None})
            headers.extend(k.value[:40] for k in node.keys if isinstance(k,ast.Constant) and isinstance(k.value,str)
                           and re.search(r'authorization|api.?key|token|content-type',k.value,re.I))
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute):
            if node.func.attr in ('run','Popen','call','check_output','check_call') and node.args:
                argv=node.args[0]
                if isinstance(argv,(ast.List,ast.Tuple)) and argv.elts:
                    first=argv.elts[0]
                    if isinstance(first,ast.Constant) and isinstance(first.value,str):calls.append(first.value[:60])
                elif isinstance(argv,ast.Constant) and isinstance(argv.value,str):calls.append('<shell string>')
    out.update(imports=sorted(set(imports))[:5],endpoints=sorted(set(urls))[:2],headers=sorted(set(headers))[:4],
               auth=auth[:2],launch=calls[:2])
    return out


def received(raw,documents=''):
    result={'reply_chars':len(raw),'reply_hash':digest(raw)}
    if len(raw)>32768:return {**result,'parse':'not_inspected_size_limit'}
    try:
        try:data=json.loads(raw)
        except ValueError:
            blocks=re.findall(r'```(?:json)?[ \t]*\n(.*?)\n```',raw,re.S)
            if len(blocks)!=1:raise
            data=json.loads(blocks[0]);result['fenced']=True
        if not isinstance(data,dict):return {**result,'parse':'not_object'}
    except (ValueError,TypeError,RecursionError):return {**result,'parse':'not_json'}
    if 'cmd' in data or 'submit' in data:
        result['protocol']='simple'
        try:data=normalize_decision(data,{})
        except ValueError:result['protocol']='invalid_simple'
    result.update(parse='json',rid=str(data.get('request_id',''))[:40],version=data.get('version') if type(data.get('version')) is int else None,intent=str(data.get('intent',''))[:16])
    plan=data.get('command_plan') or (data if 'operation' in data else {})
    if isinstance(plan,dict):
        result.update(op=plan.get('operation'),cwd=str(plan.get('path',''))[:80])
        if isinstance(plan.get('code'),str):result.update(program(plan['code'],documents))
    answer=data.get('answer_candidate')
    if isinstance(answer,dict):
        result['answer']={'format':str(answer.get('format',''))[:10],'refs_n':len(answer['evidence_refs']) if isinstance(answer.get('evidence_refs'),list) else None,
                          'method':'extract' if 'extract' in answer else 'compose' if 'compose' in answer else 'value',
                          'keys':[str(k)[:24] for k in list(answer['value'])[:8]] if isinstance(answer.get('value'),dict) else None}
    return {k:v for k,v in result.items() if v is not None and v!=[]}
