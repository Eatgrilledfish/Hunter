"""Inspected local file manifests; no imports or filesystem access in callback."""
import ast
import posixpath


def import_candidates(path, text, entry):
    if not path.endswith('.py'):
        return set()
    if not isinstance(text, str):
        raise ValueError('Python source must be completely decoded: '+path)
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        raise ValueError('Python source cannot be inspected: '+path) from exc
    candidates = set()
    for node in ast.walk(tree):
        modules, bases = [], {'', posixpath.dirname(entry)}
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ''
            modules = [module]+[module+'.'+alias.name if module else alias.name
                                for alias in node.names if alias.name != '*']
            if node.level:
                base = posixpath.dirname(path)
                for _ in range(node.level-1):
                    base = posixpath.dirname(base)
                bases = {base}
        for base in bases:
            for module in modules:
                parts = module.split('.') if module else []
                stem = posixpath.join(base, *parts) if parts else base
                if stem:
                    candidates.add(stem+'.py')
                    candidates.add(posixpath.join(stem, '__init__.py'))
                for end in range(1, len(parts)):
                    candidates.add(posixpath.join(base, *parts[:end], '__init__.py'))
        if len(candidates) > 128 or any(len(p) > 1024 for p in candidates):
            raise ValueError('local import candidate budget exceeded')
    return candidates


def execution_manifest(plan, evidence):
    known, inspected = {}, {}
    for record in evidence.values():
        if record.get('source') != 'sandbox' or not record.get('usable'):
            continue
        data = record['data']
        for item in data.get('entries', []):
            if isinstance(item, dict) and isinstance(item.get('path'), str):
                known[item['path']] = item.get('kind')
        if data.get('operation') == 'read_slice':
            inspected[data.get('path')] = data
    dependencies = plan.get('dependencies', [])
    if (not isinstance(dependencies, list) or len(dependencies) > 7
            or any(not isinstance(p, str) or len(p) > 1024 or '\x00' in p for p in dependencies)
            or len(set(dependencies)) != len(dependencies)):
        raise ValueError('dependencies must name at most seven distinct discovered files')
    entry = plan['path']
    pending, manifest, candidates = [entry]+dependencies, {}, set()
    while pending:
        path = pending.pop(0)
        if path in manifest:
            continue
        data = inspected.get(path)
        if (known.get(path) != 'file' or not data or data.get('completeness') != 'complete'
                or not isinstance(data.get('file_sha256'), str)):
            raise ValueError('dependency must be discovered and fully inspected: '+str(path))
        manifest[path] = data['file_sha256']
        if len(manifest) > 8:
            raise ValueError('execution manifest exceeds eight files')
        imports = import_candidates(path, data.get('text'), entry)
        candidates.update(imports)
        if len(candidates) > 128:
            raise ValueError('local import candidate budget exceeded')
        pending.extend(sorted(p for p in imports if known.get(p) == 'file' and p not in manifest))
    return dict(sorted(manifest.items())), sorted(candidates)


def input_manifest(plan, evidence, fixed):
    paths = plan.get('inputs', [])
    if (not isinstance(paths, list) or len(paths) > 7
            or any(not isinstance(p, str) or len(p) > 1024 or '\x00' in p for p in paths)
            or len(set(paths)) != len(paths) or set(paths) & set(fixed)):
        raise ValueError('inputs must be distinct data files separate from code/dependencies')
    if len(paths)+len(fixed) > 8:
        raise ValueError('code, dependencies and inputs exceed eight files')
    known, inspected = set(), {}
    for record in evidence.values():
        if record.get('source') != 'sandbox' or not record.get('usable'):
            continue
        data = record['data']
        known.update(e.get('path') for e in data.get('entries', []) if isinstance(e, dict) and e.get('kind') == 'file')
        if data.get('operation') == 'read_slice':
            inspected[data.get('path')] = data
    result = {}
    for path in paths:
        data = inspected.get(path)
        if (path not in known or not data or data.get('completeness') != 'complete'
                or not isinstance(data.get('file_sha256'), str)):
            raise ValueError('input must be discovered and fully inspected: '+path)
        result[path] = data['file_sha256']
    return dict(sorted(result.items()))
