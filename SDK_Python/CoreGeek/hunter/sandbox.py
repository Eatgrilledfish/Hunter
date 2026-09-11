"""Generate bounded judge-side operations. Nothing here executes in the service."""
import ast
import json
import re
import shlex

from .protocol import strict_json, integer
from .dependencies import execution_manifest, input_manifest
from .receipts import guarded_script


def parse_result(text):
    if not isinstance(text, str) or not text:
        return {"status": "missing", "text": "", "complete": False}
    truncated = text.rstrip().endswith("[TRUNCATED]") or len(text.encode()) > 65536
    text = text[:65536]
    head, _, body = text.partition("\n")
    match = re.fullmatch(r"\[exitCode:(-?\d+)\]", head.strip())
    status = "ok" if match and int(match[1]) == 0 else "exit_error" if match else {
        "[TIMEOUT]": "timeout", "[JUDGER_ERROR]": "judger_error"}.get(head.strip(), "invalid")
    if truncated:
        status = "truncated"
    return {"status": status, "text": body[:32768], "complete": status == "ok",
            "exit_code": int(match[1]) if match else None}


def discovery(context):
    marker = "HUNTER_DISCOVER:" + context["nonce"]
    return "printf '%s\\n' " + shlex.quote(marker) + "; pwd -P; command -v python3 || command -v python"


# This source is sent as text to the official sandbox. In tests only, it is
# executed in a separate synthetic task directory to verify the wrapper itself.
OPERATION_SCRIPT = r'''
import hashlib, json, os, stat, subprocess, time, selectors, signal, base64
class InputChanged(ValueError):
    pass
out = {"version": 1, "context": P["context"], "operation": P["operation"]}
try:
    root = os.path.realpath(P["root"])
    if not os.path.isdir(root) or root == os.path.abspath(os.sep):
        raise ValueError("invalid task root")
    def safe(path, regular=False):
        value = os.path.realpath(os.path.join(root, path))
        if os.path.commonpath([root, value]) != root:
            raise ValueError("outside task root")
        if regular and not stat.S_ISREG(os.stat(value).st_mode):
            raise ValueError("not a regular file")
        return value
    op = P["operation"]
    if op == "list_dir":
        folder = safe(P.get("path", "."))
        entries = []
        started = time.monotonic()
        before = os.stat(folder)
        with os.scandir(folder) as listing:
            for count, item in enumerate(listing, 1):
                if count > 8192 or time.monotonic()-started > 1:
                    raise ValueError("directory exceeds local scan budget")
                if item.is_symlink():
                    continue
                if item.is_dir():
                    kind = "directory"
                elif item.is_file():
                    kind = "file"
                else:
                    continue
                relative = os.path.relpath(item.path, root)
                if relative > P["after"]:
                    entries.append({"path": relative, "kind": kind})
        after_stat = os.stat(folder)
        if (before.st_mtime_ns, before.st_ctime_ns) != (after_stat.st_mtime_ns, after_stat.st_ctime_ns):
            raise ValueError("directory changed during listing")
        entries.sort(key=lambda e: e["path"])
        page, used = [], 0
        for entry in entries:
            size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
            if len(page) == 128 or used+size > 20000:
                break
            page.append(entry)
            used += size
        if entries and not page:
            raise ValueError("directory entry exceeds output budget")
        more = len(page) < len(entries)
        out.update(status="ok", path=P["path"], after=P["after"], entries=page,
                   has_more=more, next_after=page[-1]["path"] if more else None,
                   completeness="slice" if more or P["after"] else "complete")
    elif op == "read_slice":
        path = safe(P["path"], True)
        offset, limit = P["offset"], P["limit"]
        with open(path, "rb") as f:
            before = os.fstat(f.fileno())
            size = before.st_size
            if offset > size:
                raise ValueError("offset beyond end of file")
            f.seek(offset)
            block = f.read(limit)
            sha = None
            if size <= 131072:
                f.seek(0)
                sha = hashlib.sha256(f.read(131073)).hexdigest()
            after = os.fstat(f.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("file changed during slice read")
        try:
            text = block.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        out.update(status="ok", path=P["path"], text=text, file_size=size,
                   bytes_base64=base64.b64encode(block).decode("ascii"),
                   offset=offset, next_byte=offset+len(block), has_more=offset+len(block)<size,
                   file_sha256=sha, chunk_sha256=hashlib.sha256(block).hexdigest(),
                   completeness="complete" if offset == 0 and len(block) == size else "slice")
        if len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > 30000:
            out.pop("text", None)  # Raw bytes remain complete; decode in the SDK.
    elif op in {"run_tool", "run_python"}:
        path = safe(P["path"], op == "run_tool")
        out.update(executed=False, path=P["path"], manifest=P["manifest"], input_manifest=P["input_manifest"],
                   dependency_scope=("task documentation only; generated program dependencies not fingerprinted" if op == "run_python"
                                     else "declared files and static local Python imports; dynamic/system dependencies not complete"))
        def verify_manifest():
            for candidate in P["import_candidates"]:
                location = safe(candidate)
                if os.path.isfile(location) and candidate not in P["manifest"]:
                    raise ValueError("uninspected local import: "+candidate)
            for relative, expected in {**P["manifest"], **P["input_manifest"]}.items():
                try:
                    location = safe(relative, True)
                    with open(location, "rb") as source:
                        before = os.fstat(source.fileno())
                        if before.st_size > 131072:
                            raise ValueError("file exceeds fingerprint budget")
                        digest = hashlib.sha256(source.read(131073)).hexdigest()
                        after = os.fstat(source.fileno())
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise ValueError("file changed while hashing: "+relative)
                    if digest != expected:
                        raise ValueError("file fingerprint changed: "+relative)
                except (ValueError, OSError) as exc:
                    if relative in P["input_manifest"]:
                        raise InputChanged(str(exc)) from exc
                    raise
        verify_manifest()
        sha = P.get("code_sha256") or P["manifest"][P["path"]]
        # Load local Python source directly, so a stale timestamp-based .pyc
        # cannot disagree with the files whose hashes were just verified.
        bootstrap = """
import os, sys, runpy, importlib.machinery
task_root = os.getcwd()
original_code = importlib.machinery.SourceFileLoader.get_code
def source_code(loader, fullname):
    location = os.path.realpath(loader.path)
    if os.path.commonpath([task_root, location]) == task_root:
        return loader.source_to_code(loader.get_data(loader.path), loader.path)
    return original_code(loader, fullname)
importlib.machinery.SourceFileLoader.get_code = source_code
sys.dont_write_bytecode = True
entry = sys.argv[1]
sys.argv = sys.argv[1:]
sys.path.insert(0, os.path.dirname(entry))
runpy.run_path(entry, run_name="__main__")
"""
        argv = [P["python"], "-I", "-B", "-c", bootstrap, path] if path.endswith(".py") else [path]
        argv.extend(P["args"])
        if op == "run_python":
            argv = [P["python"], "-I", "-B", "-c", P["code"]]
        child = subprocess.Popen(argv, cwd=path if op == "run_python" else root, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        out["executed"] = True
        output, status = bytearray(), "ok"
        limit_time = time.monotonic()+11
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        try:
            while selector.get_map():
                if time.monotonic() >= limit_time:
                    status = "timeout"
                    break
                for key, _ in selector.select(min(0.1, max(0, limit_time-time.monotonic()))):
                    block = os.read(key.fd, 4096)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(block[:max(0, 16384-len(output))])
                    if len(output) >= 16384:
                        status = "truncated"
                        break
                if status != "ok":
                    break
            if status == "ok":
                try:
                    child.wait(timeout=max(0.001, limit_time-time.monotonic()))
                except subprocess.TimeoutExpired:
                    status = "timeout"
        finally:
            # Kill a remaining process group, including descendants retaining pipes.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=1)
            selector.close()
            child.stdout.close()
        if status == "ok" and child.returncode != 0:
            status = "exit_error"
        try:
            verify_manifest()
        except (ValueError, OSError) as exc:
            status = "input_changed" if isinstance(exc, InputChanged) else "dependency_changed"
            out["dependency_error"] = str(exc)[:512]
        try:
            text = output.decode("utf-8")
        except UnicodeDecodeError:
            text = None
            if status == "ok":
                status = "invalid_encoding"
        out.update(status=status, path=P["path"], file_sha256=sha, text=text,
                   tool_exit_code=child.returncode, completeness="complete" if status == "ok" else "partial")
        if status == "ok":
            try:
                def unique(pairs):
                    obj = {}
                    for key, value in pairs:
                        if key in obj:
                            raise ValueError("duplicate tool JSON key")
                        obj[key] = value
                    return obj
                def invalid_constant(value):
                    raise ValueError("nonfinite tool JSON number")
                out["data"] = json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)
            except ValueError:
                pass
    else:
        raise ValueError("unsupported operation")
except Exception as exc:
    out.update(status="error", error_type=type(exc).__name__, message=str(exc)[:512], completeness="unknown")
    if isinstance(exc, InputChanged):
        out["failure_scope"] = "input"
encoded = json.dumps(out, ensure_ascii=False, allow_nan=False)
if len(encoded.encode("utf-8")) > 30000 and "data" in out:
    out.pop("text", None)  # Preserve structured output without a duplicate copy.
    encoded = json.dumps(out, ensure_ascii=False, allow_nan=False)
while len(encoded.encode("utf-8")) > 30000 and out.get("text"):
    out["text"] = out["text"][:len(out["text"])//2]
    out.update(status="truncated", completeness="partial", transport_budget_exceeded=True)
    encoded = json.dumps(out, ensure_ascii=False, allow_nan=False)
RESULT = encoded
'''


def compile_operation(context, plan, environment, evidence):
    """Validate discovered paths, inspected tool fingerprint, and bounded argv."""
    if not isinstance(plan, dict) or plan.get("operation") not in {"list_dir", "read_slice", "run_tool", "run_python"}:
        raise ValueError("unsupported command plan")
    operation = plan["operation"]
    root, python = environment.get("root"), environment.get("python")
    if not isinstance(root, str) or not root.startswith("/") or root == "/":
        raise ValueError("task root not discovered")
    if not isinstance(python, str) or not python.startswith("/"):
        raise ValueError("interpreter not discovered")
    path = plan.get("path", ".")
    if not isinstance(path, str) or len(path) > 1024 or "\x00" in path:
        raise ValueError("invalid path")
    known_paths = {".": "directory"}
    inspections = {}
    for record in evidence.values():
        if record.get("source") != "sandbox" or not record.get("usable"):
            continue
        data = record["data"]
        for entry in data.get("entries", []):
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                known_paths[entry["path"]] = entry.get("kind")
        if data.get("operation") == "read_slice" and data.get("file_sha256"):
            inspections[data.get("path")] = data
    if path not in known_paths:
        raise ValueError("path not discovered in current task")
    payload = {"context": context, "root": root, "python": python, "operation": operation, "path": path}
    if operation == "list_dir":
        if known_paths[path] != "directory":
            raise ValueError("not a directory")
        after = plan.get("after", "")
        if not isinstance(after, str) or len(after) > 1024 or "\x00" in after:
            raise ValueError("invalid directory cursor")
        payload["after"] = after
    elif operation == "read_slice":
        offset, limit = plan.get("offset", 0), plan.get("limit", 4096)
        if not integer(offset, 0) or not integer(limit, 1) or limit > 8192:
            raise ValueError("invalid read bounds")
        if known_paths[path] != "file":
            raise ValueError("not a discovered file")
        payload.update(offset=offset, limit=limit)
    elif operation == "run_python":
        code = plan.get("code")
        refs = plan.get("evidence_refs")
        if known_paths[path] != "directory":
            raise ValueError("working directory must be discovered")
        if not isinstance(code, str) or not code.strip() or len(code.encode()) > 16384:
            raise ValueError("invalid Python program size")
        try:
            ast.parse(code)
        except SyntaxError as exc:
            raise ValueError("invalid Python syntax: "+str(exc)) from exc
        if plan.get("effect") not in {"read_only", "mutation"}:
            raise ValueError("program effect declaration required")
        if not isinstance(refs, list) or not refs or len(refs) > 8:
            raise ValueError("program requires inspected task documentation")
        manifest = {}
        for ref in refs:
            record = evidence.get(ref, {}) if isinstance(ref, str) else {}
            data = record.get("data", {})
            if (not record.get("usable") or data.get("operation") != "read_slice"
                    or data.get("completeness") != "complete" or not data.get("file_sha256")
                    or not isinstance(data.get("text"), str)):
                raise ValueError("program documentation must be completely read")
            manifest[data["path"]] = data["file_sha256"]
        import hashlib
        payload.update(code=code, code_sha256=hashlib.sha256(code.encode()).hexdigest(),
                       args=[], manifest=manifest, input_manifest={}, import_candidates=[])
    else:
        inspection = inspections.get(path)
        if inspection is None or inspection.get("completeness") != "complete":
            raise ValueError("tool must be fully inspected in current task")
        args = plan.get("args")
        if not isinstance(args, list) or len(args) > 32 or any(not isinstance(x, str) or len(x) > 1024 or "\x00" in x for x in args):
            raise ValueError("invalid argv")
        # Arguments remain separate argv values, never shell expressions. Explicit
        # effect declarations govern retry policy; they do not prove tool safety.
        if plan.get("effect") not in {"read_only", "mutation"}:
            raise ValueError("tool effect declaration required")
        payload.update(args=args, file_sha256=inspection["file_sha256"])
        manifest, import_candidates = execution_manifest(plan, evidence)
        inputs = input_manifest(plan, evidence, manifest)
        payload.update(manifest=manifest, import_candidates=import_candidates, input_manifest=inputs)
    if operation in {"run_tool", "run_python"}:
        namespace = environment.get("receipt_namespace")
        if namespace is None:
            # Direct compiler clients without a TaskEngine retain command-local
            # identity, but cannot claim isolation between fresh service runs.
            namespace = "direct-compiler"
        if not isinstance(namespace, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,80}", namespace):
            raise ValueError("invalid receipt namespace")
        payload["receipt_namespace"] = namespace
        script = guarded_script(payload, OPERATION_SCRIPT)
    else:
        script = "P = " + repr(payload) + "\n" + OPERATION_SCRIPT + "\nprint(RESULT)"
    # The wrapper's own imports must not resolve to task files such as json.py.
    return shlex.quote(python) + " -I -c " + shlex.quote(script)


def task_documents(text):
    """File references are locators, never the task contents themselves."""
    return sorted(set(re.findall(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.md(?![A-Za-z0-9_.-])", text)))[:8]


LOCATE_SCRIPT = r'''
import json, os, time
out = {"version":1, "context":P["context"], "operation":"locate_task"}
started = time.monotonic()
matches, errors, visited = [], [], 0
# The second root is observed in the competition trace, not an assumed cwd.
roots = [P["cwd"]] if P["cwd"] != "/" else []
roots.append("/tmp/selfEvolutionTask")
complete = True
for root in dict.fromkeys(roots):
    if not os.path.isdir(root) or os.path.islink(root):
        continue
    def onerror(exc):
        errors.append(str(exc)[:256])
    for folder, dirs, files in os.walk(root, topdown=True, onerror=onerror, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not os.path.islink(os.path.join(folder,d)))
        if len(os.path.relpath(folder, root).split(os.sep)) >= 6:
            if dirs: complete = False
            dirs[:] = []
        visited += len(dirs)+len(files)+1
        if visited > 4096 or time.monotonic()-started > 1.5:
            complete = False
            break
        for name in sorted(files):
            if name in P["names"]:
                path = os.path.join(folder,name)
                if os.path.isfile(path) and not os.path.islink(path):
                    matches.append(os.path.realpath(path))
        if len(matches) > 32:
            complete = False
            break
    if not complete: break
matches = sorted(set(matches))
out.update(candidates=matches[:32], scan_complete=complete and not errors, scan_errors=errors[:8])
if len(matches) == 1:
    path = matches[0]
    out.update(status="ok", root=os.path.dirname(path), statement=os.path.basename(path),
               entries=[{"path":os.path.basename(path), "kind":"file"}], completeness="complete",
               selection="unique_observed_candidate; scan completeness recorded separately")
else:
    out.update(status="ambiguous" if len(matches)>1 else "incomplete" if not complete or errors else "not_found",
               completeness="unknown")
print(json.dumps(out, ensure_ascii=False))
'''


def locate_task(context, environment, names):
    python, cwd = environment.get("python"), environment.get("root")
    if not isinstance(python, str) or not python.startswith("/") or not isinstance(cwd, str) or not cwd.startswith("/"):
        raise ValueError("interpreter/cwd not discovered")
    if not names or any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.md", name) for name in names):
        raise ValueError("task document names unavailable")
    payload = {"context":context, "cwd":cwd, "names":names}
    return shlex.quote(python) + " -I -c " + shlex.quote("P = " + repr(payload) + "\n" + LOCATE_SCRIPT)
