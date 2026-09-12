"""Bounded evidence-backed partial answers for the current task deadline."""
from copy import deepcopy
import json

from .answer_contract import contract as answer_contract
from .protocol import fingerprint


def remember(task, spec):
    from .tasks import evidence_answer
    contract = answer_contract(task)
    schema = contract.get('schema') or {}
    required = schema.get('required', [])
    if not isinstance(required, list) or not all(isinstance(k, str) for k in required):return
    fields = set(contract['required_fields']) | set(required)
    if not fields or spec.get('format') != 'json':
        return
    # Cross-schema branches and nested partial aggregates require a richer
    # field dependency model. Do not infer their independence here.
    if any(k in schema for k in ('allOf','anyOf','oneOf','dependentSchemas','dependencies')):
        return
    try:
        candidate = evidence_answer(task, {**spec, 'partial': True})
        value = json.loads(candidate['text'])
    except (ValueError, TypeError, KeyError):
        return
    if (not isinstance(value, dict) or not value or not (fields & value.keys())
            or any(isinstance(v, (dict, list)) for v in value.values())):
        return
    dependencies = schema.get('dependentRequired', {})
    if not isinstance(dependencies, dict):return
    for field, required in dependencies.items():
        if field in value and (not isinstance(required, list) or not all(isinstance(k, str) for k in required)
                               or not set(required) <= value.keys()):
            return
    # Only retain incomplete candidates. A complete result stays on the normal
    # fast submission path and must never be relabelled as a partial fallback.
    try:
        evidence_answer(task, {**spec, 'partial': False})
        return
    except (ValueError, TypeError, KeyError):
        pass
    proof = {key: deepcopy(task.evidence[key]) for key in candidate['evidence_refs']}
    if len(json.dumps(proof, ensure_ascii=False).encode()) > 32768:return
    row = {'key': task.key, 'candidate': candidate, 'proof': proof,
           'contract': fingerprint(contract),
           'documents': {p: d['hash'] for p, d in task.documents.files.items()},
           'fields': sorted(fields & value.keys())}
    rows = [r for r in getattr(task, 'checkpoints', []) if r['candidate']['hash'] != candidate['hash']]
    rows.append(row)
    task.checkpoints = sorted(rows, key=lambda r: -len(r['fields']))[:4]


def checkpoint(task):
    from .tasks import evidence_answer
    for row in getattr(task, 'checkpoints', []):
        if row['key'] != task.key or row['contract'] != fingerprint(answer_contract(task)):continue
        if any(p not in task.documents.files or task.documents.files[p]['hash'] != h
               for p, h in row['documents'].items()):continue
        if row['candidate']['hash'] in {s['hash'] for s in task.submitted}:continue
        shadow = deepcopy(task)
        good = True
        for key, record in row['proof'].items():
            if key in task.evidence and fingerprint(task.evidence[key]) != fingerprint(record):
                good = False;break
            shadow.evidence[key] = record
        if not good:continue
        try:
            answer = evidence_answer(shadow, row['candidate']['spec'])
        except (ValueError, KeyError, TypeError):
            continue
        if answer['hash'] == row['candidate']['hash']:
            for key, record in row['proof'].items():
                task.evidence.setdefault(key, deepcopy(record))
            return answer
    return None


