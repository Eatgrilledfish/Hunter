"""Explicit document-local checker requirements and observed completion gates."""
import posixpath
import re


def checker_paths(task):
    paths=set()
    for evidence in task.evidence.values():
        data=evidence.get('data',{});name=data.get('path','')
        if not (evidence.get('usable') and data.get('operation')=='read_slice'
                and data.get('completeness')=='complete' and isinstance(name,str)
                and (name==task.statement_path or name.endswith('/spec.md') or name=='spec.md')):continue
        text=data.get('text') or ''
        document_dir=posixpath.dirname(name)
        # An explicit working directory in this document scopes its relative
        # commands. Do not inherit another document's directory or infer one
        # from an execution that happened to succeed.
        work_dirs={posixpath.normpath(posixpath.join(document_dir,m[1]))
                   for m in re.finditer(r'\bWork\s+in\s+[`\"\']?((?:\./)?[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*)(?=[`\"\'\s,;]|\.(?:\s|$)|$)',text,re.I)}
        root = (getattr(task, 'environment', {}) or {}).get('root')
        for match in re.finditer(r'\bcd\s+[`\"\']?([A-Za-z0-9_./-]+)(?=[`\"\'\s;]|$)', text):
            directory = posixpath.normpath(match[1])
            if directory.startswith('/'):
                if not root or not directory.startswith(root.rstrip('/') + '/'):
                    continue
                directory = posixpath.relpath(directory, root)
            else:
                directory = posixpath.normpath(posixpath.join(document_dir, directory))
            if directory != '..' and not directory.startswith('../'):
                work_dirs.add(directory)
        command_dir=next(iter(work_dirs)) if len(work_dirs)==1 else document_dir
        for m in re.finditer(r'(\bRun|\bExecute|\bchecker\s+is|运行|执行|检查器为)\s+[`\"\']?(\./[A-Za-z0-9_/-]+(?:\.py|\.sh)?)(?=[`\"\'\s.,;。]|$)',text,re.I):
            if text[m.end():m.end()+1]=='.' and text[m.end()+1:m.end()+2].isalnum():continue
            if m[1].lower() in ('run','execute','运行','执行') and not re.fullmatch(
                    r'(?:check(?:er)?|validate|verify)(?:[-_][A-Za-z0-9_-]+|[0-9]+)?(?:\.py|\.sh)?',posixpath.basename(m[2]),re.I):continue
            path=posixpath.normpath(posixpath.join(command_dir,m[2]))
            if path!='..' and not path.startswith('../') and not path.startswith('/'):paths.add(path)
    return sorted(paths)


def validate(task, refs):
    required=checker_paths(task)
    if not required:return
    # Require a single selected successful execution to carry all declared
    # checker completions. Never merge successful runs across separate repairs.
    executions=[task.evidence[key]['data'] for key in refs if task.evidence[key]['data'].get('operation') in ('run_python','run_tool')]
    for data in executions:
        results={e.get('path'):e.get('returncode') for e in data.get('runtime_events',[])
                 if isinstance(e,dict) and e.get('kind')=='checker_exit'}
        for event in data.get('runtime_events',[]):
            if isinstance(event,dict) and event.get('kind')=='checker_exits' and isinstance(event.get('results'),dict):
                results.update(event['results'])
        if data.get('operation')=='run_tool':
            results[posixpath.normpath(data.get('path',''))]=data.get('tool_exit_code')
        if data.get('status')=='ok' and all(type(results.get(p)) is int and results[p]==0 for p in required):return
    raise ValueError('declared checker has no observed successful completion: '+', '.join(required))


def token_answer(task, data):
    """Recover captured output only from this execution's declared checker."""
    from .answer_contract import contract
    required=checker_paths(task)
    schema=contract(task)
    fields=set(schema['required_fields']) | set((schema.get('schema') or {}).get('required',[]))
    texts=[task.text]+[r['data'].get('text','') or '' for r in task.evidence.values()
        if r.get('usable') and r.get('data',{}).get('operation')=='read_slice'
        and r['data'].get('completeness')=='complete'
        and (r['data'].get('path')==task.statement_path or str(r['data'].get('path','')).endswith('spec.md'))]
    if not fields and any(re.search(r'\breturn\s+JSON\s+with\s+(?:field\s+)?[`\"\']?token[`\"\']?\s*[.;\n]',t,re.I) for t in texts):
        fields={'token'}
    if not fields and any(re.search(r'\breturn\s+(?:its|the\s+checker[’\']?s)\s+token\s*[.;]',t,re.I) for t in texts):
        fields={'token'}
    if not fields:
        import json
        for text in texts:
            for match in re.finditer(r'(?:返回|提交)[^。\n]{0,60}?(\{[^{}\n]*\})',text):
                try:value=json.loads(match[1])
                except ValueError:continue
                if isinstance(value,dict) and set(value)=={'token'}:fields={'token'}
    if not required or fields!={'token'} or not schema['json_required']:
        return None
    sources=[r.get('stdout','') for r in data.get('checker_outputs',[])
             if isinstance(r,dict) and r.get('path') in required and r.get('complete') is True
             and type(r.get('returncode')) is int and r['returncode']==0]
    if data.get('operation')=='run_tool' and posixpath.normpath(data.get('path','')) in required:
        sources.append(data.get('text',''))
    # A single labelled token is the supported contract; JSON or different
    # checker formats stay with the existing model/evidence validation path.
    tokens={m[1] for text in sources if isinstance(text,str)
            for m in re.finditer(r'(?m)^\s*TOKEN:\s*([A-Za-z0-9_-]+)\s*$',text)}
    if len(tokens)!=1:return None
    return {'token':tokens.pop()}
