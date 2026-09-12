"""Observed two-inside/one-outside gate cycle; no predicted positions or stock.

This opt-in cycle keeps W/P at their actual gun stands, seals from outside,
and uses only observed mineral routes. Failed seals abort that night's release.
Night return waits outside; this module never rebuilds or removes a night gate.
"""
from dataclasses import dataclass, field
import time

from .arbitration import Candidate
from .navigation import neighbours
from .robot_threats import active as active_threats
from .protocol import distance, pos_json
from .rules import station_rings
from .task_side_layout import _field, BudgetExpired


def valid_seal(world, builds):
    """Recheck the narrow exception from current facts, not merely a flag."""
    permit = getattr(world, 'external_gate_permit', None)
    if not permit or len(builds) != 1:
        return False
    actor, kind, target = builds[0]
    if (actor, kind, target) != (permit.get('builder', permit['m']), 'wall', permit['gate']):
        return False
    blue,yellow = station_rings(world.stations[0].pos)
    walls = {u.pos for u in world.ours.values() if u.alive and u.kind=='wall'}
    m,w,p = (world.ours.get(permit[k]) for k in ('m','w','p'))
    plan = getattr(world,'task_side_plan',None)
    return bool(plan and m and all(u and u.alive for u in (w,p))
        and len(world.movers)==(3 if m.alive else 2) and len({m.id,w.id,p.id})==3
        and m.kind=='worker' and w.kind=='worker' and p.kind=='pioneer'
        and not world.phase_task
        and (not m.alive or m.pos not in blue|yellow|world.stations[0].cells)
        and ((actor == m.id and m.alive and w.pos==plan['w'] and p.pos in plan['c_stands']
              and distance(m.pos,target)==1 and m.inventory['stone']>=1)
             or (actor == w.id and permit.get('inner') and w.pos in blue and p.pos in blue
                 and distance(w.pos,target)==1 and w.inventory['stone']>=1))
        and target not in world.occupied
        and not any(u.alive and u.pos in blue|world.stations[0].cells for u in world.robots.values())
        and walls==yellow-{target} and {u.pos for u in world.weapons}=={q for _,q in plan['slots']})


@dataclass
class ExternalGate:
    day: int | None = None
    stage: str = 'INACTIVE'
    m: str | None = None
    w: str | None = None
    p: str | None = None
    damage_upper: int | None = None
    aborted_day: int | None = None
    last_action: tuple | None = None
    return_committed: bool = False
    inner_backup: bool = False
    cashout_committed: bool = False
    cashout_report: dict = field(default_factory=dict)
    mining_report: dict = field(default_factory=dict)
    purchase_report: dict = field(default_factory=dict)
    purchase_pending: dict | None = None
    purchase_receipt: dict = field(default_factory=dict)
    emergency_active: bool = False
    emergency_reason: str = ""
    commands: dict = field(default_factory=dict)
    firearms: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)
    deferred_night: bool = False
    gate_assignment: dict = field(default_factory=dict)
    gate_offer: dict = field(default_factory=dict)
    gate_before: dict = field(default_factory=dict)

    def permit(self, candidate):
        command=candidate.command
        identity=command.get('controllerId') if command.get('action')=='attack' else candidate.actor
        if identity not in self.commands:
            return None
        if command.get('action')=='attack':
            return candidate.actor in self.firearms.get(identity,())
        if command.get('action')=='use' and command.get('name') in ('Medicine','Bomb','DizzyWeapon'):
            return True
        if identity in self.firearms and command.get('action')=='use':
            return True  # Current-position legality, stock and thresholds remain in existing planners.
        if identity==self.p and identity in self.firearms and command.get('action') in ('submitAnswer','acceptTask'):
            return True  # Real task eligibility and the service bundle still apply.
        return command in self.commands[identity]

    def prepare(self, world, clock, rules, policy, deadline, task_busy=False, *, defer_regular_night=False):
        world.night_economy_active=False
        self.commands={};self.firearms={};self.diagnostic={'stage':self.stage};self.cashout_report={};self.mining_report={};self.purchase_report={}
        self.deferred_night=False
        self.gate_offer={}
        self._observe_gate_assignment(world)
        from copy import deepcopy
        self.gate_before={name:deepcopy(getattr(self,name)) for name in
                          ('stage','day','inner_backup','m','w','p')}
        self.gate_before['traffic']=deepcopy(world.night_roster.traffic)
        self.gate_before['yielding']=set(world.roster_yielding)
        world.external_gate_permit=None
        world.gate_worker_duty=None
        world.night_forage_commands={}
        world.forage_contract=None
        self.purchase_receipt={}
        pending=self.purchase_pending
        if pending and world.round>pending['round']:
            actor=world.ours.get(pending['actor'])
            feedback=world.raw.get('lastRoundRoleActionResults',{})
            if actor and actor.backpack is not None and actor.inventory[pending['name']]>pending['count_before']:
                self.purchase_receipt={'status':'personal voucher observed','name':pending['name'],'actor':pending['actor']}
                self.purchase_pending=None
            elif world.round==pending['round']+1 and isinstance(feedback,dict) and feedback.get(pending['actor']) is False:
                self.purchase_receipt={'status':'purchase explicitly failed','name':pending['name'],'actor':pending['actor']}
                self.purchase_pending=None
            else:
                self.purchase_receipt={'status':'awaiting personal purchase receipt','name':pending['name'],'actor':pending['actor']}
        world.pending_night_purchase=self.purchase_pending
        observed=[u.attack_power for u in world.robots.values() if u.alive and u.attack_power is not None]
        if observed:self.damage_upper=max(observed+[self.damage_upper or 0])
        plan=getattr(world,'task_side_plan',None)
        if not policy.external_gate_enabled or not plan or clock.day is None:
            return []
        return self._cycle(world,clock,rules,policy,deadline,task_busy,plan,
                           defer_regular_night=defer_regular_night)

    def resume_night(self, world, clock, rules, policy, deadline, task=None, task_busy=False):
        world.night_economy_active=False
        if not self.deferred_night:
            return []
        self.deferred_night=False
        self.commands={};self.firearms={}
        world.forage_service_checked=True
        world.forage_task=task
        return self._cycle(world,clock,rules,policy,deadline,task_busy,world.task_side_plan,
                           check_emergency=False)

    def _observe_gate_assignment(self, world):
        assignment=self.gate_assignment
        if not assignment:return
        pending=assignment.get('pending')
        if pending and world.round>pending['round']:
            actor=world.ours.get(pending['actor']);command=pending['command']
            point=tuple(command['targetPos'][0][k] for k in ('x','y'))
            wall=next((u for u in world.ours.values() if u.alive and u.kind=='wall' and u.pos==point),None)
            confirmed=bool(actor and actor.alive and actor.pos==point) if command['action']=='move' else (
                wall is not None if command['action']=='build' else wall is None)
            feedback=world.raw.get('lastRoundRoleActionResults',{})
            receipt=dict(round=world.round,actor=pending['actor'],action=command['action'],
                         confirmed=confirmed,feedback=feedback.get(pending['actor']) if isinstance(feedback,dict) else None)
            assignment['observed'].append(receipt)
            assignment['observed']=assignment['observed'][-40:]
            assignment['confirmed_actions']+=int(confirmed)
            assignment['pending']=None
        wall=next((u for u in world.ours.values() if u.alive and u.kind=='wall' and u.pos==assignment['gate']),None)
        if (wall is not None)==(assignment['kind']=='seal'):
            assignment.setdefault('gate_confirmed_round',world.round)
            if assignment.get('worker')==self.m:assignment['completed_observed']=True

    def _gate_options(self, world, plan, deadline, kind, policy, task_busy=False):
        """At most M direct, W direct and one real P yield; costs are previews."""
        from copy import copy
        from .night_roles import clear_c_access
        roster=world.night_roster
        m,w,p=(world.ours.get(i) for i in (roster.m,roster.w,roster.p))
        blue,yellow=station_rings(plan['anchor']);gate=plan['gate']
        walls={u.pos for u in world.ours.values() if u.alive and u.kind=='wall'}
        sealing=kind=='seal'
        if sealing and (task_busy or world.phase_task or not m or not w or not p
                or not w.alive or not p.alive or w.pos not in blue or p.pos not in blue
                or m.alive and m.pos in blue|yellow|world.stations[0].cells
                or walls!=yellow-{gate} or {u.pos for u in world.weapons}!={q for _,q in plan['slots']}
                or any(u.alive and u.pos in blue|world.stations[0].cells for u in world.robots.values())):
            return []
        rows=[]
        economic_field={}
        if m and m.alive:
            loaded=any(m.inventory[name]>(1 if name=='stone' and sealing else 0) and world.vendor.get(name,0)>0
                       for name in ('stone','iron','copper'))
            economic_targets=set(world.zones.get('vendor',())) if loaded else set()
            if not economic_targets:
                economic_targets={point for name in ('stone','iron','copper') if world.vendor.get(name,0)>0
                                  for point in world.zones.get(name,())}
            economic_blocked=(world.occupied|world.navigation_avoided.get(m.pos,set()))-{m.pos}
            if sealing:economic_blocked.add(gate)
            else:economic_blocked.discard(gate)
            economic_goals={q for point in economic_targets for q in neighbours(point)}-economic_blocked
            if economic_goals:economic_field=_field(world,economic_goals,economic_blocked,deadline)
        for label,actor in (('m',m),('w',w)):
            if not actor or not actor.alive or actor.kind!='worker':continue
            if sealing and (actor.backpack is None or not actor.inventory['stone']):continue
            if sealing and label=='m' and (w.pos!=plan['w'] or p.pos not in plan['c_stands']
                    or self.damage_upper is None or actor.health<165 or actor.health<=2*self.damage_upper
                    or w.health<165 or p.health<150 or min(w.health,p.health)<=2*self.damage_upper):continue
            blocked=(world.occupied|world.navigation_avoided.get(actor.pos,set()))-{actor.pos}
            # W's ordinary gate duty stays inside; it never replaces M outside.
            if label=='w':
                blocked|={(x,y) for x in range(world.width) for y in range(world.height) if (x,y) not in blue}
            threats=active_threats(world)
            if any(u.attack_power is None or u.attack_range is None for u in threats):continue
            for robot in threats:
                for x in range(max(0,robot.pos[0]-robot.attack_range),min(world.width,robot.pos[0]+robot.attack_range+1)):
                    if time.monotonic()>=deadline:raise BudgetExpired
                    for y in range(max(0,robot.pos[1]-robot.attack_range),min(world.height,robot.pos[1]+robot.attack_range+1)):
                        q=(x,y)
                        if 2*sum(r.attack_power for r in threats if distance(q,r.pos)<=r.attack_range)>=actor.health:blocked.add(q)
            blocked.discard(actor.pos)
            goals={q for q in neighbours(gate) if world.inside(q) and q not in blocked}
            if sealing and label=='m':goals-=blue|yellow|world.stations[0].cells
            route=_field(world,goals,blocked,deadline)
            yield_stand=None;traffic={};yield_actions=0
            if actor.pos not in route and label=='w' and p and p.alive and not task_busy:
                preview=copy(world);preview.night_roster=copy(roster);preview.roster_yielding=set(world.roster_yielding)
                yielding=clear_c_access(preview,actor,set(neighbours(gate))&blue,p.id,deadline)
                if yielding:
                    yield_stand=yielding[p.id];yield_actions=1
                    traffic=preview.night_roster.traffic
                    blocked=(blocked-{p.pos})|{yield_stand}
                    goals=set(neighbours(gate))&blue-blocked
                    route=_field(world,goals,blocked,deadline)
            if actor.pos not in route:continue
            reach=_field(world,{actor.pos},blocked,deadline)
            reachable=goals&reach.keys()
            if not reachable:continue
            if label=='w':
                home=_field(world,{plan['w']},blocked,deadline)
                reachable &= home.keys()
                if not reachable:continue
                endpoint=min(reachable,key=lambda q:(reach[q]+home[q],reach[q],q))
                return_actions=home[endpoint]
            else:
                endpoint=min(reachable,key=lambda q:(reach[q],q));return_actions=0
            p_return=0
            p_start=yield_stand or (p.pos if p and p.alive else None)
            if label=='w' and p_start is not None and p_start not in plan['c_stands']:
                restored=(world.occupied-{w.pos,p.pos})|{plan['w']}
                p_home=_field(world,set(plan['c_stands'])-restored,restored,deadline,blue)
                if p_start not in p_home:continue
                p_return=p_home[p_start]
            opening=yield_actions+reach[endpoint]+1
            total=opening+return_actions+p_return
            economic_extra=(reach[endpoint]+economic_field[endpoint]-economic_field[m.pos]
                            if label=='m' and m.pos in economic_field and endpoint in economic_field else
                            0 if label=='w' else None)
            door_and_economy=(total-reach[endpoint]+economic_extra if label=='m' and economic_extra is not None else
                              total if label=='w' else None)
            if sealing and total+2+policy.return_buffer>=getattr(world,'_gate_daylight',float('inf')):continue
            task_actions=None
            if not sealing and p_start is not None:
                task_goals={q for t in world.tasks if t.get('isValid') is True
                            for cell in world.task_cells(t) for q in neighbours(cell)}
                if task_goals:
                    occupied=(world.occupied-{gate,p.pos,actor.pos})|({plan['w']} if label=='w' else {endpoint})
                    task_route=_field(world,task_goals-occupied,occupied,deadline)
                    if p_start in task_route:task_actions=opening+return_actions+task_route[p_start]
            command=({'action':'move','targetPos':[pos_json(yield_stand)]} if yield_stand else
                     self._move(actor,_field(world,{endpoint},blocked,deadline)) if reach[endpoint] else
                     {'action':'build','name':'wall','targetPos':[pos_json(gate)]} if sealing else
                     {'action':'remove','targetPos':[pos_json(gate)]})
            rows.append(dict(kind=kind,label=label,worker=actor.id,gate=gate,endpoint=endpoint,
                             actor=p.id if yield_stand else actor.id,command=command,traffic=traffic,
                             planned=dict(opening_actions=opening,yield_actions=yield_actions,
                                          worker_travel=reach[endpoint],worker_return=return_actions,
                                          pioneer_return=p_return,total_actions=total,p_task_actions=task_actions,
                                          m_economic_extra_actions=economic_extra,door_and_economic_actions=door_and_economy,
                                          m_hold_actions=total if sealing and label=='w' else 0,
                                          p_hold_actions=opening+return_actions-yield_actions
                                              if label=='w' and not world.phase_task else 0)))
        if time.monotonic()>=deadline:raise BudgetExpired
        return rows

    @staticmethod
    def _choose_gate(rows, mode, current=None):
        eligible=[row for row in rows if mode=='auto' or row['label']==mode]
        if current:
            same=[row for row in eligible if row['worker']==current]
            if same:return same[0]
        if not eligible:return None
        def duty_cost(row):
            value=row['planned']['door_and_economic_actions']
            return row['planned']['total_actions'] if value is None else value
        return min(eligible,key=lambda row:(
            (row['planned']['p_task_actions'] if row['planned']['p_task_actions'] is not None
             else row['planned']['opening_actions']) if row['kind']=='dawn' else duty_cost(row),
            duty_cost(row),row['label']!='m',row['worker']))

    def _offer_gate(self, world, row, rows, candidates, mode):
        if not candidates:return candidates
        self.gate_offer=dict(row=row,mode=mode,alternatives=[dict(worker=r['worker'],label=r['label'],planned=r['planned']) for r in rows],
                             commands=[(c.actor,c.command) for c in candidates],
                             held=[identity for identity in (self.m,self.p) if identity in self.commands and not self.commands[identity]])
        if world.night_roster.traffic and world.night_roster.traffic.get('traveller')==self.w:
            world.night_roster.traffic['gate_owned']=True
        self.diagnostic['assignment']=dict(worker=row['worker'],kind=row['kind'],mode=mode,
                                           planned=row['planned'],selected_observed=False,
                                           alternatives=self.gate_offer['alternatives'])
        return candidates

    def _clear_gate_traffic(self, world):
        traffic=world.night_roster.traffic
        if traffic.get('gate_owned'):
            world.roster_yielding.discard(traffic['blocker']);world.night_roster.traffic={}

    def _gate_return_tail(self, world, plan, deadline):
        """After observed wall change, restore actual W/P without recalling M."""
        w,p=(world.ours.get(i) for i in (self.w,self.p));blue,_=station_rings(plan['anchor'])
        self.commands={self.m:[]} if self.gate_assignment.get('kind')=='seal' else {}
        if not w or not w.alive:
            self._clear_gate_traffic(world)
            return [],False,'worker unavailable'
        if w.pos!=plan['w']:
            blocked=(world.occupied|world.navigation_avoided.get(w.pos,set()))-{w.pos}
            home=_field(world,{plan['w']},blocked,deadline,blue)
            if w.pos not in home:
                self.commands[w.id]=[]
                if p and p.alive and not world.phase_task:self.commands[p.id]=[]
                return [],False,'return route blocked'
            command=self._move(w,home)
            self.commands[w.id]=[command] if command else []
            if p and p.alive and not world.phase_task:self.commands[p.id]=[]
            return ([Candidate(w.id,command,1000,'return actual gate worker to common stand')] if command else []),False,'return worker'
        self._clear_gate_traffic(world)
        if p and p.alive and p.pos not in plan['c_stands'] and not world.phase_task:
            blocked=(world.occupied|world.navigation_avoided.get(p.pos,set()))-{p.pos}
            home=_field(world,set(plan['c_stands'])-blocked,blocked,deadline,blue)
            if p.pos not in home:return [],False,'pioneer return route blocked'
            command=self._move(p,home)
            self.commands[p.id]=[command] if command else []
            self.commands[w.id]=[]
            return ([Candidate(p.id,command,1000,'restore actual third-gun stand after gate duty')] if command else []),False,'return pioneer'
        return [],bool(not p or not p.alive or p.pos in plan['c_stands'] or world.phase_task),'returned'

    def _cycle(self, world, clock, rules, policy, deadline, task_busy, plan, *,
               defer_regular_night=False, check_emergency=True):
        blue,yellow=station_rings(plan['anchor']);gate=plan['gate']
        walls={u.pos for u in world.ours.values() if u.alive and u.kind=='wall'}
        complete={u.pos:u for u in world.weapons}
        active=self.emergency_active or self.inner_backup or self.stage in ('DUSK_MOVE','SEAL_PENDING','SEALED','NIGHT_FORAGE','NIGHT_CASHOUT','NIGHT_PURCHASE','NIGHT_WAIT_SERVICE','RETURN_TO_GATE','RETURN_BLOCKED','DAWN_OPEN','DAWN_BACKUP')
        fully_armed=set(complete)=={q for _,q in plan['slots']}
        open_exit=None
        roster=world.night_roster
        economic_worker=world.ours.get(roster.m)
        # An already exterior M can work through an observed open yellow exit
        # on the first night, without a synthetic prior full-ring seal event.
        if (clock.phases=={'night'} and policy.night_foraging_enabled and fully_armed
                and economic_worker and economic_worker.alive
                and economic_worker.pos not in blue|yellow|world.stations[0].cells
                and economic_worker.id not in world.night_defenders):
            openings=[q for q in yellow-world.occupied
                      if any(p in blue and p not in world.occupied for p in neighbours(q))
                      and any(world.inside(p) and p not in blue|yellow|world.stations[0].cells
                              and p not in world.occupied for p in neighbours(q))]
            if openings:
                open_exit=min(openings,key=lambda q:(distance(economic_worker.pos,q),q!=gate,q))
                gate=open_exit
        previous_seal=world.seal_cells
        try:
            if clock.phases == {'day'}:
                self.emergency_active=False
                self.emergency_reason=''
            elif clock.phases == {'night'} and policy.emergency_gate_enabled and check_emergency:
                emergency=self._emergency_open(world,clock,plan,deadline)
                if emergency is not None:return emergency
            if clock.phases == {'night'} and defer_regular_night:
                # Ordinary night decisions need this frame's task and repair
                # candidates. Do not consume a return commitment prematurely.
                self.deferred_night=True
                self.commands={world.night_roster.m:[]} if world.night_roster.m else {}
                return []
            opening = self.stage in ('DAWN_OPEN', 'DAWN_BACKUP', 'PROTECTED_GATE') or (
                self.gate_assignment.get('kind')=='dawn' and not self.gate_assignment.get('completed_observed'))
            next_day = self.day is not None and clock.day > self.day
            if clock.phases == {'day'} and next_day and gate not in walls and not opening:
                # An open-exit economy night has no dawn removal to issue.
                # End its reservation and continue this actual daytime frame.
                self.day=None;self.stage='INACTIVE';self.return_committed=False;self.cashout_committed=False
                active=False;next_day=False
            observed_day_gate = (self.day is None and gate in walls and clock.until_night > 18
                                 and (clock.day>=2 or walls==yellow))
            if clock.phases == {'day'} and (opening or next_day or observed_day_gate):
                # A cold session can recover access from current geometry;
                # missing guns do not make a daytime door inaccessible.
                roster = world.night_roster
                self.w = self.w or roster.w
                self.m = self.m or roster.m
                self.p = self.p or roster.p
                return self._dawn_open(world, plan, deadline, policy, task_busy)
            roster = world.night_roster
            worker, miner = world.ours.get(roster.w), world.ours.get(roster.m)
            seal_row=None;seal_rows=[]
            if self.inner_backup:
                candidates=self._inner_seal(world, clock, policy, plan, deadline, task_busy)
                row=self.gate_assignment.get('row')
                return self._offer_gate(world,row,[row],candidates,policy.gate_seal_choice) if row else candidates
            if (clock.phases == {'day'} and clock.day>=2 and clock.until_night<=18
                    and walls==yellow-{gate} and (not active or self.aborted_day==clock.day)):
                world._gate_daylight=clock.until_night
                seal_rows=self._gate_options(world,plan,deadline,'seal',policy,task_busy)
                if self.aborted_day==clock.day:seal_rows=[row for row in seal_rows if row['label']!='m']
                seal_row=self._choose_gate(seal_rows,policy.gate_seal_choice)
                if seal_row and seal_row['label']=='w':
                    self.w,self.m,self.p=roster.w,roster.m,roster.p
                    candidates=self._inner_seal(world,clock,policy,plan,deadline,task_busy)
                    return self._offer_gate(world,seal_row,seal_rows,candidates,policy.gate_seal_choice)
                if not seal_row and policy.gate_seal_choice!='auto':
                    self.commands={i:[] for i in (roster.w,roster.m) if i}
                    self.stage='GATE_CHOICE_UNAVAILABLE'
                    self.diagnostic=dict(stage=self.stage,mode=policy.gate_seal_choice,gate=gate,
                                         reason='requested worker has no complete legal seal plan')
                    return []
            if not fully_armed and not active:
                self.stage='DEFENCE_UNAVAILABLE'
                return []
            if open_exit is not None:
                self.w,self.m,self.p=roster.w,roster.m,roster.p
                if self.day!=clock.day:self.return_committed=False;self.cashout_committed=False
                self.day=clock.day
            elif not active:
                if (clock.phases!={'day'} or clock.day<2 or clock.until_night>18
                        or self.aborted_day==clock.day or task_busy or self.damage_upper is None
                        or walls!=yellow-{gate}):
                    return []
                workers=[u for u in world.movers if u.kind=='worker']
                pioneers=[u for u in world.movers if u.kind=='pioneer']
                guards=[u for u in workers if u.pos==plan['w']]
                if len(workers)!=2 or len(pioneers)!=1 or len(guards)!=1 or pioneers[0].pos not in plan['c_stands']:
                    return []
                self.w=guards[0].id;self.p=pioneers[0].id
                m=next(u for u in workers if u.id!=self.w)
                if (m.backpack is None
                        or m.health<165 or m.health<=2*self.damage_upper or not m.inventory['stone']):
                    return []
                self.m=m.id;self.day=clock.day;self.return_committed=False;self.cashout_committed=False
            m=world.ours.get(self.m);w=world.ours.get(self.w);p=world.ours.get(self.p)
            if not m or not m.alive:
                self.stage='ROLE_UNAVAILABLE'
                return []
            blocked=(world.occupied|world.navigation_avoided.get(m.pos,set()))-{m.pos}
            threats=active_threats(world)
            risk_known=all(u.attack_power is not None and u.attack_range is not None for u in threats)
            # Reject currently exposed routes using an explicit two-opportunity
            # upper scenario, not a prediction of a robot's chosen target.
            danger={}
            for robot in threats:
                if robot.attack_range is None:continue
                radius=robot.attack_range
                for x in range(max(0,robot.pos[0]-radius),min(world.width,robot.pos[0]+radius+1)):
                    if time.monotonic()>=deadline:raise BudgetExpired
                    for y in range(max(0,robot.pos[1]-radius),min(world.height,robot.pos[1]+radius+1)):
                        q=(x,y)
                        danger[q]=danger.get(q,0)+(robot.attack_power if robot.attack_power is not None else m.health)
            blocked.update(q for q,h in danger.items() if h>0)
            night_boundary=blue|yellow|world.stations[0].cells if open_exit is not None else frozenset()
            blocked.update(night_boundary)
            blocked.discard(m.pos)
            outside={q for q in neighbours(gate) if world.inside(q) and q not in blue|yellow|world.stations[0].cells and q not in blocked}
            home=_field(world,outside,blocked,deadline)
            if m.pos not in home:
                self.stage='RETURN_BLOCKED'
                self.return_committed=True
                if active:self.commands={m.id:[]}
                return []
            # A sent build is never a built wall. Absence in the next snapshot
            # consumes the attempt and returns M through the still open gate.
            if self.last_action and self.last_action[1]=='build' and self.last_action[0]<world.round:
                self.last_action=None
                if gate not in walls:
                    self.aborted_day=self.day;self.stage='SEAL_FAILED'
                    return []
                self.stage='SEALED'
            from .forage_admission import pioneer_service_available
            p_service=(pioneer_service_available(world) if clock.phases=={'night'}
                       else bool(p and p.pos in plan['c_stands'] and not task_busy))
            observed_damage=self.damage_upper if self.damage_upper is not None else (0 if not threats else None)
            guard_ok=bool(fully_armed and w and w.alive and p and p.alive
                          and w.pos==plan['w'] and p_service
                          and w.health>=165 and p.health>=150
                          and observed_damage is not None
                          and all(u.health>2*max(observed_damage,danger.get(u.pos,0)) for u in (w,p)))
            safe=(risk_known and m.health>=165 and observed_damage is not None
                  and m.health>2*max(observed_damage,danger.get(m.pos,0)))
            from .forage_admission import assess
            admission=assess(world,rules,policy,deadline,open_exit=open_exit) if guard_ok else {'allowed':False,'reason':'guards unavailable'}
            mine_values={q:world.vendor[name] for name in ('stone','iron','copper')
                         if world.vendor.get(name,0)>0 for q in world.zones.get(name,())}
            mines=set(mine_values)
            options=[]
            reach=_field(world,[m.pos],blocked,deadline)
            for mine in sorted(mines):
                for stand in neighbours(mine):
                    if stand in reach and stand in home:
                        options.append((reach[stand]+home[stand],reach[stand],mine,stand))
            command=None;economic_floor=0
            if clock.phases=={'day'} and clock.day==self.day:
                if gate in walls:
                    if not active:return []
                    self.stage='SEALED'
                else:
                    if not guard_ok or not safe:
                        self.aborted_day=self.day;self.stage='RELEASE_CANCELLED'
                        self.diagnostic={'stage':self.stage,'admission':admission}
                        return []
                    # Allow an observed failed build and one outer retry.
                    # M remains outside; no inner backup-gunner fallback.
                    reserve=2+policy.return_buffer
                    if reserve is None or clock.until_night<=home[m.pos]+1+reserve:
                        self.aborted_day=self.day;self.stage='FALLBACK_DEADLINE'
                        return []
                    if home[m.pos]:
                        self.stage='DUSK_MOVE'
                        command=self._move(m,home)
                    else:
                        world.external_gate_permit=dict(m=m.id,w=w.id,p=p.id,gate=gate)
                        if not valid_seal(world,((m.id,'wall',gate),)):
                            world.external_gate_permit=None;return []
                        world.seal_cells=frozenset({gate})
                        self.stage='SEAL_PENDING'
                        command={'action':'build','name':'wall','targetPos':[pos_json(gate)]}
            elif clock.phases=={'night'}:
                world.night_economy_active=True
                remaining=min(130-(world.round-o)%130 for o in clock.offsets)
                self.stage='RETURN_TO_GATE'
                if (policy.night_foraging_enabled and not self.return_committed and (gate in walls or open_exit is not None) and guard_ok and safe and admission['allowed']
                        and m.capacity and m.backpack is not None):
                    saleable=self._saleable(world,m,reserve_gate=clock.day<10)
                    if not saleable:self.cashout_committed=False
                    from . import night_procurement
                    purchase=night_procurement.prepare(world,clock,rules,policy,m,reach,home,blocked,
                                                       remaining,deadline,task_actor=self.p if world.phase_task else None,
                                                       open_exit=open_exit,night_boundary=night_boundary)
                    self.purchase_report=purchase.diagnostic
                    self.purchase_report['receipt']=self.purchase_receipt
                    if self.purchase_pending:
                        purchase.candidate=None
                        purchase.shop_tail=None
                    sale_field,sale_info=night_procurement.sale_tail(
                        world,m,purchase,home,blocked,saleable,policy,deadline)
                    cash_needed=bool(saleable and (self.cashout_committed or len(m.backpack)>=m.capacity
                                                   or not options or clock.day==10 or sale_info['via_shop']))
                    if cash_needed:
                        after_sale=purchase.shop_tail if sale_info['via_shop'] and sale_field.get(m.pos,float('inf'))<remaining else None
                        command=self._night_cashout(world,m,saleable,reach,home,blocked,remaining,policy,deadline,after_sale)
                    elif purchase.candidate:
                        command=purchase.candidate.command
                        economic_floor=purchase.candidate.gold_reserve
                        self.stage='NIGHT_PURCHASE'
                    if command is None and options and len(m.backpack)<m.capacity and not cash_needed:
                        options=[o for o in options if o[1]+1+home[o[3]]+1+policy.return_buffer<remaining]
                        checkout=None
                        if clock.day==10:
                            from copy import copy
                            from .day_schedule import weighted_field
                            topology=copy(world);topology.occupied=blocked
                            vendor_cells={q for v in world.zones.get('vendor',()) for q in neighbours(v) if q in home}
                            checkout=weighted_field(topology,{q:home[q]+2+policy.return_buffer for q in vendor_cells},m,deadline)
                            if checkout is None:raise BudgetExpired
                            options=[option for option in options
                                     if option[1]+1+checkout.get(option[3],float('inf'))<remaining]
                        # With a visible vendor, reserve the complete tail for
                        # existing stock plus this one prospective collect.
                        # The estimate never contributes spendable gold.
                        tails={}
                        if world.zones.get('vendor'):
                            for name in ('stone','iron','copper'):
                                if any(o[2] in world.zones.get(name,()) for o in options):
                                    tails[name]=night_procurement.sale_tail(world,m,purchase,home,blocked,
                                        saleable,policy,deadline,extra_mineral=name)[0]
                            options=[o for o in options if any(o[2] in world.zones.get(name,())
                                      and o[1]+1+tail.get(o[3],float('inf'))<remaining for name,tail in tails.items())]
                        if not options:
                            if saleable:
                                command=self._night_cashout(world,m,saleable,reach,home,blocked,remaining,policy,deadline)
                        else:
                            def trip(o):
                                for name,tail in tails.items():
                                    if o[2] in world.zones.get(name,()):return o[1]+1+tail[o[3]]
                                return o[1]+1+(checkout[o[3]] if checkout is not None
                                                else home[o[3]]+1+policy.return_buffer)
                            chosen=min(options,key=lambda o:(-mine_values[o[2]]/trip(o),o))
                            _,travel,mine,stand=chosen
                            self.mining_report=dict(mine=mine,quoted_unit_value=mine_values[mine],
                                                    trip_actions=trip(chosen),value_per_action=mine_values[mine]/trip(chosen))
                            if travel+1+home[stand]+1+policy.return_buffer<remaining:
                                self.stage='NIGHT_FORAGE'
                                if travel==0:command={'action':'collect','targetPos':[pos_json(mine)]}
                                else:command=self._move(m,_field(world,[stand],blocked,deadline))
                if command is None:
                    if open_exit is not None and safe and remaining>home[m.pos]+policy.return_buffer+2 and not self.return_committed:
                        # Guard transit is an observation, not a permanent
                        # nightly recall. Keep exterior M available for the
                        # first frame where the actual W/P service is ready.
                        self.stage='NIGHT_WAIT_SERVICE'
                    else:
                        self.return_committed=True
                        if home[m.pos]:command=self._move(m,home)
            if time.monotonic()>=deadline:raise BudgetExpired
            self.commands={m.id:[command] if command else []}
            extra=[]
            if clock.phases=={'night'} and self.stage in ('NIGHT_FORAGE','NIGHT_CASHOUT','NIGHT_PURCHASE') and command:
                from .forage_admission import contract
                world.forage_contract=contract(world,m.id,command,admission)
                # Keep an actual return alternative available when a guard's
                # chosen action cannot satisfy the mining service dependency.
                back=self._move(m,home) if home[m.pos] else None
                if back and back!=command and (world.forage_contract['critical']
                        or p.pos not in plan['c_stands'] or getattr(world,'repair_service',{})):
                    self.commands[m.id].append(back)
                    extra=[Candidate(m.id,back,900,'return when selected guard actions interrupt night service')]
                    world.forage_contract['return_command']=back
            for actor,keys in ((w,('a','b')),(p,('c',))):
                if guard_ok and actor and actor.alive and actor.id in world.night_defenders:
                    self.commands[actor.id]=[]
                    self.firearms[actor.id]=tuple(complete[plan[k]].id for k in keys if plan[k] in complete)
            world.night_forage_commands={m.id:[command]} if command and command.get('action')=='collect' else {}
            self.diagnostic=dict(stage=self.stage,m=m.id,w=self.w,p=self.p,gate=gate,
                                 actual_outside=m.pos not in blue,open_exit=open_exit,damage_upper=self.damage_upper,admission=admission,cashout=self.cashout_report,mining=self.mining_report,purchase=self.purchase_report)
            candidates=([Candidate(m.id,command,1000,'observed external gate cycle: '+self.stage,gold_reserve=economic_floor)]+extra) if command else []
            if clock.phases=={'day'} and self.stage in ('DUSK_MOVE','SEAL_PENDING'):
                row=seal_row or (self.gate_assignment.get('row') if self.gate_assignment.get('kind')=='seal' else None)
                if row:return self._offer_gate(world,row,seal_rows or [row],candidates,policy.gate_seal_choice)
            return candidates
        except BudgetExpired:
            world.external_gate_permit=None;world.seal_cells=previous_seal
            world.night_economy_active=False
            self.commands={self.m:[]} if active and self.m else {};self.firearms={}
            self.diagnostic={'stage':'BUDGET_EXHAUSTED'}
            self.gate_offer={}
            self._restore_gate_offer(world)
            return []

    def _emergency_open(self, world, clock, plan, deadline):
        """Explicit candidate switch; observations, never a promised closed ring."""
        roster=world.night_roster
        m,w,p=(world.ours.get(i) for i in (roster.m,roster.w,roster.p))
        if not m or not m.alive:
            if self.emergency_active:
                self.stage='ROLE_UNAVAILABLE'
                self.diagnostic={'stage':self.stage,'reason':'emergency traveller unavailable',
                                 'arrived_observed':False}
                return []
            return None
        blue,yellow=station_rings(plan['anchor']);gate=plan['gate']
        threats=active_threats(world)
        known=all(r.attack_power is not None and r.attack_range is not None for r in threats)
        wall=next((u for u in world.ours.values() if u.alive and u.kind=='wall' and u.pos==gate),None)
        hit=(2*sum(r.attack_power for r in threats if distance(m.pos,r.pos)<=r.attack_range)) if known else None
        if not self.emergency_active:
            if not wall or m.pos in blue or not known:
                return None
            if hit and hit>=m.health:
                self.emergency_reason='observed two-opportunity damage threatens exterior worker'
            elif p is not None and not p.alive and w and w.alive and m.id in world.night_defenders:
                self.emergency_reason='observed pioneer death leaves third-gun replacement outside'
            else:return None
            self.emergency_active=True;self.day=clock.day
        self.w,self.m,self.p=roster.w,roster.m,roster.p
        self.return_committed=True;self.cashout_committed=False
        self.commands={m.id:[]}
        self.stage='EMERGENCY_OPEN' if wall else 'NIGHT_GATE_OPEN'
        self.diagnostic=dict(stage=self.stage,reason=self.emergency_reason,gate=gate,
                             opened_observed=wall is None,accepts_open_gap=True)
        if not known:
            self.diagnostic['blocked']='robot damage or range unknown'
            return []
        danger={}
        for x in range(world.width):
            if time.monotonic()>=deadline:raise BudgetExpired
            for y in range(world.height):
                q=(x,y)
                danger[q]=2*sum(r.attack_power for r in threats if distance(q,r.pos)<=r.attack_range)
        def route(actor,goals):
            blocked=(world.occupied|world.navigation_avoided.get(actor.pos,set())|
                     {q for q,damage in danger.items() if damage>=actor.health})-{actor.pos}
            return _field(world,set(goals)-blocked,blocked,deadline)
        if wall:
            if wall.level!=1:
                self.diagnostic['blocked']='protected or unknown gate level'
                return []
            choices=[]
            for actor in (m,w):
                if not actor or not actor.alive:continue
                path=route(actor,neighbours(gate))
                if actor.pos not in path:continue
                command=(self._move(actor,path) if path[actor.pos] else
                         {'action':'remove','targetPos':[pos_json(gate)]})
                if command:choices.append((path[actor.pos],actor.id!=m.id,actor.id,command))
            if not choices:
                self.stage='RETURN_BLOCKED';self.diagnostic['stage']=self.stage
                self.diagnostic['blocked']='no safe reachable opener'
                return []
            steps,_,identity,command=min(choices)
            self.commands[identity]=[command]
            self.diagnostic.update(opener=identity,opening_steps=steps+1)
            return [Candidate(identity,command,1100,'explicit emergency opening: '+self.emergency_reason)]
        # Removing a wall does not put M inside. Walk only after its absence is
        # observed, and retain the same maximum-two-controller night roster.
        goals=set(plan['c_stands']) if m.id in world.night_defenders else blue
        path=route(m,goals)
        if m.pos not in path:
            self.stage='RETURN_BLOCKED';self.diagnostic['stage']=self.stage
            self.diagnostic['blocked']='no safe interior route after observed opening'
            return []
        if path[m.pos]:
            command=self._move(m,path)
            if command:
                self.commands[m.id]=[command]
                return [Candidate(m.id,command,1100,'enter after observed emergency opening')]
        self.diagnostic['arrived_observed']=m.pos in goals
        if m.id in world.night_defenders:
            self.firearms[m.id]=tuple(g.id for g in world.weapons if g.pos==plan['c'])
        return []

    @staticmethod
    def _saleable(world, actor, reserve_gate=True):
        # Keep an already held stone only when another game day remains.
        return {name:count for name in ('stone','iron','copper')
                if world.vendor.get(name,0)>0
                and (count := max(0,actor.inventory[name]-(reserve_gate and name=='stone'))) > 0}

    def _night_cashout(self, world, actor, stock, reach, home, blocked, remaining, policy, deadline, after_sale=None):
        vendors={p for q in world.zones.get('vendor',()) for p in neighbours(q)
                 if p in reach and p in home}
        sale_actions=len(stock)
        tail=after_sale if after_sale is not None else {p:n+1+policy.return_buffer for p,n in home.items()}
        options=[(reach[p]+sale_actions+tail[p],reach[p],p) for p in vendors if p in tail]
        if not options:
            self.cashout_report={'status':'no observed vendor return route'}
            return None
        cost,_,stand=min(options)
        if time.monotonic()>=deadline:raise BudgetExpired
        self.cashout_report=dict(required=cost,remaining=remaining,sale_actions=sale_actions,
                                 quoted_stock_value=sum(n*world.vendor[k] for k,n in stock.items()),via_shop=after_sale is not None)
        if cost>=remaining:
            self.cashout_report['status']='return deadline prevents sale'
            return None
        self.cashout_committed=True
        self.stage='NIGHT_CASHOUT'
        if world.near_zone(actor.pos,'vendor'):
            # Use the actual current vendor cell only if selling here fits too.
            current=sale_actions+tail.get(actor.pos,float('inf'))
            if current<remaining:
                name=max(stock,key=lambda k:(stock[k]*world.vendor[k],k))
                self.cashout_report.update(status='sell personal stock',required=current)
                return {'action':'sell','name':name,'num':stock[name]}
        self.cashout_report['status']='walk to observed vendor'
        return self._move(actor,_field(world,{stand},blocked,deadline))

    def _inner_seal(self, world, clock, policy, plan, deadline, task_busy):
        """W's personal stone, actual inside route, then observed return to W."""
        from copy import copy
        from .night_roles import clear_c_access
        roster=world.night_roster
        w,m,p=(world.ours.get(i) for i in (roster.w,roster.m,roster.p))
        blue,yellow=station_rings(plan['anchor']);gate=plan['gate']
        walls={u.pos for u in world.ours.values() if u.alive and u.kind=='wall'}
        if not m or not all(u and u.alive for u in (w,p)):
            self.inner_backup=False
            if roster.traffic.get('traveller') == roster.w:
                world.roster_yielding.discard(roster.traffic.get('blocker'))
                roster.traffic={}
            self.stage='ROLE_UNAVAILABLE'
            self.diagnostic=dict(stage=self.stage,gate=gate,returned_observed=False,
                                 reason='backup worker or pioneer unavailable')
            return []
        self.w,self.m,self.p=w.id,m.id,p.id
        blocked=(world.occupied|world.navigation_avoided.get(w.pos,set()))-{w.pos}
        # Both outbound and return routes stay strictly in the blue floor.
        blocked |= {(x,y) for x in range(world.width) for y in range(world.height)
                    if (x,y) not in blue}
        home=_field(world,{plan['w']},blocked,deadline)
        returning = gate in walls or clock.phases != {'day'} or task_busy or not w.inventory['stone']
        if returning:
            candidates,returned,reason=self._gate_return_tail(world,plan,deadline)
            self.stage='BACKUP_RETURN'
            if returned:
                self.inner_backup=False
                if roster.traffic.get('traveller') == w.id:
                    world.roster_yielding.discard(roster.traffic.get('blocker'))
                    roster.traffic={}
                self.stage='SEALED' if gate in walls else 'SEAL_FAILED'
                self.commands={}
                if self.gate_assignment:self.gate_assignment['completed_observed']=True
            self.diagnostic=dict(stage=self.stage,builder=w.id,returned_observed=returned,
                                 worker_returned_observed=w.pos==plan['w'],gate=gate,reason=reason)
            return candidates
        if ((m.alive and m.pos in blue|yellow|world.stations[0].cells) or p.pos not in blue or w.pos not in blue
                or {u.pos for u in world.weapons}!={q for _,q in plan['slots']}
                or walls != yellow-{gate}
                or any(u.alive and u.pos in blue|world.stations[0].cells for u in world.robots.values())):
            self.stage='BACKUP_UNAVAILABLE'
            return []
        goals=set(neighbours(gate)) & blue - blocked
        route=_field(world,goals,blocked,deadline)
        if w.pos not in route:
            # Preview a genuine one-step P yield and charge both the yield and
            # W's full trip before committing the move.
            preview=copy(world);preview.night_roster=copy(roster)
            preview.roster_yielding=set(world.roster_yielding)
            yielding=clear_c_access(preview,w,set(neighbours(gate)) & blue,p.id,deadline)
            if yielding:
                stand=yielding[p.id]
                freed=(blocked-{p.pos})|{stand}
                outbound=_field(world,set(neighbours(gate)) & blue-freed,freed,deadline)
                back=_field(world,{plan['w']},freed,deadline)
                options=[outbound[w.pos]+1+back[q] for q in neighbours(gate)
                         if q in back and q in outbound and w.pos in outbound and outbound[q]==0]
                if options and max(options)+2+policy.return_buffer < clock.until_night:
                    roster.traffic=preview.night_roster.traffic
                    world.roster_yielding=preview.roster_yielding
                    self.inner_backup=True;self.day=clock.day
                    command={'action':'move','targetPos':[pos_json(stand)]}
                    self.commands={m.id:[],w.id:[],p.id:[command]}
                    self.stage='BACKUP_YIELD'
                    self.diagnostic=dict(stage=self.stage,builder=w.id,gate=gate)
                    return [Candidate(p.id,command,1000,'yield for actual inner backup seal')]
            self.stage='BACKUP_BLOCKED'
            return []
        # Outbound field alone does not prove a return after sealing.
        choices=[q for q in goals if q in home]
        required=route[w.pos]+1+max((home[q] for q in choices),default=float('inf'))
        if required+2+policy.return_buffer >= clock.until_night:
            self.stage='BACKUP_DEADLINE'
            if roster.traffic.get('traveller') == w.id:
                world.roster_yielding.discard(roster.traffic.get('blocker'))
                roster.traffic={}
            self.inner_backup=False
            return []
        self.inner_backup=True;self.day=clock.day
        self.commands={m.id:[],p.id:[]}
        if route[w.pos]:
            command=self._move(w,route);self.stage='BACKUP_MOVE'
        else:
            world.external_gate_permit=dict(m=m.id,w=w.id,p=p.id,gate=gate,builder=w.id,inner=True)
            if not valid_seal(world,((w.id,'wall',gate),)):
                world.external_gate_permit=None;return []
            world.seal_cells=frozenset({gate})
            command={'action':'build','name':'wall','targetPos':[pos_json(gate)]}
            self.stage='BACKUP_SEAL_PENDING'
        self.commands[w.id]=[command] if command else []
        self.diagnostic=dict(stage=self.stage,builder=w.id,gate=gate,required=required,actual_m_outside=True)
        return [Candidate(w.id,command,1000,'observed inner backup seal: '+self.stage)] if command else []

    def _dawn_open(self, world, plan, deadline, policy, task_busy=False):
        """Choose a real reachable worker; opening completes only on observation."""
        gate = plan['gate']
        wall = next((u for u in world.ours.values() if u.alive and u.kind == 'wall' and u.pos == gate), None)
        if wall is None:
            self.stage = 'INACTIVE'
            self.last_action = None
            self.diagnostic = {'stage': self.stage, 'gate': gate, 'opened_observed': True}
            assignment=self.gate_assignment
            if assignment.get('kind')=='dawn' and assignment.get('worker')==self.w and not assignment.get('completed_observed'):
                candidates,returned,reason=self._gate_return_tail(world,plan,deadline)
                self.diagnostic.update(returned_observed=returned,tail=reason,
                                       worker_returned_observed=bool(world.ours.get(self.w) and world.ours[self.w].pos==plan['w']))
                assignment['completed_observed']=returned
                if not returned:
                    return self._offer_gate(world,assignment['row'],[assignment['row']],candidates,policy.gate_dawn_choice)
            if assignment.get('kind')=='dawn':assignment['completed_observed']=True
            self.day = None
            return []
        if wall.level != 1:
            self.stage = 'PROTECTED_GATE'
            self.diagnostic = {'stage': self.stage, 'gate': gate}
            return []
        # This round's gate assignment also constrains legacy immediate opening
        # candidates. Other economic, task and evasion actions remain available.
        world.gate_worker_duty=dict(round=world.round,gate=gate,worker=None)
        choices=self._gate_options(world,plan,deadline,'dawn',policy,task_busy)
        assignment=self.gate_assignment
        current=assignment.get('worker') if assignment.get('kind')=='dawn' and not assignment.get('completed_observed') else None
        choice=self._choose_gate(choices,policy.gate_dawn_choice,current)
        if not choice:
            self.stage = 'RETURN_BLOCKED'
            self.diagnostic = {'stage': self.stage, 'gate': gate, 'reason': 'no complete legal gate-worker plan',
                               'mode':policy.gate_dawn_choice}
            return []
        identity=choice['actor'];command=choice['command'];backup=choice['label']=='w'
        world.gate_worker_duty['worker']=choice['worker']
        if choice['traffic']:
            world.night_roster.traffic=choice['traffic']
            world.roster_yielding.add(choice['traffic']['blocker'])
        self.stage = 'DAWN_BACKUP' if backup else 'DAWN_OPEN'
        self.commands = {identity: [command]}
        if identity!=choice['worker']:self.commands[choice['worker']]=[]
        if backup and self.p and not world.phase_task:self.commands.setdefault(self.p,[])
        self.diagnostic = dict(stage=self.stage, gate=gate, opener=choice['worker'], backup=backup,
                               opening_steps=choice['planned']['opening_actions'], opened_observed=False)
        if identity!=choice['worker']:self.diagnostic['yielding']=identity
        return self._offer_gate(world,choice,choices,
            [Candidate(identity, command, 1000, 'observed dawn gate worker: ' + self.stage)],policy.gate_dawn_choice)

    def _restore_gate_offer(self, world):
        if not self.gate_before:return
        for name,value in self.gate_before.items():
            if name not in ('traffic','yielding'):setattr(self,name,value)
        world.night_roster.traffic=self.gate_before['traffic']
        world.roster_yielding=self.gate_before['yielding']

    @staticmethod
    def _move(actor,field):
        choices=sorted(q for q in neighbours(actor.pos) if q in field and field[q]<field[actor.pos])
        return {'action':'move','targetPos':[pos_json(choices[0])]} if choices else None

    def finalize(self,world,response):
        offer=self.gate_offer
        if offer:
            selected=[(identity,command) for identity,command in offer['commands']
                      if response['roleCommandMap'].get(identity)==command]
            if selected:
                from copy import deepcopy
                row=offer['row']
                if (not self.gate_assignment or self.gate_assignment.get('kind')!=row['kind']
                        or self.gate_assignment.get('worker')!=row['worker'] or self.gate_assignment.get('completed_observed')):
                    self.gate_assignment=dict(kind=row['kind'],worker=row['worker'],gate=row['gate'],
                        row=deepcopy(row),planned=deepcopy(row['planned']),mode=offer['mode'],
                        started_round=world.round,selected_actions=0,confirmed_actions=0,observed=[],
                        response_hold_rounds={},completed_observed=False)
                identity,issued=selected[0]
                self.gate_assignment['selected_actions']+=1
                self.gate_assignment['pending']=dict(round=world.round,actor=identity,command=deepcopy(issued))
                self.gate_assignment['alternatives']=deepcopy(offer['alternatives'])
            else:
                self._restore_gate_offer(world)
                self.diagnostic['assignment_rejected']=True
            assignment=self.gate_assignment
            if (assignment.get('kind')==offer['row']['kind'] and assignment.get('worker')==offer['row']['worker']
                    and assignment.get('last_hold_round')!=world.round):
                busy=set(response['roleCommandMap'])|{command.get('controllerId') for command in response['roleCommandMap'].values()}
                held=assignment.setdefault('response_hold_rounds',{})
                for identity in offer['held']:
                    if identity not in busy:held[identity]=held.get(identity,0)+1
                assignment['last_hold_round']=world.round
        if self.gate_assignment:
            self.diagnostic['assignment_state']={k:v for k,v in self.gate_assignment.items() if k!='row'}
        command=response['roleCommandMap'].get(self.m,{})
        if (command.get('action')=='buy' and self.stage=='NIGHT_PURCHASE'
                and command in self.commands.get(self.m,()) and not self.purchase_pending):
            self.purchase_pending=dict(actor=self.m,name=command['name'],round=world.round,
                                       count_before=world.ours[self.m].inventory[command['name']])
        intent=getattr(world,'forage_contract',None)
        if intent:
            from .forage_admission import bundle_allowed
            if (command==intent.get('return_command') or
                    not bundle_allowed(world,response['roleCommandMap'],complete=True,require_service=True)):
                self.return_committed=True
                self.stage='RETURN_TO_GATE'
                self.diagnostic.update(stage=self.stage,reason='selected actions interrupt guard service')
        if command.get('action') in ('build','remove') and command in self.commands.get(self.m,()):
            self.last_action=(world.round,command['action'])
