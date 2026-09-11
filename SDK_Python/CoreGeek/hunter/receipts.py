"""Source embedded in judge-side commands; never executed by the callback."""

RECEIPT_SOURCE = r'''
import hashlib
import json
import os
from pathlib import Path
import stat


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, ensure_ascii=False).encode()


def durable_write(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def read_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError('invalid receipt file')
        return json.loads(stream.read(65537))


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def execute_once(ledger, identity, payload, operation):
    ledger = Path(ledger)
    ledger.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not stat.S_ISDIR(ledger.lstat().st_mode):
        raise ValueError('ledger must be a directory, not a symlink')
    key = hashlib.sha256(encoded(identity)).hexdigest()
    request_hash = hashlib.sha256(encoded(payload)).hexdigest()
    claim, result = ledger/(key+'.claim'), ledger/(key+'.result')
    try:
        durable_write(claim, encoded({'request_hash': request_hash}))
    except FileExistsError:
        try:
            previous = read_regular(claim)
            if previous['request_hash'] != request_hash:
                return {'status': 'identity_conflict'}
            saved = read_regular(result)
            if saved['request_hash'] != request_hash:
                return {'status': 'unknown'}
            return {'status': 'complete', 'replayed': True, 'result': saved['result']}
        except (OSError, ValueError, KeyError, TypeError):
            return {'status': 'unknown'}
    # Failure from here leaves the claim in place, including failure to publish
    # a receipt after a successful operation. No automatic retry is safe then.
    sync_directory(ledger)
    value = operation()
    content = encoded({'request_hash': request_hash, 'result': value})
    if len(content) > 65536:
        raise ValueError('result exceeds receipt budget')
    staging = ledger/(key+'.pending')
    durable_write(staging, content)
    os.replace(staging, result)
    sync_directory(ledger)
    return {'status': 'complete', 'replayed': False, 'result': value}

'''


def guarded_script(payload, operation_source):
    return ('P = ' + repr(payload) + '\n' + RECEIPT_SOURCE + '\n' +
            'OPERATION_SOURCE = ' + repr(operation_source) + '\n' + RUN_RECEIPTED)


RUN_RECEIPTED = r'''
def invoke():
    namespace = {"P": P}
    exec(OPERATION_SOURCE, namespace)
    return namespace["RESULT"]
try:
    # This directory is SDK bookkeeping in the discovered task root. It must
    # survive duplicate command delivery; it is not a security boundary against
    # task programs with permission to delete or modify it.
    root = os.path.realpath(P["root"])
    if not os.path.isdir(root) or root == os.path.abspath(os.sep):
        raise ValueError("invalid task root")
    base = Path(root)/".hunter-execution-receipts-v1"
    base.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(base.lstat().st_mode):
        raise ValueError("receipt parent is not a directory")
    ledger = base/P["receipt_namespace"]
    outcome = execute_once(ledger, P["context"], P, invoke)
    if outcome["status"] == "complete":
        print(outcome["result"])
    else:
        print(json.dumps({"version": 1, "context": P["context"], "operation": P["operation"],
                          "status": "receipt_" + outcome["status"], "executed": None,
                          "completeness": "unknown"}))
except Exception as error:
    print(json.dumps({"version": 1, "context": P["context"], "operation": P["operation"],
                      "status": "receipt_error", "executed": None, "completeness": "unknown",
                      "message": str(error)[:512]}))
'''
