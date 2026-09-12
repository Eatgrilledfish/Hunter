"""Narrow, evidence-backed repairs to generated sandbox programs.

This module only generates source; it performs no I/O or API calls in callback.
"""
import ast
import posixpath
import re
from urllib.parse import urlsplit


def prepare(code, root, cwd, documents, diagnostics=None, feedback=()):
    tree = ast.parse(code)
    changes = []
    # The wrapper already chdir's to cwd. Resolve a repeated root-relative cwd
    # once, rather than accidentally invoking ws_1/ws_1.
    class Paths(ast.NodeTransformer):
        def visit_Call(self, node):
            self.generic_visit(node)
            if isinstance(node.func, ast.Attribute) and node.func.attr in {'run','Popen','call','check_call','check_output','chdir'}:
                values = [k.value for k in node.keywords if k.arg == 'cwd']
                if node.func.attr == 'chdir': values += node.args[:1]
                for value in values:
                    if (cwd != '.' and isinstance(value, ast.Constant) and isinstance(value.value,str)
                            and posixpath.normpath(value.value) == cwd):
                        value.value = posixpath.join(root,cwd)
                        changes.append('root_relative_cwd')
            return node
    tree = Paths().visit(tree)
    texts = '\n'.join(documents)
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node,ast.Assign) and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str):
            if any(isinstance(t,ast.Name) and re.fullmatch(r'api_?key',t.id,re.I) for t in node.targets):
                value=node.value.value
                if len(value)>=8 and value in texts:keys.add(value)
    origins=set()
    for url in re.findall(r'http://(?:localhost|127\.0\.0\.1)(?::\d+)?(?![A-Za-z0-9_.:-])(?:/[^\s`<>\"\']*)?',texts):
        parsed=urlsplit(url)
        origins.add((parsed.scheme,parsed.hostname,parsed.port or 80))
    bearer = bool(re.search(
        r'Authorization[\s`\"\'|:：=*]{0,32}f?[`\"\']?\s*Bearer\b',texts,re.I))
    documented_bearer = bearer
    observed_bearer = any(e.get('kind')=='http' and e.get('status')==401
        and isinstance(e.get('origin'),str)
        and (lambda p:(p.scheme,p.hostname,p.port or 80) in origins)(urlsplit(e['origin']))
        and re.search(r'Authorization:\s*Bearer\s',e.get('error_hint',''),re.I)
        for e in feedback if isinstance(e,dict))
    bearer = bearer or observed_bearer
    auth = len(keys)==1 and len(origins)==1 and bearer
    if diagnostics is not None:
        diagnostics.update(documents=len(documents), matched_credentials=len(keys), origins=len(origins),
                           documented_header='Authorization/Bearer' if documented_bearer else None,
                           observed_header='Authorization/Bearer' if observed_bearer else None,
                           auth=('ready' if auth else 'credential_not_uniquely_bound' if len(keys)!=1
                                 else 'origin_not_unique' if len(origins)!=1 else 'header_contract_unrecognized'))
    source=ast.unparse(tree) if changes else code
    probe = len(keys)==1 and len(origins)==1 and any(
        isinstance(n,ast.Import) and any(a.name in ('urllib.request','requests') for a in n.names)
        or isinstance(n,ast.ImportFrom) and n.module in ('urllib','urllib.request','requests') for n in ast.walk(tree))
    if auth or probe:
        # Credentials come from BOTH inspected documentation and the generated
        # literal. Limit injection to that documented local origin; never carry
        # authorization through a redirect to a different origin.
        bootstrap = '''
import urllib.request as _hu, urllib.parse as _hp, urllib.error as _he
_hunter_opener_open = _hu.OpenerDirector.open
_hunter_redirect = _hu.HTTPRedirectHandler.redirect_request
_hunter_auth_failed = set()
def _hunter_auth_challenge(method, hint):
    global _hunter_bearer_ready
    if method=='GET' and not _hunter_bearer_ready and __import__('re').search(r'Authorization:\\s*Bearer\\s',hint,__import__('re').I):
        _hunter_bearer_ready=True
        return True
    return False
def _hunter_recovered(address):
    for event in globals().get('_hunter_events',[]):
        if (event.get('kind')=='http' and event.get('status')==401 and event.get('path')==_hp.urlsplit(address).path
                and event.get('origin') and _hunter_origin(event['origin'])==_hunter_origin(address)):
            event['recovered']=True
def _hunter_origin(url):
    p = _hp.urlsplit(url)
    return (p.scheme,p.hostname,p.port or 80)
def _hunter_redirect_request(self,req,fp,code,msg,headers,newurl):
    if _hunter_origin(req.full_url) in _hunter_origins and _hunter_origin(newurl) != _hunter_origin(req.full_url):
        raise ValueError('cross-origin authenticated redirect refused')
    return _hunter_redirect(self,req,fp,code,msg,headers,newurl)
def _hunter_open(self,url,data=None,timeout=5):
    address = url.full_url if isinstance(url,_hu.Request) else url
    if isinstance(address,str) and _hunter_origin(address) in _hunter_origins:
        origin = _hunter_origin(address)
        if origin in _hunter_auth_failed:
            raise RuntimeError('HTTP 401: unchanged documented authentication already failed; inspect documentation')
        req = url if isinstance(url,_hu.Request) else _hu.Request(url,data=data)
        req.full_url = _hp.quote(req.full_url, safe=":/?&=%+;,@!$'()*[]#~")
        if _hunter_bearer_ready:
            req.remove_header('Authorization')
            req.add_header('Authorization','Bearer '+_hunter_key)
        status = None
        try:
            direct = getattr(self, '_hunter_direct', None)
            if direct is None:
                direct = _hu.build_opener(_hu.ProxyHandler({}), *[h for h in self.handlers if not isinstance(h,_hu.ProxyHandler)])
                self._hunter_direct = direct
            bounded_timeout = min(timeout, 5) if isinstance(timeout, (int,float)) else 5
            response = _hunter_opener_open(direct,req,data=data,timeout=bounded_timeout)
            status = response.status
            return response
        except _he.HTTPError as exc:
            status = exc.code
            if status==401:
                try:hint=exc.fp.peek(1024)[:1024].decode('utf-8','replace')
                except (AttributeError,OSError,ValueError):hint=''
                if _hunter_auth_challenge(req.get_method(),hint):
                    exc.close()
                    req.remove_header('Authorization')
                    req.add_header('Authorization','Bearer '+_hunter_key)
                    try:
                        response=_hunter_opener_open(direct,req,data=data,timeout=bounded_timeout)
                    except _he.HTTPError as retry_error:
                        if retry_error.code!=401:_hunter_recovered(address)
                        else:_hunter_auth_failed.add(origin)
                        raise
                    _hunter_recovered(address)
                    return response
            if status == 401:_hunter_auth_failed.add(origin)
            raise
    return _hunter_opener_open(self,url,data=data,timeout=timeout)
_hu.OpenerDirector.open = _hunter_open
_hu.HTTPRedirectHandler.redirect_request = _hunter_redirect_request
try:
    import requests as _hrequests
except ImportError:
    pass
else:
    _hunter_auth_requests_send = _hrequests.Session.send
    def _hunter_send(self, req, **kwargs):
        if _hunter_origin(req.url) in _hunter_origins:
            if _hunter_bearer_ready:req.headers['Authorization'] = 'Bearer '+_hunter_key
            kwargs['proxies'] = {}
            kwargs['allow_redirects'] = False
            kwargs['timeout'] = 5
        response=_hunter_auth_requests_send(self, req, **kwargs)
        if (_hunter_origin(req.url) in _hunter_origins and response.status_code==401
                and not kwargs.get('stream') and _hunter_auth_challenge(req.method,response.text[:1024])):
            response.close()
            req.headers['Authorization']='Bearer '+_hunter_key
            response=_hunter_auth_requests_send(self,req,**kwargs)
            if response.status_code!=401:_hunter_recovered(req.url)
        return response
    _hrequests.Session.send = _hunter_send
'''
        source='_hunter_bearer_ready = '+repr(bool(auth))+'\n_hunter_key = '+repr(next(iter(keys)))+'\n_hunter_origins = '+repr(sorted(origins))+'\n'+bootstrap+'\nexec(compile('+repr(source)+',"<task_program>","exec"))'
        changes.append('documented_local_bearer' if auth else 'local_auth_challenge')
    return source, sorted(set(changes))
