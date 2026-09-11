"""Structured stdout diagnostics; the competition platform owns log collection."""
from dataclasses import asdict, is_dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import uuid


class Diagnostics(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.run_id = uuid.uuid4().hex
        self.local = threading.local()
        self.output_lock = threading.RLock()
        self.sequence = 0

    def event(self, event, **data):
        # Logging must never replace a valid competition response with a failure.
        try:
            payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"),
                                 default=lambda x: asdict(x) if is_dataclass(x) else
                                 sorted(x, key=repr) if isinstance(x, set) else repr(x))
            # Bound individual lines, retaining the complete payload across parts.
            parts = [payload[i:i+3000] for i in range(0, len(payload), 3000)] or [""]
            with self.output_lock:
                self.sequence += 1
                for index, part in enumerate(parts):
                    record = dict(schema=1, run=self.run_id, seq=self.sequence,
                                  call=getattr(self.local, "call", None),
                                  round=getattr(self.local, "round", None),
                                  time=time.time(), event=event, part=index+1,
                                  parts=len(parts), payload=part)
                    print("HUNTER " + json.dumps(record, ensure_ascii=False, separators=(",", ":")), flush=True)
        except Exception:
            try:
                print("HUNTER_LOG_ERROR: diagnostic output failed", file=sys.stderr, flush=True)
            except Exception:
                pass

    def emit(self, record):
        self.event("exception" if record.exc_info else "warning", message=record.getMessage(),
                   logger=record.name, traceback="".join(traceback.format_exception(*record.exc_info))
                   if record.exc_info else None)

    def startup(self, agent):
        root = Path(__file__).resolve().parents[2]
        hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted((root / "CoreGeek").rglob("*"))
                  if p.is_file() and p.suffix in {".py", ".json"}}
        hashes["run.sh"] = hashlib.sha256((root / "run.sh").read_bytes()).hexdigest()
        self.event("startup", python=sys.version, pid=os.getpid(), files=hashes,
                   rules=agent.rules, policy=agent.policy)

    def run(self, raw, function):
        previous = self.local.__dict__.copy()
        self.local.call = uuid.uuid4().hex
        self.local.round = raw.get("roundNo") if isinstance(raw, dict) else None
        self.local.outcome = "ok"
        started = time.monotonic()
        try:
            self.event("request", request=raw)
            response = function(raw)
            self.event("response", response=response, outcome=self.local.outcome,
                       elapsed_ms=round((time.monotonic()-started)*1000, 3))
            return response
        except Exception:
            self.event("uncaught_exception", traceback=traceback.format_exc())
            raise
        finally:
            self.local.__dict__.clear()
            self.local.__dict__.update(previous)

    def outcome(self, value):
        self.local.outcome = value
