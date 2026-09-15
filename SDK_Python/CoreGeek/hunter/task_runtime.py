"""Source for diagnostics/preflight INSIDE the competition task subprocess."""


def prelude(root):
    return '_hunter_task_root = ' + repr(root) + '\n' + SOURCE


SOURCE = r'''
import atexit as _ha, json as _hj, os as _ho, shlex as _hs, shutil as _hh
import subprocess as _hsub, sys as _hsys, re as _hre
_hunter_events = []
_hunter_json_pending = False
_hunter_json_pages = 0
_hunter_temporal = {}
_hunter_rows_observed = 0
_hunter_json_dataset = None
_hunter_page_sets = {}
def _hunter_sample(value, depth=0):
    if depth > 3:return '<nested value omitted>'
    if isinstance(value,dict):
        return {str(k)[:64]:('<redacted>' if _hre.search('token|key|password|secret|authorization',str(k),_hre.I)
                else _hunter_sample(v,depth+1)) for k,v in list(value.items())[:24]}
    if isinstance(value,list):return [_hunter_sample(v,depth+1) for v in value[:2]]
    if isinstance(value,str):return value[:160]
    return value
def _hunter_event(**record):
    if len(_hunter_events) < 4:
        _hunter_events.append(record)
    elif record.get('error') or isinstance(record.get('status'), int) and record['status'] >= 400:
        _hunter_events[-1] = record  # A late failed page must survive the log cap.
def _hunter_report():
    audit=globals().get('HUNTER_STATISTICS_AUDIT')
    if isinstance(audit,dict):
        allowed=('field','definition','records_count','pages_complete','raw_time','sort_key','selected_name','output_value')
        record=dict(kind='statistics_audit',program_report=True,
            values={k:_hunter_sample(audit[k]) for k in allowed if k in audit})
        if len(_hunter_events)<4:_hunter_events.append(record)
        else:
            # Keep interface errors and the complete paging observation.
            replace=next((i for i,e in enumerate(_hunter_events) if e.get('kind')=='http' and e.get('status')==200),None)
            if replace is not None:_hunter_events[replace]=record
    if _hunter_events:
        print('\nHUNTER_RUNTIME:' + _hj.dumps(_hunter_events, ensure_ascii=True), file=_hsys.stderr, flush=True)
_ha.register(_hunter_report)
_hunter_json_loads = _hj.loads
def _hunter_shape(value,depth=0):
    if depth>=2:return type(value).__name__
    if isinstance(value,dict):return {str(k)[:48]:_hunter_shape(v,depth+1) for k,v in list(value.items())[:6]}
    if isinstance(value,list):return {'type':'list','length':len(value),'item':_hunter_shape(value[0],depth+1) if value else None}
    return type(value).__name__
def _hunter_loads(*args,**kwargs):
    global _hunter_json_pending, _hunter_json_pages, _hunter_rows_observed
    value=_hunter_json_loads(*args,**kwargs)
    if _hunter_json_pending:
        _hunter_json_pending=False
        record=dict(kind='json_shape',shape=_hunter_shape(value))
        _hunter_json_pages += 1
        record['responses_observed'] = _hunter_json_pages
        body = value.get('data',value) if isinstance(value,dict) else value
        rows = body.get('records') if isinstance(body,dict) else body
        if isinstance(rows,list):
            _hunter_rows_observed += len(rows)
            record['records_observed'] = _hunter_rows_observed
            for row in rows:
                if not isinstance(row,dict):continue
                for key,val in list(row.items())[:32]:
                    if not _hre.search(r'era|dynasty|period|year|date|年代|朝代|年份',str(key),_hre.I):continue
                    if _hre.search(r'token|key|password|secret|authorization',str(key),_hre.I):continue
                    if type(val) not in (str,int,float) or len(str(val))>80:continue
                    key=str(key)[:64]
                    if key not in _hunter_temporal and len(_hunter_temporal)>=6:continue
                    counts=_hunter_temporal.setdefault(key,{})
                    encoded=_hj.dumps(val,ensure_ascii=False)
                    if encoded in counts or len(counts)<24:counts[encoded]=counts.get(encoded,0)+1
            if _hunter_temporal:
                record['temporal_value_counts']={key:[{'value':_hunter_json_loads(val),'count':count}
                    for val,count in counts.items()] for key,counts in _hunter_temporal.items()}
                record['records_observed']=_hunter_rows_observed
                record['temporal_values_partial']=True  # Observed pages, capped values; not a proof of complete paging.
            record.update(records_on_page=len(rows),record_samples=_hunter_sample(rows),
                          samples_partial=True)
            if rows and isinstance(rows[0],dict):
                record['record_fields']={str(k)[:64]:type(v).__name__ for k,v in list(rows[0].items())[:32]}
        if isinstance(body,dict) and isinstance(body.get('pagination'),dict):
            record['pagination']=_hunter_sample(body['pagination'])
            paging = body['pagination']
            total, offset = paging.get('total_count'), paging.get('offset')
            if (isinstance(rows,list) and type(total) is int and total>=0
                    and type(offset) is int and offset>=0 and _hunter_json_dataset is not None):
                key = (_hunter_json_dataset,total)
                if key in _hunter_page_sets or len(_hunter_page_sets)<8:
                    intervals = _hunter_page_sets.setdefault(key,[])
                    if len(intervals)<64:
                        intervals.append((offset,offset+len(rows)))
                    merged=[]
                    for start,end in sorted(intervals):
                        if merged and start<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],end)
                        else:merged.append([start,end])
                    covered=sum(max(0,min(end,total)-min(start,total)) for start,end in merged)
                    record['pagination_coverage']={'total_count':total,'covered_records':covered,
                        'complete': covered==total,'ranges':merged[:8],'ranges_partial':len(merged)>8}
        prior=next((i for i,e in enumerate(_hunter_events) if e.get('kind')=='json_shape'),None)
        if prior is not None:_hunter_events[prior]=record
        else:_hunter_event(**record)
    return value
_hj.loads=_hunter_loads
_hunter_excepthook=_hsys.excepthook
def _hunter_exception(kind,value,tb):
    info={'kind':'exception','error':kind.__name__}
    if isinstance(value,AttributeError):
        info.update(attribute=str(getattr(value,'name',''))[:48],object_type=type(getattr(value,'obj',None)).__name__)
    _hunter_event(**info)
    _hunter_excepthook(kind,value,tb)
_hsys.excepthook=_hunter_exception
import urllib.request as _hr, urllib.parse as _hp, urllib.error as _he
_hunter_http_open = _hr.OpenerDirector.open
def _hunter_http_detail(url, error=None):
    """Observe the request passed to urllib; never expose header values."""
    address=url.full_url if isinstance(url,_hr.Request) else url
    headers={k.lower():v for k,v in url.header_items()} if isinstance(url,_hr.Request) else {}
    names=[k for k in headers if _hre.search('authorization|api.?key|token',k)][:4]
    scheme=headers.get('authorization','').split(' ',1)[0].lower()
    parsed=_hp.urlsplit(address)
    info={'path':parsed.path[:96],'origin':parsed.scheme+'://'+str(parsed.hostname)+(':'+str(parsed.port) if parsed.port else ''),'auth_headers':names,
          'authorization_scheme':scheme if scheme in ('bearer','basic') else 'other' if scheme else 'absent'}
    if error is not None:
        # peek does not consume the HTTPError body the generated program may read.
        # Unsupported streams simply have no body hint; never replace error.fp.
        try:
            hint=error.fp.peek(384)[:384].decode('utf-8','replace')
            for name in names:
                value=headers[name]
                for secret in (value,value.split(' ',1)[-1]):
                    if secret:hint=hint.replace(secret,'<redacted>')
            hint=_hre.sub(r'(?i)((?:Bearer|Basic)\s+)(?![<{])\S+',r'\1<redacted>',hint)
            hint=_hre.sub(r"""(?i)((?:api[_-]?key|token|password|secret)["']*\s*[=:]\s*)\S+""",r'\1<redacted>',hint)
            info['error_hint']=' '.join(hint.split())[:160]
        except (AttributeError,OSError,ValueError,TypeError):pass
    return info
def _hunter_observe_http(self, url, data=None, timeout=5):
    global _hunter_json_pending, _hunter_json_dataset
    address = url.full_url if isinstance(url, _hr.Request) else url
    local = isinstance(address, str) and _hp.urlsplit(address).hostname in ('localhost', '127.0.0.1')
    try:
        response = _hunter_http_open(self, url, data, timeout)
        if local:
            _hunter_event(kind='http',status=response.status,**_hunter_http_detail(url))
            _hunter_json_pending=200<=response.status<300
            parsed=_hp.urlsplit(address)
            _hunter_json_dataset=(parsed.scheme,parsed.netloc,parsed.path,
                tuple(sorted((k,v) for k,v in _hp.parse_qsl(parsed.query) if k not in ('offset','limit'))))
        return response
    except _he.HTTPError as exc:
        if local:_hunter_event(kind='http',status=exc.code,**_hunter_http_detail(url,exc))
        raise
    except _he.URLError as exc:
        if local:_hunter_event(kind='http',error=type(exc).__name__)
        raise
_hr.OpenerDirector.open = _hunter_observe_http
try:
    import requests as _hrequests
except ImportError:
    pass
else:
    _hunter_requests_send = _hrequests.Session.send
    def _hunter_observe_requests(self, req, **kwargs):
        global _hunter_json_pending, _hunter_json_dataset
        local = _hp.urlsplit(req.url).hostname in ('localhost','127.0.0.1')
        if local:
            kwargs['proxies'] = {}
        try:
            response = _hunter_requests_send(self, req, **kwargs)
            if local:
                view = _hr.Request(req.url, headers=dict(req.headers))
                detail = _hunter_http_detail(view)
                # Do not force a streamed response body to be downloaded.
                if response.status_code >= 400 and not kwargs.get('stream'):
                    import io as _hio
                    error = type('_HunterError',(),{})()
                    error.fp = _hio.BufferedReader(_hio.BytesIO(response.content[:384]))
                    detail = _hunter_http_detail(view,error)
                _hunter_event(kind='http',status=response.status_code,**detail)
                _hunter_json_pending=200<=response.status_code<300
                parsed=_hp.urlsplit(req.url)
                _hunter_json_dataset=(parsed.scheme,parsed.netloc,parsed.path,
                    tuple(sorted((k,v) for k,v in _hp.parse_qsl(parsed.query) if k not in ('offset','limit'))))
            return response
        except _hrequests.RequestException as exc:
            if local:_hunter_event(kind='http',error=type(exc).__name__)
            raise
    _hrequests.Session.send = _hunter_observe_requests
_hunter_popen = _hsub.Popen
class _HunterPopen(_hunter_popen):
    def __init__(self, args, *positional, **kwargs):
        # Only argv calls with keyword options can be inspected unambiguously.
        # Never reinterpret a shell string, opaque executable override or flags.
        if not positional and not kwargs.get('shell') and not kwargs.get('executable') and isinstance(args, (list, tuple)) and args:
            executable = args[0]
            cwd = _ho.path.abspath(kwargs.get('cwd') or _ho.getcwd())
            if isinstance(executable, str) and '/' in executable:
                path = _ho.path.abspath(_ho.path.join(cwd, executable))
                real = _ho.path.realpath(path)
                root = _ho.path.realpath(_hunter_task_root)
                if _ho.path.commonpath([root, real]) == root:
                    info = dict(kind='launch', cwd=_ho.path.relpath(cwd, root),
                                path=_ho.path.relpath(path, root), exists=_ho.path.isfile(path),
                                symlink=_ho.path.islink(path), executable=_ho.access(path, _ho.X_OK))
                    if info['exists']:
                        try:
                            with open(path, 'rb') as source:
                                line = source.readline(512)
                            if line.startswith(b'#!') and len(line) < 512:
                                parts = _hs.split(line[2:].decode('utf-8').strip())
                                interpreter = parts[0] if parts else ''
                                info.update(interpreter=interpreter[:100], crlf=line.endswith(b'\r\n'))
                                available = _ho.path.isfile(interpreter) and _ho.access(interpreter, _ho.X_OK)
                                info['interpreter_exists'] = available
                                # Preserve the script bytes. Use only its declared
                                # interpreter; don't substitute sh for bash or
                                # Python 3 for an unknown Python version.
                                replacement = interpreter if available else None
                                if not replacement and _ho.path.basename(interpreter) == 'python3':
                                    replacement = _hsys.executable
                                if not replacement and _ho.path.basename(interpreter) in ('bash', 'sh'):
                                    replacement = _hh.which(_ho.path.basename(interpreter))
                                if replacement and (not available or info['crlf'] or not info['executable']):
                                    args = [replacement, *parts[1:], path, *args[1:]]
                                    info['repair'] = 'declared_interpreter'
                        except (OSError, UnicodeError, ValueError) as exc:
                            info['inspection_error'] = type(exc).__name__
                    self._hunter_local_program = info.get("path")
                    _hunter_event(**info)

        # Recognize a direct script argument to an observed standard
        # interpreter. Do not parse shell strings, -m/-c forms or argv flags.
        if (not getattr(self,'_hunter_local_program',None) and not positional
                and not kwargs.get('shell') and not kwargs.get('executable')
                and isinstance(args,(list,tuple)) and len(args)>=2
                and isinstance(args[0],str) and isinstance(args[1],str)
                and not args[1].startswith('-')):
            executable=_hh.which(args[0])
            known={_ho.path.realpath(p) for p in [_hsys.executable,*[_hh.which(n) for n in ('python3','python','bash','sh')]] if p}
            cwd=_ho.path.abspath(kwargs.get('cwd') or _ho.getcwd())
            script_path=_ho.path.abspath(_ho.path.join(cwd,args[1]))
            script=_ho.path.realpath(script_path)
            root=_ho.path.realpath(_hunter_task_root)
            if (executable and _ho.path.realpath(executable) in known
                    and _ho.path.commonpath([root,script])==root and _ho.path.isfile(script)):
                self._hunter_local_program=_ho.path.relpath(script_path,root)
                _hunter_event(kind='launch',path=self._hunter_local_program,exists=True,
                              interpreter=_ho.path.basename(executable))
        super().__init__(args, *positional, **kwargs)
_hsub.Popen = _HunterPopen

_hunter_wait = _HunterPopen.wait
_hunter_original_event = _hunter_event
def _hunter_event(**record):
    if record.get('kind') != 'checker_exit':
        return _hunter_original_event(**record)
    # One bounded completion map avoids evicting one checker's proof for the
    # next checker. Keep the latest completion per path, up to sixteen paths.
    for event in _hunter_events:
        if event.get('kind')=='checker_exits':
            results=event['results'];results.pop(record['path'],None)
            results[record['path']]=record['returncode']
            if len(results)>16:
                results.pop(next(iter(results)));event['truncated']=True
            return
    record={'kind':'checker_exits','results':{record['path']:record['returncode']}}
    if len(_hunter_events)<4:
        _hunter_events.append(record);return
    for i,event in enumerate(_hunter_events):
        if not (event.get('kind')=='http' and (event.get('error') or event.get('status',0)>=400)):
            _hunter_events[i]=record;return
    # All slots are failures; omitting completion leaves checker proof unknown.
def _hunter_checked_wait(self,*args,**kwargs):
    result=_hunter_wait(self,*args,**kwargs)
    path=getattr(self,'_hunter_local_program',None)
    if path and not getattr(self,'_hunter_completion_reported',False):
        self._hunter_completion_reported=True
        _hunter_event(kind='checker_exit',path=path,returncode=result)
    return result
_HunterPopen.wait=_hunter_checked_wait

'''
