"""Submission gates derived from this task's inspected statement and feedback."""
import json
import re
from .checker_contract import checker_paths, validate as validate_checkers


def explicit_fields(text):
    """Only unambiguous field lists; arbitrary prose and API examples stay unknown."""
    fields = set()
    pattern = (r'(?:return\s+JSON\s+(?:with\s+(?:fields?\s*)?|fields?\s*[:：]?\s*)'
               r'|(?:返回|提交|输出)\s*JSON\s*(?:字段|包含字段)\s*[:：]?\s*)'
               r'([^\n。.;]+)')
    key = r'[`\"\']?([A-Za-z_][A-Za-z0-9_]*)[`\"\']?'
    for match in re.finditer(pattern, text, re.I):
        parts = re.split(r'\s*(?:,|，|、|\band\b|和)\s*', match[1].strip())
        matches = [re.fullmatch(key, part.strip()) for part in parts]
        if len(parts) >= 2 and all(matches):
            fields.update(m[1] for m in matches)
    return sorted(fields)


def answer_schema(text):
    # Accept a schema only after an explicit answer-schema label, never a
    # request/response example elsewhere in the API manual.
    for match in re.finditer(r'(?:answer\s+(?:JSON\s+)?schema|答案\s*(?:JSON\s*)?Schema)\s*[:：]\s*(?:```(?:json)?\s*)?', text, re.I):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[match.end():])
            if isinstance(value, dict) and value.get('type') == 'object':
                return value
        except (ValueError, RecursionError):
            continue
    return None


def validate_schema(value, schema, partial=False, path='$', depth=0):
    """Known structural subset only; no invented pagination/checker semantics."""
    if not isinstance(schema, dict) or depth > 16:
        return
    checks = {'object': lambda v: isinstance(v, dict), 'array': lambda v: isinstance(v, list),
              'string': lambda v: isinstance(v, str), 'integer': lambda v: type(v) is int,
              'number': lambda v: type(v) in (int, float), 'boolean': lambda v: type(v) is bool,
              'null': lambda v: v is None}
    kind = schema.get('type')
    kinds = [kind] if isinstance(kind, str) else kind if isinstance(kind, list) else []
    if kinds and all(isinstance(k, str) and k in checks for k in kinds):
        if not any(checks[k](value) for k in kinds):
            raise ValueError(f'answer field {path} has wrong type; expected {kinds}')
    if isinstance(schema.get('enum'), list) and value not in schema['enum']:
        raise ValueError(f'answer field {path} is outside declared enum')
    if isinstance(value, dict):
        # A present field cannot lose its explicitly required companions,
        # even when submitting an otherwise partial answer. Dependencies are
        # directional; legacy schema-valued dependencies are not inferred.
        for keyword in ('dependentRequired', 'dependencies'):
            dependencies = schema.get(keyword)
            if not isinstance(dependencies, dict):
                continue
            for key, fields in dependencies.items():
                if (key not in value or not isinstance(fields, list)
                        or not all(isinstance(field, str) for field in fields)):
                    continue
                missing = sorted(set(fields)-value.keys())
                if missing:
                    raise ValueError(f'answer field {path}.{key} requires dependent fields: {missing}')
        required = schema.get('required', [])
        if isinstance(required, list) and all(isinstance(k, str) for k in required) and not partial:
            missing = sorted(set(required)-value.keys())
            if missing:
                raise ValueError(f'answer missing required fields at {path}: {missing}')
        properties = schema.get('properties', {})
        if isinstance(properties, dict):
            if schema.get('additionalProperties') is False and value.keys()-properties.keys():
                raise ValueError(f'answer has undeclared fields at {path}')
            for key, item in value.items():
                if key in properties:
                    validate_schema(item, properties[key], partial, path+'.'+key, depth+1)
    if isinstance(value, list) and isinstance(schema.get('items'), dict):
        for index, item in enumerate(value):
            validate_schema(item, schema['items'], partial, path+f'[{index}]', depth+1)


def contract(task):
    statements = [task.text]
    for record in task.evidence.values():
        data = record.get("data", {})
        if (record.get("usable") and data.get("operation") == "read_slice"
                and data.get("completeness") == "complete"
                and (data.get("path") == task.statement_path or str(data.get("path", "")).endswith("spec.md"))):
            statements.append(data.get("text") or "")
    text = "\n".join(statements)
    feedback = str([s.get("feedback", {}) for s in task.submitted])
    result = {
        "json_required": bool((re.search(r"(?<![.\w])JSON\b", text, re.I) and not re.search(r"without\s+JSON|不(?:要|使用|用)\s*JSON|纯文本", text, re.I)) or "合法 JSON" in feedback or "合法JSON" in feedback),
        "execution_required": bool(re.search(r"\bAPI\b|\./check|修复|查询|query|repair|run .*check", text, re.I)),
        "required_fields": explicit_fields(text),
        "schema": answer_schema(text),
        "validation_scope": "explicit field lists and declared structural schema; semantic correctness requires execution/judge evidence",
    }
    paths = checker_paths(task)
    if paths:
        result.update(required_checker_paths=paths,
                      checker_evidence_rule="Run the declared document-relative checkers; selected execution evidence must contain their observed zero exit codes. An outer Python exit of zero is insufficient.")
    return result


def validate(task, spec, value, refs):
    required = contract(task)
    if required["json_required"] and (spec["format"] != "json" or isinstance(value, str)):
        raise ValueError("task requires structured JSON results, not a text summary or JSON string")
    partial = spec.get('partial') is True
    fields = required['required_fields']
    if fields:
        if not isinstance(value, dict):
            raise ValueError('task requires a JSON object with named answer fields')
        missing = sorted(set(fields)-value.keys())
        if missing and not partial:
            raise ValueError('answer missing required fields: '+', '.join(missing))
        if partial and not (set(fields) & value.keys()):
            raise ValueError('partial answer contains no declared answer fields')
    if required['schema']:
        validate_schema(value, required['schema'], partial)
    results = [task.evidence[key].get("data", {}) for key in refs]
    # Concrete observed pagination can disprove a complete API aggregation.
    # This does not infer missing pages, field meanings or unseen API contracts.
    aggregate_fields = set(fields) | set((required.get('schema') or {}).get('properties', {}))
    aggregate = bool(aggregate_fields & {'world_heritage_count','oldest_era'})
    if required['execution_required'] and aggregate and not partial:
        for result in results:
            for event in result.get('runtime_events', []):
                if event.get('kind') != 'json_shape':
                    continue
                coverage = event.get('pagination_coverage')
                if isinstance(coverage,dict) and coverage.get('complete') is False:
                    raise ValueError('API pagination incomplete: observed record ranges do not cover total_count')
                paging = event.get('pagination', {})
                observed = event.get('records_observed',event.get('records_on_page'))
                total = paging.get('total_count') if isinstance(paging,dict) else None
                if type(observed) is int and type(total) is int and observed < total:
                    raise ValueError(f'API pagination incomplete: observed {observed} records of {total}')
    if required["execution_required"] and not any(
            r.get("operation") in {"run_tool", "run_python"} and r.get("status") == "ok"
            and r.get("completeness") == "complete" for r in results):
        raise ValueError("task requires execution results: call the documented API or repair/check the workspace first")
    validate_checkers(task, refs)
    if isinstance(value, str) and re.match(r"\s*(任务信息已获取|任务要求|该任务要求|本任务要求|The task requires)", value, re.I):
        raise ValueError("task description is not a completed answer; execute the required work")
