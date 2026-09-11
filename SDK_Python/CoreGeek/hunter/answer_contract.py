"""Submission gates derived from this task's inspected statement and feedback."""
import re


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
    return {
        "json_required": bool((re.search(r"(?<![.\w])JSON\b", text, re.I) and not re.search(r"without\s+JSON|不(?:要|使用|用)\s*JSON|纯文本", text, re.I)) or "合法 JSON" in feedback or "合法JSON" in feedback),
        "execution_required": bool(re.search(r"\bAPI\b|\./check|修复|查询|query|repair|run .*check", text, re.I)),
    }


def validate(task, spec, value, refs):
    required = contract(task)
    if required["json_required"] and (spec["format"] != "json" or isinstance(value, str)):
        raise ValueError("task requires structured JSON results, not a text summary or JSON string")
    results = [task.evidence[key].get("data", {}) for key in refs]
    if required["execution_required"] and not any(
            r.get("operation") in {"run_tool", "run_python"} and r.get("status") == "ok"
            and r.get("completeness") == "complete" for r in results):
        raise ValueError("task requires execution results: call the documented API or repair/check the workspace first")
    if isinstance(value, str) and re.match(r"\s*(任务信息已获取|任务要求|该任务要求|本任务要求|The task requires)", value, re.I):
        raise ValueError("task description is not a completed answer; execute the required work")
