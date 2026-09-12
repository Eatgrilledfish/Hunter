"""Current-document-bound generated programs; never a cache of task answers."""
import ast
from copy import deepcopy
from dataclasses import dataclass, field

from .protocol import fingerprint


def document(task, path):
    for key, record in reversed(list(task.evidence.items())):
        data = record.get('data', {})
        if data.get('operation') == 'read_slice' and data.get('path') == path:
            if (record.get('usable') and data.get('completeness') == 'complete'
                    and isinstance(data.get('text'), str) and data.get('file_sha256')):
                return key, data
            break
    raise ValueError('program parameter document is not completely inspected: '+path)


def argument(text, value, evidence):
    if isinstance(value, str):
        result = value
    else:
        if not isinstance(value, dict) or set(value) not in (
                {'task_prefix', 'task_suffix'}, {'document_path', 'task_prefix', 'task_suffix'}):
            raise ValueError('invalid generated-program argument binding')
        if 'document_path' in value:
            class View:
                pass
            view = View()
            view.evidence = evidence
            _, source = document(view, value['document_path'])
            text = source['text']
        prefix, suffix = value['task_prefix'], value['task_suffix']
        if not isinstance(prefix, str) or not prefix or not isinstance(suffix, str) or not suffix:
            raise ValueError('program binding needs nonempty delimiters')
        if text.count(prefix) != 1 or suffix not in text.partition(prefix)[2]:
            raise ValueError('program binding is missing or ambiguous')
        result = text.partition(prefix)[2].partition(suffix)[0]
    if not isinstance(result, str) or not result or len(result) > 1024 or '\x00' in result:
        raise ValueError('invalid generated-program argument')
    return result


def arguments(text, plan, evidence):
    values = plan.get('args', [])
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError('program argv exceeds local limits')
    return [argument(text, value, evidence) for value in values]


def resolve_path(task, path):
    if path == '@statement':
        if not task.statement_path:
            raise ValueError('current statement path unavailable')
        return task.statement_path
    return path


def materialize(task, template, paths):
    plan = deepcopy(template)
    plan['evidence_refs'] = [document(task, resolve_path(task, p))[0] for p in paths]
    for arg in plan.get('args', []):
        if isinstance(arg, dict) and 'document_path' in arg:
            arg['document_path'] = resolve_path(task, arg['document_path'])
    return plan


def family(task, plan, paths):
    texts = {p: document(task, resolve_path(task, p))[1]['text'] for p in paths}
    main = task.text.replace(task.statement_path, '@statement') if task.statement_path else task.text
    for arg in plan.get('args', []):
        if not isinstance(arg, dict):
            continue
        value = argument(task.text, arg, task.evidence)
        fragment = arg['task_prefix']+value+arg['task_suffix']
        replacement = arg['task_prefix']+'{current_argument}'+arg['task_suffix']
        if 'document_path' in arg:
            path = '@statement' if arg['document_path'] == task.statement_path else arg['document_path']
            if path not in texts:
                raise ValueError('bound document absent from program manifest')
            texts[path] = texts[path].replace(fragment, replacement, 1)
        else:
            main = main.replace(fragment, replacement, 1)
    return fingerprint({'task': main, 'documents': texts, 'python': task.environment.get('python')})


@dataclass
class ProgramRecipes:
    records: list = field(default_factory=list)

    def reject(self, identity):
        for record in self.records:
            if record['id'] == identity:
                record['validation_level'] = 'SUSPECT'

    def learn(self, task, execution, answer):
        plan = execution['plan']
        if (plan.get('operation') != 'run_python' or answer.get('partial')
                or answer.get('basis') != 'deterministic_extraction' or not execution.get('manifest')):
            return
        try:
            values = arguments(task.text, plan, task.evidence)
            # Declaring an argument must not silently retain its old value as a
            # program literal. This is a conservative reuse gate, not execution
            # sandboxing or a proof that arbitrary generated code is correct.
            bound = [v for v, a in zip(values, plan.get('args', [])) if isinstance(a, dict)]
            constants = [n.value for n in ast.walk(ast.parse(plan['code'])) if isinstance(n, ast.Constant)]
            if any(str(c) == v or isinstance(c, str) and len(v) >= 2 and v in c for c in constants for v in bound):
                return
            root = task.environment.get('root')
            if root and root in plan['code']:
                return
            paths = sorted('@statement' if p == task.statement_path else p for p in execution['manifest'])
            signature = family(task, plan, paths)
            template = deepcopy(plan)
            template.pop('evidence_refs', None)
            template.pop('answer_output', None)
            for arg in template.get('args', []):
                if isinstance(arg, dict) and arg.get('document_path') == task.statement_path:
                    arg['document_path'] = '@statement'
            # Only the selector is retained; current result IDs and values are
            # never part of the reusable answer extractor.
            output = {'format': answer['spec']['format'], 'selector': answer['spec']['extract']['selector'], 'partial': False}
            identity = fingerprint({'family': signature, 'template': template, 'paths': paths, 'output': output})
            old = next((r for r in self.records if r['id'] == identity), None)
            if old:
                old['validation_runs'] += 1
                return identity
            self.records.append({'id': identity, 'family': signature, 'template': template, 'paths': paths,
                'output': output, 'validation_level': 'EXECUTION_AND_SUBMISSION_CHECKED',
                'official_correctness': 'UNKNOWN', 'validation_runs': 1})
            self.records = self.records[-16:]
            return identity
        except (ValueError, KeyError, TypeError, SyntaxError):
            return

    def next(self, task):
        for record in reversed(self.records):
            if record['validation_level'] != 'EXECUTION_AND_SUBMISSION_CHECKED':
                continue
            try:
                plan = materialize(task, record['template'], record['paths'])
                if family(task, plan, record['paths']) != record['family']:
                    continue
                arguments(task.text, plan, task.evidence)
                plan['answer_output'] = deepcopy(record['output'])
                return plan, record['id']
            except (ValueError, TypeError, KeyError):
                continue
        return None, None
