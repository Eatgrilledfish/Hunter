"""Bounded recovery from observed task failures, without accepting failed output."""
import posixpath
import re

from .protocol import fingerprint


def progress(task):
    return fingerprint([list(task.evidence), [(s['hash'], s.get('feedback')) for s in task.submitted]])


def stalled(task, reason, round_no):
    signature = progress(task)
    previous = task.recovery.get('stall', {})
    task.recovery['stall'] = dict(signature=signature,
        count=previous.get('count', 0)+1 if previous.get('signature') == signature else 1,
        reason=reason[:512], round=round_no)


def blocked(task):
    state = task.recovery.get('stall', {})
    return state.get('count', 0) >= 3 and state.get('signature') == progress(task)


def latest_failure(task):
    latest = next((r for r in reversed(list(task.evidence.values()))
                   if r.get('data', {}).get('operation') in {'run_python', 'run_tool'}), None)
    return bool(latest and (not latest.get('usable') or latest.get('answer_usable') is False))


def needs_execution(task):
    latest = next((key for key,r in reversed(list(task.evidence.items()))
                   if r.get('data', {}).get('operation') in {'run_python', 'run_tool'}), None)
    return latest_failure(task) or bool(latest and task.recovery.get('recompute_after') == latest)


def feedback(task):
    state = task.recovery.get('stall', {})
    current = state if state.get('signature') == progress(task) else {}
    return dict(required_action='cmd' if needs_execution(task) else None,
        reason='Latest execution or answer validation failed; inspect or run a corrected program before submitting.'
               if needs_execution(task) else current.get('reason'),
        no_progress_count=current.get('count', 0),
        validation=[{k: row[k] for k in ('kind', 'round', 'reason', 'auto_submit_block_reason') if k in row}
                    for row in task.diagnostic_events[-3:]])


def recheck(task, data, pending, evidence_id, round_no):
    """One fresh check after an outer failure; never replay the repair program.

    Only a captured no-argument checker with an unchanged file and ordinary
    invocation options is eligible. Opaque invocations stay with the model.
    """
    from .checker_contract import checker_paths
    required = checker_paths(task)
    if data.get('status') != 'exit_error' or len(required) != 1:
        return
    row = next((r for r in data.get('checker_outputs', [])
                if isinstance(r, dict) and r.get('path') == required[0]
                and type(r.get('returncode')) is int and r['returncode'] == 0
                and r.get('complete') is True), None)
    replay = row.get('recheck') if row else None
    if not isinstance(replay, dict):
        return
    path, cwd, sha = required[0], replay.get('cwd'), replay.get('sha256')
    if (not isinstance(cwd, str) or cwd.startswith('/') or '..' in cwd.split('/')
            or posixpath.dirname(path) != ('' if cwd == '.' else cwd)
            or not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha)):
        return
    identity = fingerprint([path, cwd, sha])
    attempted = task.recovery.setdefault('checkers', [])
    if identity in attempted:
        return
    refs = pending.get('plan', {}).get('evidence_refs', [])
    if not refs:
        return
    code = ('import hashlib, pathlib, subprocess\n'
            f'p = pathlib.Path({path!r})\n'
            f'assert not p.is_symlink() and hashlib.sha256(p.read_bytes()).hexdigest() == {sha!r}, "checker changed; inspect again"\n'
            f'r = subprocess.run([{("./"+posixpath.basename(path))!r}], cwd={cwd!r}, capture_output=True, text=True, check=True)\n'
            'print(r.stdout, end="")\n')
    task.command_plan = dict(operation='run_python', path='.', effect='mutation',
                             code=code, evidence_refs=list(refs))
    from .answer_contract import contract
    required_answer=contract(task)
    fields=set(required_answer['required_fields']) | set((required_answer.get('schema') or {}).get('required',[]))
    if required_answer['json_required'] and fields=={'token'}:
        task.command_plan['answer_output']=dict(format='json',selector=['data'])
    attempted.append(identity)
    task.events.append(dict(kind='checker_revalidation_scheduled', round=round_no,
                            evidence=evidence_id, path=path))
    task.diagnostic_events.append(dict(kind='checker_revalidation_scheduled',round=round_no,
        reason='checker completed before outer failure; verifying unchanged checker in a fresh execution',evidence=evidence_id))
    task.diagnostic_events=task.diagnostic_events[-8:]
