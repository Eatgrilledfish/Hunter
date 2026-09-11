"""Copyable stdout summaries by default; full event capture is opt-in."""
from collections import Counter, OrderedDict
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

from .protocol import obj, array
from . import llm_trace


class Diagnostics(logging.Handler):
    def __init__(self, mode=None, interval=20):
        super().__init__(logging.WARNING)
        self.mode = "full" if (mode or os.environ.get("HUNTER_LOG_MODE")) == "full" else "compact"
        self.interval = max(1, int(interval))
        self.run_id = uuid.uuid4().hex
        self.local = threading.local()
        self.output_lock = threading.RLock()
        self.sequence = 0
        self.sessions = OrderedDict()
        self.external_counts = Counter()

    @staticmethod
    def _brief(value, limit=160):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
        return text if len(text) <= limit else text[:limit-1] + "…"

    @staticmethod
    def _pos(value):
        return f"{value.get('x','?')},{value.get('y','?')}" if isinstance(value, dict) else "?"

    def _command(self, command):
        action = command.get("action", "?")
        detail = command.get("controllerId", "")
        if "targetPos" in command:
            detail += ">" + ";".join(self._pos(p) for p in command["targetPos"][:3])
        if "name" in command:
            detail += ":" + str(command["name"]) + "*" + str(command.get("num",1))
        if "taskAnswer" in command:
            answer = command["taskAnswer"]
            detail += ":" + self._brief(answer,72) + "#" + hashlib.sha256(answer.encode()).hexdigest()[:8]
        return self._brief(action + detail,120)

    def _write_compact(self, event, **data):
        record = dict(schema=2, run=self.run_id[:8], round=getattr(self.local,"round",None), event=event, **data)
        # Valid single-line JSON, never a byte slice of a serialized record.
        # Essential issue details take precedence over optional current context.
        for optional in ("reasons", "channels", "work", "commands", "units", "task", "defence", "docs", "root_entries", "entries"):
            if len(json.dumps(record,ensure_ascii=False,separators=(",", ":")).encode()) <= 1400:
                break
            record.pop(optional,None)
            record["context_cut"] = True
        if len(json.dumps(record,ensure_ascii=False,separators=(",", ":")).encode()) > 1400:
            record["issue"] = [self._brief(i,100) for i in record.get("issue",[])[:2]]
            record["context_cut"] = True
            if "detail" in record:
                record["detail"] = self._brief(record["detail"],180)
        # Preserve exception tails even when multibyte text exceeds the line budget.
        while isinstance(record.get("result"),str) and len(record['result']) > 40 and len(
                json.dumps(record,ensure_ascii=False,separators=(",", ":")).encode()) > 1400:
            value = record['result'];keep = len(value)//3
            record['result'] = value[:keep]+" … "+value[-keep:]
            record['context_cut'] = True
        print("HUNTER " + json.dumps(record,ensure_ascii=False,separators=(",", ":")),flush=True)

    def _compact_event(self, event, data):
        if event == "decision":
            self.local.decision = data
        elif event == "attack_checks":
            self.local.attacks = data.get("checks",[])
        elif event == "task_state":
            active = data.get("active") or {}
            pending = active.get("sandbox_pending") or {}
            closed = data.get("closed") or []
            faults = [e for e in active.get("events",[]) if e.get("kind") in
                      {"quarantined_llm", "invalid_llm", "invalid_command_plan", "missing_llm", "missing_command_effect_unknown"}]
            self.local.task_fault = faults[-1] if faults else None
            self.local.llm_verdict = next((e for e in reversed(active.get('events',[]))
                if e.get('round')==getattr(self.local,'round',None) and e.get('kind') in
                {'llm_consumed','invalid_llm','quarantined_llm','invalid_command_plan'}),{})
            executed = [e for e in active.get("events",[]) if e.get("kind") == "sandbox_result"
                        and e.get("op") in {"run_python", "run_tool"}]
            self.local.task_tool = executed[-1] if executed else None
            self.local.task = {
                "id": hashlib.sha256(str(active.get("key")).encode()).hexdigest()[:8] if active else None,
                "phase":active.get("phase"), "file":self._brief(active.get("statement_path"),100),
                "root":self._brief((active.get("environment") or {}).get("root"),100),
                "read":active.get("statement_ready"), "op":pending.get("operation"),
                "llm":bool(active.get("llm_pending")),
                "left":max(0,active["timeout"]-(getattr(self.local,"round",0)-(active.get("accept_round") or active.get("activation_round") or 0))) if active.get("timeout") is not None else None,
                "submitted":len(active.get("submitted",[])),
                "last": self._brief(active.get("events",[])[-1:],180),
                "end":self._brief(closed[-1].get("reason"),120) if not active and closed else None}
            self.local.task = {k:v for k,v in self.local.task.items() if v is not None and v not in ("null","[]")}
            if not active and not closed:
                self.local.task = {}
                self.local.task_fault = None
        elif event not in {"request","response"}:
            issue = self._brief({"kind":event,**data},320)
            if hasattr(self.local,"issues"):
                self.local.issues.append(issue)
                if event in {"exception","uncaught_exception"}:
                    self.local.critical = True
            else:
                # Malformed requests can recur without entering Agent.callback.
                with self.output_lock:
                    self.external_counts[event] += 1
                    count = self.external_counts[event]
                    if count == 1 or count % self.interval == 0:
                        self._write_compact(event,count=count,detail=issue)

    def _llm_turn(self,state,raw,response,task,number):
        pending=state.get('llm_trace_pending');logged=False
        counts=state.setdefault('llm_trace_counts',OrderedDict())
        if pending:
            reply=raw.get('llmResp')
            ended=task.get('id')!=pending['task']
            if reply or ended or number-pending['round']>2:
                identity=pending['task'];counts.setdefault(identity,0)
                output=llm_trace.received(reply,pending['input'].get('_documents','')) if isinstance(reply,str) and reply else {}
                if output.get('code_hash'):
                    mappings=state.setdefault('program_rids',{})
                    mappings[output['code_hash']]=pending['input'].get('rid')
                    if len(mappings)>16:del mappings[next(iter(mappings))]
                if counts[identity]<3:
                    verdict=getattr(self.local,'llm_verdict',{})
                    record={'task':identity,'rid':pending['input'].get('rid'),'sent_round':pending['round'],
                            'wait_rounds':number-pending['round'],
                            'verdict':('task_changed' if ended else verdict.get('kind','received') if reply else 'missing'),
                            'input':{k:v for k,v in pending['input'].items() if not k.startswith('_') and k!='rid'},
                            'output':output}
                    if verdict.get('reason'):record['reason']=str(verdict['reason'])[:100]
                    # Keep valid one-line JSON and a strict extra-line byte budget.
                    for group,key in [('input','docs'),('output','imports'),('output','endpoints'),('output','auth'),('output','launch')]:
                        if len(json.dumps(record,ensure_ascii=False).encode())<=1000:break
                        record[group].pop(key,None);record['cut']=True
                    if len(json.dumps(record,ensure_ascii=False).encode())>1100:
                        record['output']={k:v for k,v in output.items() if k in {'reply_hash','reply_chars','parse','code_hash','intent','op'}}
                        record['cut']=True
                    self._write_compact('llm',**record);counts[identity]+=1;logged=True
                else:state['stats']['llm_trace_omitted']+=1
                state['llm_trace_pending']=None
        if response.get('prompt') and task.get('id'):
            state['llm_trace_pending']={'task':task['id'],'round':number,'input':llm_trace.sent(response['prompt'])}
        while len(counts)>32:counts.popitem(last=False)
        return logged

    def _compact_turn(self, raw, response, elapsed):
        raw = obj(raw)
        team = obj(raw.get("teamOur"))
        key = (str(team.get("teamId","?")),str(team.get("type","?")))
        number = raw.get("roundNo")
        commands = {str(k):self._command(c) for k,c in response.get("roleCommandMap",{}).items()}
        units = {str(u.get("id")):u for u in array(team.get("roles")) if isinstance(u,dict)}
        with self.output_lock:
            state = self.sessions.setdefault(key,dict(round=None,commands={},units={},attacks={},
                failures={},seen=set(),stats=Counter(),calls=0,task=None,issues=[],issue_round=None,
                details=0,critical_reported=False,targets={},task_faults=set(),task_tools=set()))
            self.sessions.move_to_end(key)
            while len(self.sessions)>4:
                self.sessions.popitem(last=False)
            if type(number) is not int or (state["round"] is not None and number<=state["round"]):
                state["stats"]["duplicate_or_stale"] += 1
                if self.local.outcome not in {"ok","cache_hit","isolated_stale_or_other_session"}:
                    count = state["stats"]["duplicate_or_stale"]
                    if count == 1 or count % self.interval == 0:
                        self._write_compact("callback_error",count=count,outcome=self.local.outcome,
                                            issue=getattr(self.local,"issues",[])[:2])
                return
            previous_round = state["round"]
            state["calls"] += 1
            state["stats"].update(c.get("action","?") for c in response.get("roleCommandMap",{}).values())
            issues = list(getattr(self.local,"issues",[]))[:2]
            critical = getattr(self.local,"critical",False)
            if critical:
                state["stats"]["exceptions"] += 1
            failures = []
            new_failure = False
            feedback = obj(raw.get("lastRoundRoleActionResults"))
            for actor, accepted in feedback.items():
                actor = str(actor)
                if accepted is not False:
                    if accepted is True:
                        state["failures"].pop(actor,None)
                    continue
                state["stats"]["action_fail"] += 1
                attributed = previous_round is not None and number == previous_round+1 and actor in state["commands"]
                action = state["commands"].get(actor) if attributed else "unknown_previous_command"
                origin = self._pos(state["units"].get(actor,{}).get("pos")) if attributed else "?"
                signature = (action,origin)
                old = state["failures"].get(actor)
                count = old[1]+1 if old and old[0]==signature else 1
                state["failures"][actor] = (signature,count)
                new_failure |= count == 1
                detail = f"{actor} {action} from={origin} now={self._pos(units.get(actor,{}).get('pos'))} n={count}"
                if attributed and actor in state["attacks"]:
                    check = state["attacks"][actor]
                    detail += " check=" + self._brief({"control_d":check.get("controller_distance"),
                        "range":check.get("range"),"target_d":check.get("target_distances"),"cd":check.get("cooldown")},140)
                elif attributed and state["targets"].get(actor):
                    detail += " occupied=" + state["targets"][actor]
                failures.append(self._brief(detail,260))
            for error in array(raw.get("errors"))[:3]:
                if isinstance(error,dict):
                    state["stats"]["error_"+self._brief(str(error.get("errorCode")),8)] += 1
                    detail = "judge:"+self._brief(error,180)
                    if error.get("errorCode")==2 and previous_round is not None and number==previous_round+1:
                        submitted = [(a,c) for a,c in state["commands"].items() if c.startswith("submitAnswer")]
                        if submitted:
                            actor, action = submitted[0]
                            detail += f" after={actor} ack={feedback.get(actor)} {action}"
                    issues.append(self._brief(detail,320))
            task = getattr(self.local,"task",{})
            llm_logged = self._llm_turn(state,raw,response,task,number)
            tool = getattr(self.local,"task_tool",None)
            tool_key = (task.get("id"),(tool or {}).get("round"))
            if tool and task.get("id") and tool_key not in state["task_tools"]:
                if sum(k[0]==task["id"] for k in state["task_tools"]) < 2:
                    self._write_compact("task_exec",task=task["id"],left=task.get("left"),
                        rid=state.get('program_rids',{}).get(str(tool.get('program') or '')[:12]),
                        op=tool.get("op"),path=self._brief(tool.get("path"),100),status=tool.get("status"),
                        exit=tool.get("exit"),usable=tool.get("usable"),answer_usable=tool.get('answer_usable'),
                        failure=tool.get('failure'),cwd=tool.get('cwd'),entries=(tool.get('entries') or [])[:8],
                        adapters=tool.get('adapters',[]),
                        root_entries=(tool.get('root_entries') or [])[:8],
                        program=str(tool.get('program') or '')[:12],docs=tool.get('docs'),result=self._brief(tool.get("result",""),480),
                        result_round=tool.get("round"))
                state["task_tools"].add(tool_key)
                if len(state["task_tools"]) > 32:state["task_tools"]={tool_key}
            fault = getattr(self.local,"task_fault",None)
            fault_key = (task.get("id"),(fault or {}).get("kind"))
            if fault and llm_logged and fault.get('round')==number:
                state['task_faults'].add(fault_key)
            if fault and fault_key not in state["task_faults"] and not (llm_logged and fault.get("round")==number):
                # One diagnostic per failure kind per task, capped to two per
                # task. Never let discovery events consume this allowance.
                if sum(k[0]==task.get("id") for k in state["task_faults"]) < 2:
                    self._write_compact("task_fault",task=task.get("id"),left=task.get("left"),
                        kind=fault.get("kind"),reason=self._brief(fault.get("reason",""),120),
                        op=fault.get("op"),path=self._brief(fault.get("path"),100),
                        expected=fault.get("expected"),received=self._brief(fault.get("received",""),120),
                        reply=self._brief(fault.get("reply",""),240),fault_round=fault.get("round"))
                state["task_faults"].add(fault_key)
                if len(state["task_faults"]) > 32:
                    state["task_faults"] = {fault_key}
            task_marker = (task.get("id"),task.get("phase"),task.get("file"),task.get("read"),task.get("end"))
            task_changed = task_marker != state["task"] and bool(task.get("id") or task.get("end"))
            # Keep the latest issue sample in periodic summaries even when
            # repeated individual reports are suppressed.
            if issues or failures:
                state["issues"] = (issues+failures)[:3]
                state["issue_round"] = number
            fresh_issue = any(i not in state["seen"] for i in issues)
            state["seen"].update(issues)
            if len(state["seen"])>32:
                state["seen"] = set(issues)
            periodic = state["calls"] % self.interval == 0
            trigger = new_failure or fresh_issue or task_changed
            # At most two extra lines per twenty accepted observations. The
            # periodic summary retains counters and the latest suppressed issue.
            urgent = critical and fresh_issue and not state["critical_reported"]
            emit = state["calls"] == 1 or periodic or urgent or (trigger and state["details"]<2)
            if trigger and not emit:
                state["stats"]["merged_events"] += 1
            if emit:
                if critical:
                    state["critical_reported"] = True
                if state["calls"] != 1 and not periodic:
                    state["details"] += 1
                mobiles = [f"{i}@{self._pos(u.get('pos'))}/{u.get('health','?')}" for i,u in units.items()
                           if u.get("roleType") in {"worker","pioneer"}][:3]
                bases = [u.get("health") if type(u.get("health")) is int else None
                         for u in units.values() if u.get("roleType")=="station"][:2]
                decision = getattr(self.local,"decision",{})
                reasons = [self._brief(f"{c.get('actor')}:{c.get('reason')}",90) for c in decision.get("selected",[])][:3]
                channels = {k:self._brief(raw.get(k),140) for k in ("lastCmdResult","llmResp") if raw.get(k)} if issues else {}
                if decision and (periodic or state["calls"]==1):
                    self._write_compact("defence",supply=decision.get("wall_supply"),guns=decision.get("gun_status"),
                        returns={i:{k:r.get(k) for k in ("due","length")} for i,r in decision.get("return_routes",{}).items()},
                        upgrades={i:{'hp':u.get('health'),'level':u.get('level')} for i,u in units.items()
                                  if u.get('roleType') in {'rocket','gatling','railgun'}},
                        layout=next((r.get("reason") for r in decision.get("rejected",[]) if r.get("verdict")=="layout_bundle_rejected"),
                                    obj(decision.get("battery_plan")).get("mode")))
                self._write_compact("summary" if periodic else "turn",team=self._brief("/".join(key),48),
                    gold=team.get("goldNum") if type(team.get("goldNum")) is int else None,
                    score=team.get("totalScore") if type(team.get("totalScore")) is int else None,base_hp=bases,
                    robots=len(array(obj(raw.get("robot")).get("roles"))),units=mobiles,
                    walls=sum(u.get("roleType")=="wall" and (u.get("health") or 0)>0 for u in units.values()),
                    guns=[f"{i}:{u.get('roleType')}@{self._pos(u.get('pos'))}" for i,u in units.items()
                          if u.get("roleType") in {"gatling","railgun","rocket"}][:3],
                    commands=commands,task=task,reasons=reasons,channels=channels,
                    work=decision.get("work_status"),

                    issue=state["issues"] if periodic else (issues+failures)[:3],
                    issue_round=state["issue_round"] if periodic else number if issues or failures else None,
                    stats=dict(state["stats"]),ms=round(elapsed,1),outcome=self.local.outcome)
            occupied = {}
            for group in (team,obj(raw.get("teamEnemy")),obj(raw.get("robot"))):
                for unit in array(group.get("roles")):
                    unit = obj(unit); pos = obj(unit.get("pos"))
                    if unit.get("health")==0 or type(pos.get("x")) is not int or type(pos.get("y")) is not int:
                        continue
                    cells = [(pos['x'],pos['y'])]
                    if unit.get('roleType')=='station':
                        cells += [(pos['x']+1,pos['y']),(pos['x'],pos['y']-1),(pos['x']+1,pos['y']-1)]
                    for x,y in cells:
                        occupied[f"{x},{y}"] = f"{unit.get('id')}:{unit.get('roleType')}"
            for zone in array(obj(raw.get("mapInfo")).get("zones")):
                zone = obj(zone)
                occupied[self._pos(zone.get('pos'))] = str(zone.get('neutralType'))
            targets = {str(actor):self._brief(','.join(occupied.get(self._pos(p),'') for p in array(cmd.get('targetPos'))[:3]),100)
                       for actor,cmd in response.get('roleCommandMap',{}).items() if cmd.get('action')=='move'}
            state.update(round=number,commands=commands,units={i:{"pos":u.get("pos")} for i,u in units.items()},targets=targets,
                         attacks={c.get("weapon"):c for c in getattr(self.local,"attacks",[])},task=task_marker)
            if periodic:
                state["stats"].clear()
                state["details"] = 0
                state["critical_reported"] = False

    def event(self, event, **data):
        # Logging must never replace a valid competition response with a failure.
        try:
            if self.mode == "compact":
                self._compact_event(event,data)
                return
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
        if self.mode == "compact":
            where = [f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in traceback.extract_tb(record.exc_info[2])[-3:]] if record.exc_info else []
            self.event("exception" if record.exc_info else "warning",message=self._brief(record.getMessage(),160),
                       error=record.exc_info[0].__name__ if record.exc_info else None,at=where)
            return
        self.event("exception" if record.exc_info else "warning", message=record.getMessage(),
                   logger=record.name, traceback="".join(traceback.format_exception(*record.exc_info))
                   if record.exc_info else None)

    def startup(self, agent):
        root = Path(__file__).resolve().parents[2]
        hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted((root / "CoreGeek").rglob("*"))
                  if p.is_file() and p.suffix in {".py", ".json"}}
        hashes["run.sh"] = hashlib.sha256((root / "run.sh").read_bytes()).hexdigest()
        if self.mode == "compact":
            self._write_compact("startup",mode="compact",every=self.interval,python=sys.version.split()[0],
                sdk=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()[:12],
                config=hashlib.sha256(repr((agent.rules,agent.policy)).encode()).hexdigest()[:12],bind="0.0.0.0")
            return
        self.event("startup", python=sys.version, pid=os.getpid(), files=hashes,
                   entrypoint=str(root / "CoreGeek/main3.py"), cwd=os.getcwd(),
                   file_manifest_sha256=hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
                   rules=agent.rules, policy=agent.policy)

    def run(self, raw, function):
        previous = self.local.__dict__.copy()
        self.local.call = uuid.uuid4().hex
        self.local.round = raw.get("roundNo") if isinstance(raw, dict) else None
        self.local.outcome = "ok"
        self.local.issues = []
        self.local.critical = False
        started = time.monotonic()
        try:
            self.event("request", request=raw)
            response = function(raw)
            if self.mode == "compact":
                try:
                    self._compact_turn(raw,response,(time.monotonic()-started)*1000)
                except Exception:
                    pass  # Diagnostics must not change a valid response.
            self.event("response", response=response, outcome=self.local.outcome,
                       elapsed_ms=round((time.monotonic()-started)*1000, 3))
            return response
        except Exception:
            self.event("uncaught_exception", traceback=traceback.format_exc())
            if self.mode == "compact":
                self.local.outcome = "uncaught_exception"
                try:
                    self._compact_turn(raw,{},(time.monotonic()-started)*1000)
                except Exception:
                    pass
            raise
        finally:
            self.local.__dict__.clear()
            self.local.__dict__.update(previous)

    def outcome(self, value):
        self.local.outcome = value
