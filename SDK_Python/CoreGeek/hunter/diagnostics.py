"""Copyable stdout summaries by default; full event capture is opt-in."""
from collections import Counter, OrderedDict
from dataclasses import asdict, is_dataclass
from types import SimpleNamespace
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
from .checker_contract import checker_paths


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
        self.max_health = {}

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
            try:structured=json.loads(answer)
            except ValueError:structured=None
            preview=llm_trace.redact(answer) if isinstance(structured,dict) else 'text'
            detail += ":" + self._brief(preview,72) + "#" + hashlib.sha256(answer.encode()).hexdigest()[:8]
        return self._brief(action + detail,120)

    def _write_compact(self, event, **data):
        record = dict(schema=2, run=self.run_id[:8], round=getattr(self.local,"round",None), event=event, **data)
        # Valid single-line JSON, never a byte slice of a serialized record.
        # Essential issue details take precedence over optional current context.
        for optional in ("reasons", "channels", "work", "commands", "units", "task", "defence", "docs", "root_entries", "entries"):
            if len(json.dumps(record,ensure_ascii=False,separators=(",", ":")).encode()) <= 1400:
                break
            if optional=='task' and isinstance(record.get('task'),str):continue
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
        # Keep current observations and actual receipts ahead of optional plan
        # commentary when a duty event coincides with an existing issue line.
        for section, key in (('gate','reason'),('gate','planned_worker'),
                             ('gate','gate_confirmed_round'),('forage','mine')):
            if len(json.dumps(record,ensure_ascii=False,separators=(",", ":")).encode()) <= 1400:
                break
            obj(obj(record.get('duty')).get(section)).pop(key,None)
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
            self.local.task_active = active
            self.local.task_evidence = active.get('evidence') or {}
            self.local.task_required = checker_paths(SimpleNamespace(
                evidence=self.local.task_evidence,statement_path=active.get('statement_path')))
            faults = [e for e in active.get("events",[]) if e.get("kind") in
                      {"quarantined_llm", "invalid_llm", "invalid_command_plan", "missing_llm", "oversized_llm", "missing_command_effect_unknown"}]
            self.local.task_fault = faults[-1] if faults else None
            self.local.llm_verdict = next((e for e in reversed(active.get('events',[]))
                if e.get('round')==getattr(self.local,'round',None) and e.get('kind') in
                {'llm_consumed','invalid_llm','quarantined_llm','invalid_command_plan','oversized_llm','missing_llm'}),{})
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
                "last": self._brief([{k:llm_trace.redact(v) if isinstance(v,str) else v for k,v in e.items()
                    if k in ('kind','round','op','status','reason')} for e in active.get('events',[])[-1:]],180),
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

    def _duty_summary(self, raw, response, decision, units):
        """Bounded observations and selected actions, never predicted receipts."""
        layout = obj(decision.get('task_side_layout'))
        roster = obj(decision.get('night_roster'))
        gate = obj(decision.get('external_gate'))
        if not layout.get('c') and not roster and not gate:
            return {}, None
        number = raw.get('roundNo')
        commands = obj(response.get('roleCommandMap'))
        point = lambda p: list(p) if isinstance(p, (list, tuple)) and len(p) == 2 else None
        duty = {}
        if layout.get('c'):
            duty['layout'] = {k:point(layout.get(k)) for k in ('c','w','gate')}
        if roster:
            duty['roster'] = {
                k:self._brief(str(roster[k]),32)+'@'+self._pos(obj(units.get(str(roster[k]))).get('pos'))
                for k in ('w','p','m') if roster.get(k) is not None}
            duty['roster'].update(defenders=[str(i)[:32] for i in roster.get('defenders',[])[:3]],
                defender_count=len(roster.get('defenders',[])),
                handoff=bool(roster.get('handoff_requested')),yielding=[str(i)[:32] for i in roster.get('yielding',[])[:3]])
            if roster.get('exit_pending'):
                duty['roster']['exit_pending'] = True
        assignment = obj(gate.get('assignment_state'))
        gate_point = point(gate.get('gate') or assignment.get('gate') or layout.get('gate'))
        if gate:
            details = {'stage':self._brief(gate.get('stage','unknown'),32)}
            if gate_point is not None:
                details['wall_observed'] = any(u.get('roleType')=='wall' and type(u.get('health')) is int
                    and u['health']>0 and [obj(u.get('pos')).get('x'),obj(u.get('pos')).get('y')]==gate_point
                    for u in units.values())
            offer = obj(gate.get('assignment'))
            worker = assignment.get('worker') or offer.get('worker') or gate.get('builder') or gate.get('opener')
            if worker is not None:
                details['planned_worker'] = str(worker)[:32]
            pending = obj(assignment.get('pending'))
            issued = obj(commands.get(str(pending.get('actor'))))
            if pending.get('round')==number and issued and issued==pending.get('command'):
                details['issued'] = [str(pending['actor'])[:32],issued.get('action')]
            else:
                for identity, command in commands.items():
                    targets = command.get('targetPos',[])
                    if command.get('action') in ('build','remove') and targets and gate_point is not None and (
                            [targets[0].get('x'),targets[0].get('y')]==gate_point):
                        details['issued'] = [str(identity)[:32],command['action']]
                        break
            receipts = assignment.get('observed',[])
            if receipts:
                receipt = receipts[-1]
                details['receipt'] = {k:receipt.get(k) for k in ('round','actor','action','confirmed','feedback')}
                details['receipt']['actor'] = str(receipt.get('actor'))[:32]
            if assignment.get('gate_confirmed_round') is not None:
                details['gate_confirmed_round'] = assignment['gate_confirmed_round']
            if gate.get('reason'):
                details['reason'] = self._brief(gate['reason'],64)
            duty['gate'] = details
        repairs = {}
        for identity, repair in obj(decision.get('repair')).items():
            if not isinstance(repair,dict) or len(repairs)>=2:
                continue
            wall = obj(units.get(str(repair.get('wall'))))
            actor = obj(units.get(str(identity)))
            bag = actor.get('backpack')
            repairs[str(identity)[:32]] = dict(phase=self._brief(repair.get('phase'),24),wall=str(repair.get('wall'))[:32],
                hp=[wall.get('health'),self.max_health.get('wall',{}).get(wall.get('level'))],
                stock=bag.count('WallFixer') if isinstance(bag,list) else None,
                remaining=repair.get('remaining_actions'),cd=repair.get('observed_cooldown'),
                selected=bool(repair.get('selected')),delayed=repair.get('delayed_fire'))
        if repairs:
            duty['repair'] = repairs
        origin = decision.get('origin')
        if type(number) is int and type(origin) is int and origin in (0,1) and (number-origin)%130>=70:
            remaining = 130-(number-origin)%130
            mining = obj(gate.get('mining'));cashout=obj(gate.get('cashout'));purchase=obj(gate.get('purchase'))
            trade = cashout if cashout.get('required') is not None else purchase
            action = (obj(commands.get(str(roster.get('m')))).get('action')
                      if roster.get('m') not in roster.get('defenders',()) else None)
            duty['forage'] = dict(dawn_round=number+remaining if (number-origin)//130+1<10 else None,
                                  night_end_round=number+remaining-1,remaining=remaining,
                                  issued=action if action in ('move','collect','sell','buy','remove') else None)
            if trade.get('required') is not None:
                duty['forage']['planned_checkout_actions'] = trade['required']
            if mining.get('mine'):
                duty['forage']['mine'] = point(mining['mine'])
        # Progress coordinates/countdowns do not consume a new change event on
        # every move. They are retained in the next periodic observed snapshot.
        marker = (tuple(tuple(layout.get(k) or ()) for k in ('c','w','gate')),
            tuple(roster.get('defenders',())),bool(roster.get('handoff_requested')),
            tuple(roster.get('yielding',())),bool(roster.get('exit_pending')),
            gate.get('stage'),obj(duty.get('gate')).get('wall_observed'),
            assignment.get('gate_confirmed_round'),
            tuple((i,r['phase'],r['wall'],r['selected']) for i,r in repairs.items()),
            obj(duty.get('forage')).get('issued'))
        return duty, marker

    def _notice(self,state,identity,category,signature):
        """Six novel diagnostic transitions per category/task, then count only."""
        seen=state.setdefault('task_notices',OrderedDict())
        key=(identity,category,str(signature))
        if key in seen or sum(k[:2]==key[:2] for k in seen)>=6:
            state['stats']['task_diag_repeated_or_omitted']+=1
            return False
        seen[key]=True
        while len(seen)>192:seen.popitem(last=False)
        return True

    def _task_context(self,state,task):
        seen=state.setdefault('task_context_seen',OrderedDict())
        for document in llm_trace.document_context(getattr(self.local,'task_evidence',{})):
            key=(document['path'],document['hash'])
            if key in seen:continue
            seen[key]=True
            self._write_compact('task_context',**llm_trace.fit(dict(task=task.get('id'),diag=2,
                document=document,required_checkers=getattr(self.local,'task_required',[]))))
        while len(seen)>32:seen.popitem(last=False)

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
                verdict=getattr(self.local,'llm_verdict',{})
                reason=verdict.get('reason','') if not ended else ''
                next_steps=[k for k in ('prompt','executeCmd') if response.get(k)]
                if any(c.get('action')=='submitAnswer' for c in response.get('roleCommandMap',{}).values()):
                    next_steps.append('submitAnswer')
                important=bool(reason) or output.get('intent')=='answer' or (
                    verdict.get('kind') and verdict['kind']!='llm_consumed') or not reply or (
                    output.get('intent')=='execute' and not response.get('executeCmd'))
                signature=(verdict.get('kind'),llm_trace.rejection_stage(reason),reason,output.get('intent'),next_steps)
                extra=important and self._notice(state,identity,'llm',signature)
                if counts[identity]<3 or extra:
                    record={'task':identity,'rid':pending['input'].get('rid'),'sent_round':pending['round'],
                            'wait_rounds':number-pending['round'],'left':task.get('left'),'next':next_steps,
                            'verdict':('task_changed' if ended else verdict.get('kind','received') if reply else 'missing'),
                            'input':{k:v for k,v in pending['input'].items() if not k.startswith('_') and k!='rid'},
                            'output':output}
                    if reason:
                        record['reason']=llm_trace.redact(reason)[:240]
                        record['stage']=llm_trace.rejection_stage(reason)
                    if important and not ended:
                        record['diagnostic']=llm_trace.rejection_detail(llm_trace.decision(reply),
                            getattr(self.local,'task_evidence',{}),getattr(self.local,'task_required',[]))
                        record['input']={k:v for k,v in record['input'].items() if k in ('left','prompt_chars','prompt_hash','allowed','template_intent')}
                        record['output']={k:v for k,v in output.items() if k in ('parse','rid','version','intent','op','cwd')}
                        self._task_context(state,task)
                        if reason or not reply:
                            state['last_task_diagnostic']={'stage':record.get('stage'),'reason':record.get('reason'),
                                                          'round':number,'rid':record['rid']}
                    # Keep valid one-line JSON and a strict extra-line byte budget.
                    for group,key in [('input','docs'),('output','imports'),('output','endpoints'),('output','auth'),('output','launch')]:
                        if len(json.dumps(record,ensure_ascii=False).encode())<=1000:break
                        record[group].pop(key,None);record['cut']=True
                    if len(json.dumps(record,ensure_ascii=False).encode())>1100:
                        record['output']={k:v for k,v in output.items() if k in {'reply_hash','reply_chars','parse','code_hash','intent','op'}}
                        record['cut']=True
                    self._write_compact('llm',**llm_trace.fit(record));counts[identity]+=1;logged=True
                else:state['stats']['llm_trace_omitted']+=1
                state['llm_trace_pending']=None
        if response.get('prompt') and task.get('id'):
            state['llm_trace_pending']={'task':task['id'],'round':number,'input':llm_trace.sent(response['prompt'])}
        while len(counts)>32:counts.popitem(last=False)
        return logged

    def _task_lifecycle(self,state,raw,response,task,number):
        previous=state.get('task_totals')
        identity=task.get('id')
        if previous and previous['task']!=identity:
            self._write_compact('task_end',**llm_trace.fit({**previous,
                'end':task.get('end') or 'task_changed', 'last_diagnostic':state.get('last_task_diagnostic'),
                'judge_errors':[{k:e.get(k) for k in ('errorCode','description')}
                    for e in array(raw.get('errors'))[:2] if isinstance(e,dict)]}))
            state['task_totals']=None;state['last_task_diagnostic']=None
        if not identity:return
        totals=state.get('task_totals')
        if totals is None:
            active=getattr(self.local,'task_active',{})
            totals=state['task_totals']={'task':identity,'start':number,'accept':active.get('accept_round'),
                'timeout':active.get('timeout'),'llm_calls':0,'cmd_calls':0,'submitted':0}
        totals['file']=task.get('file') or totals.get('file')
        totals['llm_calls']+=bool(response.get('prompt'))
        totals['cmd_calls']+=bool(response.get('executeCmd'))
        totals['submitted']=task.get('submitted',0)
        for command in response.get('roleCommandMap',{}).values():
            if command.get('action')=='submitAnswer':
                answer=command.get('taskAnswer','')
                try:value=json.loads(answer)
                except ValueError:value=answer
                self._write_compact('task_submit',**llm_trace.fit(dict(task=identity,left=task.get('left'),
                    answer_hash=llm_trace.digest(answer),fields=list(value)[:8] if isinstance(value,dict) else [],
                    token_hash=llm_trace.digest(value['token']) if isinstance(value,dict) and isinstance(value.get('token'),str) else None,
                    answer_type=type(value).__name__)))

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
            self._task_lifecycle(state,raw,response,task,number)
            llm_logged = self._llm_turn(state,raw,response,task,number)
            tool = getattr(self.local,"task_tool",None)
            tool_key = (task.get("id"),(tool or {}).get("round"))
            if tool and task.get("id") and tool_key not in state["task_tools"]:
                evidence=getattr(self.local,'task_evidence',{})
                eid,record=next(((k,e) for k,e in reversed(list(evidence.items()))
                    if e.get('round')==tool.get('round') and e.get('data',{}).get('operation')==tool.get('op')),('',{}))
                shape=llm_trace.result_shape(record.get('data',{}))
                runtime=[e for e in tool.get('runtime',[]) if isinstance(e,dict) and e.get('kind')!='checker_exits']
                failed=lambda e:bool(e.get('error') or isinstance(e.get('status'),int) and e['status']>=400)
                runtime.sort(key=lambda e:not failed(e))  # A late failed page survives the two-entry display cap.
                important=bool(tool.get('failure') or tool.get('status')!='ok' or shape['token_n'] or shape['checker_exits']
                               or any(failed(e) for e in runtime))
                signature=json.dumps([tool.get('status'),tool.get('failure'),shape,
                    [e for e in runtime if e.get('kind')=='http']],sort_keys=True)
                extra=important and self._notice(state,task['id'],'execution',signature)
                if sum(k[0]==task["id"] for k in state["task_tools"]) < 2 or extra:
                    self._write_compact("task_exec",**llm_trace.fit(dict(task=task["id"],left=task.get("left"),
                        rid=state.get('program_rids',{}).get(str(tool.get('program') or '')[:12]),
                        op=tool.get("op"),path=self._brief(tool.get("path"),100),status=tool.get("status"),
                        exit=tool.get("exit"),usable=tool.get("usable"),answer_usable=tool.get('answer_usable'),
                        failure=tool.get('failure'),cwd=tool.get('cwd'),evidence=eid,shape=shape,
                        adapters=tool.get('adapters',[]),
                        auth=tool.get('contract',{}).get('auth'),
                        runtime=runtime[:2],
                        program=str(tool.get('program') or '')[:12],
                        result_hash=llm_trace.digest(str(record.get('data',{}).get('text',tool.get('result','')))),
                        result_round=tool.get("round"))))
                    if important:self._task_context(state,task)
                else:state['stats']['task_exec_omitted']+=1
                state["task_tools"].add(tool_key)
                if len(state["task_tools"]) > 32:state["task_tools"]={tool_key}
            fault = getattr(self.local,"task_fault",None)
            fault_key = (task.get("id"),(fault or {}).get("kind"),(fault or {}).get('reason',''))
            if fault and llm_logged and fault.get('round')==number:
                state['task_faults'].add(fault_key)
            if fault and fault_key not in state["task_faults"] and not (llm_logged and fault.get("round")==number):
                # Different validation reasons retain separate slots, even if
                # the state machine calls both of them invalid_llm.
                if self._notice(state,task.get('id'),'fault',fault_key[1:]):
                    detail=llm_trace.rejection_detail(llm_trace.decision(raw.get('llmResp','')),
                        getattr(self.local,'task_evidence',{}),getattr(self.local,'task_required',[]))
                    self._write_compact("task_fault",**llm_trace.fit(dict(task=task.get("id"),left=task.get("left"),
                        kind=fault.get("kind"),reason=llm_trace.redact(fault.get("reason",""))[:240],
                        stage=llm_trace.rejection_stage(fault.get('reason','')),diagnostic=detail,
                        op=fault.get("op"),path=self._brief(fault.get("path"),100),
                        expected=fault.get("expected"),received=self._brief(fault.get("received",""),120),
                        fault_round=fault.get("round"))))
                    self._task_context(state,task)
                    state['last_task_diagnostic']={'stage':llm_trace.rejection_stage(fault.get('reason','')),
                        'reason':llm_trace.redact(fault.get('reason',''))[:240],'round':fault.get('round')}
                state["task_faults"].add(fault_key)
                if len(state["task_faults"]) > 32:
                    state["task_faults"] = {fault_key}
            for failure in getattr(self.local,'task_active',{}).get('diagnostic_events',[]) or []:
                if failure.get('round')==number and self._notice(state,task.get('id'),'auto_answer',failure.get('reason')):
                    self._write_compact('task_fault',**llm_trace.fit(dict(task=task.get('id'),left=task.get('left'),
                        kind=failure['kind'],reason=failure['reason'],stage=llm_trace.rejection_stage(failure['reason']),
                        diagnostic=llm_trace.rejection_detail(failure,getattr(self.local,'task_evidence',{}),
                            getattr(self.local,'task_required',[])))))
                    self._task_context(state,task)
                    state['last_task_diagnostic']={'stage':llm_trace.rejection_stage(failure['reason']),
                        'reason':llm_trace.redact(failure['reason'])[:240],'round':number}
            task_marker = (task.get("id"),task.get("phase"),task.get("file"),task.get("read"),task.get("end"))
            task_changed = task_marker != state["task"] and bool(task.get("id") or task.get("end"))
            decision = getattr(self.local,'decision',{})
            duty, duty_marker = self._duty_summary(raw,response,decision,units)
            duty_changed = duty_marker is not None and duty_marker != state.get('duty_marker')
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
            trigger = new_failure or fresh_issue or task_changed or duty_changed
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
                channels = {k:{'chars':len(raw[k]),'hash':llm_trace.digest(raw[k])}
                            for k in ('lastCmdResult','llmResp') if isinstance(raw.get(k),str) and raw[k]} if issues else {}
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
                    **({'duty':duty} if duty else {}),
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
                         attacks={c.get("weapon"):c for c in getattr(self.local,"attacks",[])},task=task_marker,
                         duty_marker=duty_marker)
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
        self.max_health = {kind:dict(levels) for kind,levels in agent.rules.max_health.items()}
        root = Path(__file__).resolve().parents[2]
        hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted((root / "CoreGeek").rglob("*"))
                  if p.is_file() and p.suffix in {".py", ".json"}}
        hashes["run.sh"] = hashlib.sha256((root / "run.sh").read_bytes()).hexdigest()
        if self.mode == "compact":
            self._write_compact("startup",mode="compact",every=self.interval,task_diag=2,python=sys.version.split()[0],
                sdk=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()[:12],
                config=hashlib.sha256(repr((agent.rules,agent.policy)).encode()).hexdigest()[:12],bind="0.0.0.0",
                strategy={'staged_walls':agent.policy.staged_walls_enabled,
                          'economy_first':agent.policy.economy_first_enabled})
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
