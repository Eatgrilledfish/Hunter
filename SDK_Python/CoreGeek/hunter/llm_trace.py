"""Bounded metadata for the actual prompt and model reply, never raw credentials."""
import ast
import hashlib
import json
import re
from urllib.parse import urlsplit


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
