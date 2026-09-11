"""Narrow, evidence-backed repairs to generated sandbox programs.

This module only generates source; it performs no I/O or API calls in callback.
"""
import ast
import posixpath
import re
from urllib.parse import urlsplit


def prepare(code, root, cwd, documents):
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
    auth = len(keys)==1 and len(origins)==1 and re.search(
        r'Authorization[`\"\']?\s*[:：=]?\s*f?[`\"\']?\s*Bearer\b',texts,re.I)
    source=ast.unparse(tree) if changes else code
    if auth:
        # Credentials come from BOTH inspected documentation and the generated
        # literal. Limit injection to that documented local origin; never carry
        # authorization through a redirect to a different origin.
        bootstrap = '''
import urllib.request as _hu, urllib.parse as _hp
_hunter_urlopen = _hu.urlopen
def _hunter_origin(url):
    p = _hp.urlsplit(url)
    return (p.scheme,p.hostname,p.port or 80)
class _HunterRedirect(_hu.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if _hunter_origin(newurl) != _hunter_origin(req.full_url):
            raise ValueError('cross-origin authenticated redirect refused')
        return super().redirect_request(req,fp,code,msg,headers,newurl)
def _hunter_open(url,data=None,timeout=5,**kwargs):
    address = url.full_url if isinstance(url,_hu.Request) else url
    if isinstance(address,str) and _hunter_origin(address) in _hunter_origins and not kwargs:
        req = url if isinstance(url,_hu.Request) else _hu.Request(url,data=data)
        if not req.has_header('Authorization'):
            req.add_header('Authorization','Bearer '+_hunter_key)
        return _hu.build_opener(_hu.ProxyHandler({}),_HunterRedirect()).open(req,data=data,timeout=timeout)
    return _hunter_urlopen(url,data=data,timeout=timeout,**kwargs)
_hu.urlopen = _hunter_open
'''
        source='_hunter_key = '+repr(next(iter(keys)))+'\n_hunter_origins = '+repr(sorted(origins))+'\n'+bootstrap+'\nexec(compile('+repr(source)+',"<task_program>","exec"))'
        changes.append('documented_local_bearer')
    return source, sorted(set(changes))
