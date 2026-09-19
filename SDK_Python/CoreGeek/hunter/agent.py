"""Serial, bounded turn transaction with a validated current-state incumbent."""
from collections import OrderedDict, deque
from dataclasses import replace
from copy import deepcopy
import logging
from pathlib import Path
import threading
import time

from . import wall_policy, combat, economy, director, joint_lookahead, task_schedule, site_clearance, exterior_evasion, forage_admission
from .arbitration import select
from .protocol import parse_request, fingerprint, empty_response, validate_response, distance, position
from .rules import Rules, Policy, Clock
from .state import Session
from .duty_budget import DutyBudget
from . import defence_duties

LOG = logging.getLogger("hunter")


class Agent:
    def __init__(self, rules=None, policy=None, diagnostics=None):
        self.diagnostics = diagnostics
        self.rules = rules or Rules.load(Path(__file__).resolve().parents[1] / "config/verified_rules.json")
        self.policy = policy or Policy()
        self.lock = threading.Lock()
        self.sessions = OrderedDict()
        self.active_key = None
        self.epoch = 0
        self.telemetry = deque(maxlen=self.policy.telemetry_entries)

    def reset(self):
        """Explicit administrative new-match reset; never inferred from a stale round."""
        with self.lock:
            self.sessions.clear()
            self.active_key = None
            self.epoch += 1

    def _base(self, world, clock, task_actor, candidates=None):
        # Independent short selection budget also works if main planning expires.
        world.night_foraging_enabled=self.policy.night_foraging_enabled
        from .night_roles import defender_ids
        defender_ids(world)
        if candidates is None:
            candidates = economy.immediate(world, self.rules, task_actor, jobs=economy.construction_jobs(world, self.rules, self.policy), policy=self.policy)
        return select(world, clock, self.rules, self.policy, candidates, time.monotonic()+0.03,
                      task_actor=task_actor)

    def _isolated_response(self, world):
        task_actor = next((u.id for u in world.movers if u.kind == "pioneer" and world.phase_task), None)
        return self._base(world, Clock(world.round, self.rules.round_origin), task_actor).response

    def _early_base(self, world, clock, task_actor, draft):
        # No layout, construction, gate or service promise has been established
        # yet. Keep only current personal healing and daytime ore actions; do
        # not spend gate stone, upgrade a not-yet-identified G, or authorize a
        # night excursion/repair before its actual duty checks exist.
        choices = economy.immediate(world, self.rules, task_actor, jobs={}, policy=self.policy, local_only=True)
        simple = [c for c in choices if
                  (c.command.get('action') == 'use' and c.command.get('name') == 'Medicine') or
                  (clock.phases == {'day'} and (c.command.get('action') == 'collect' or
                   c.command.get('action') == 'sell' and c.command.get('name') != 'stone'))]
        return self._base(world, clock, task_actor, draft.filter_failures(simple, world.round))

    def _diagnostic(self, event, **data):
        if self.diagnostics is not None:
            try:
                if event == "outcome":
                    self.diagnostics.outcome(data["value"])
                else:
                    self.diagnostics.event(event, **data)
            except Exception:
                pass

    def callback(self, raw):
        if self.diagnostics is not None:
            return self.diagnostics.run(raw, self._callback)
        return self._callback(raw)

    def _callback(self, raw):
        start = time.monotonic()
        acquired = False
        fallback = empty_response()
        try:
            world = parse_request(raw)
            digest = fingerprint(raw)
            acquired = self.lock.acquire(timeout=self.policy.lock_seconds)
            if not acquired:
                self._diagnostic("outcome", value="lock_timeout")
                # Never advance an independent session or create channel requests.
                return self._isolated_response(world)
            key = (world.team_id, world.side)
            session = self.sessions.get(key)
            if session is not None:
                cached = session.cache.get((world.round, digest))
                if cached is not None:
                    self._diagnostic("outcome", value="cache_hit")
                    return deepcopy(cached)
                if key != self.active_key or world.round <= session.last_round:
                    self._diagnostic("outcome", value="isolated_stale_or_other_session")
                    return self._isolated_response(world)
            else:
                self.epoch += 1
                session = Session(self.epoch, key, self.rules.round_origin)
            draft = deepcopy(session)
            draft.tasks.reuse_enabled = self.policy.skill_reuse_enabled
            world.strategy_policy = self.policy
            clock = draft.reconcile(world)
            world.news_task_hold = (self.policy.news_daily_enabled and
                (bool(draft.intelligence.return_plan) or
                 draft.intelligence.cycle.hold(draft.intelligence,world,clock,draft,self.policy)))
            from .task_deadline import priority as task_priority
            world.six_task_priority=task_priority(world,clock)
            # A request channel can run while P approaches a point. Do not
            # freeze the actor merely because ordinary news analysis is due.
            if world.six_task_priority and not draft.intelligence.return_plan:
                world.news_task_hold=False
            from . import pioneer_trade
            pioneer_trade.prepare(world, clock)
            task_actor = draft.task_actor(world)
            fallback = self._early_base(world, clock, task_actor, draft).response
            draft.task_layout.prepare(world, self.rules, self.policy,
                                      min(start + self.policy.planning_seconds, time.monotonic() + .12))
            world.duty_budget = DutyBudget(start+self.policy.planning_seconds)
            draft.night_roster.prepare(world)
            draft.guard_stock.prepare(world,clock)
            if getattr(world,'role_handoff',None):
                incoming=draft.night_roster.w
                draft.exterior_escape.clear()
                draft.day_schedule.active.pop(incoming,None)
                draft.day_schedule.division.helper_batches.pop(incoming,None)
                draft.day_schedule.division.helper_clearance.pop(incoming,None)
                draft.economic_routes.active.pop(incoming,None)
                draft.external_gate.commands.pop(incoming,None)
                draft.sunset_market.checkout_intents.pop(incoming,None)
                draft.sunset_market.upgrade_travellers.discard(incoming)
                draft.night_clear.actor=None
                draft.repair.active.clear()
            world.wall_rebuild_plan=draft.wall_rebuild.plan
            from . import funding
            world.funding_plan=[]
            world.treasure_reserved_gold=0
            world.night_foraging_enabled=self.policy.night_foraging_enabled
            from . import day_access
            day_access.prepare(world,clock,min(start+self.policy.planning_seconds,time.monotonic()+.025),draft.day_access_choice)
            economy.prepare_wall_cycle(world, clock, self.rules, self.policy)
            draft.sunset_market.begin_frame(world)
            draft.sunset_market.publish_checkout_targets(world)
            draft.wall_service.prepare(world, self.rules)
            draft.repair.publish_demand(world,clock,self.rules,self.policy,
                min(start+self.policy.planning_seconds,time.monotonic()+.04))
            # Quote-independent invalidation precedes grant publication (E5);
            # WallRebuild action planning still runs at its original point.
            draft.wall_rebuild.reconcile(world, clock, self.rules, self.policy, draft)
            funding.publish(world,clock,self.rules,self.policy,draft.intelligence,
                            min(start+self.policy.planning_seconds,time.monotonic()+.04))
            daily = draft.sunset_market.caretaker_day
            worker=draft.night_roster.w
            worker_unit=world.ours.get(worker)
            paid_weapon_delivery=bool(worker_unit and any(n and name.startswith('WeaponUpgradeVoucher')
                for name,n in worker_unit.inventory.items()))
            world.worker_upgrade_checkout_pending = bool(daily.day == clock.day
                and daily.phase in {'buy','sell'} and draft.sunset_market.upgrade_owner == worker)
            world.worker_close_requested = bool(self.policy.pioneer_rotation_enabled
                and daily.day == clock.day and daily.phase in {'close','use'} and not paid_weapon_delivery
                and not (world.phase_task or draft.tasks.active or draft.tasks.accept_pending))
            world.worker_upgrade_use_steps = daily.use_budget if world.worker_close_requested else 0
            world.worker_material_return = bool(daily.day == clock.day
                and daily.phase == 'home' and daily.construction_only)
            gate_candidates=world.duty_budget.run('gate', lambda budget_end: draft.external_gate.prepare(
                world,clock,self.rules,self.policy,budget_end,
                task_busy=bool(task_actor or draft.tasks.active or draft.tasks.accept_pending),defer_regular_night=True),
                min(start+self.policy.planning_seconds,time.monotonic()+.06))
            evasion=[]
            evasion_report={'status':'emergency gate plan active'}
            if not draft.external_gate.emergency_active and not draft.external_gate.deferred_night:
                evasion,evasion_report=world.duty_budget.run('exterior_evasion', lambda budget_end:
                    exterior_evasion.propose(world,clock,budget_end,trapped=draft.external_gate.stage=='RETURN_BLOCKED',state=draft.exterior_escape), min(start+self.policy.planning_seconds,time.monotonic()+.01))
                if evasion:
                    identity=evasion[0].actor
                    gate_candidates=[c for c in gate_candidates if c.actor!=identity]+evasion
                    draft.external_gate.commands[identity]=[c.command for c in evasion]
                    world.night_forage_commands.pop(identity,None)
            build_jobs = economy.construction_jobs(world, self.rules, self.policy)
            build_jobs = draft.day_schedule.division.assign(world,clock,self.rules,self.policy,build_jobs,time.monotonic()+.25)
            from .arbitration import Candidate
            clearance = getattr(world,'helper_clearance_commands',{})
            if clearance and not any(i in draft.external_gate.commands for i in clearance):
                for identity, commands in clearance.items():
                    draft.external_gate.commands[identity] = commands
                    gate_candidates.extend(Candidate(identity,command,1100,'clear exterior wall helper corridor')
                                           for command in commands)
            else:
                draft.day_schedule.division.helper_clearance.clear()
            gate_candidates=draft.day_schedule.division.prioritize_front_helper(
                world,clock,draft.external_gate,build_jobs,gate_candidates)
            if draft.external_gate.commands:
                build_jobs={i:j for i,j in build_jobs.items() if i not in draft.external_gate.commands}
            draft.day_schedule.division.reconcile_assistance(world,build_jobs)
            draft.wall_service.assign(world,clock,build_jobs)
            rebuilding=draft.wall_rebuild.prepare(world,clock,self.rules,self.policy,
                min(start+self.policy.planning_seconds,time.monotonic()+.055),draft)
            build_jobs={i:j for i,j in build_jobs.items() if i not in world.wall_rebuild_actions}
            immediate = draft.filter_failures(economy.immediate(world, self.rules, task_actor, jobs=build_jobs, policy=self.policy), world.round)
            from .wall_service import build_permitted, use_permitted
            immediate=[candidate for candidate in immediate
                       if build_permitted(world,candidate) and use_permitted(world,candidate)]
            incumbent = self._base(world, clock, task_actor, immediate)
            fallback = incumbent.response
            candidates = list(immediate)+rebuilding
            deadline = start + self.policy.planning_seconds
            task_choice = task_schedule.choose(world,clock,self.policy,min(deadline,time.monotonic()+.04),draft.tasks.timing)
            if (self.policy.pioneer_rotation_enabled and not world.six_task_priority and clock.phases=={'day'} and task_choice
                    and task_choice.get('selected') and not draft.tasks.active and not draft.tasks.accept_pending
                    and draft.sunset_market.upgrade_owner in (None,task_choice['actor'])):
                actor=world.ours[task_choice['actor']]
                committed=draft.sunset_market.upgrade_owner==actor.id
                if task_schedule.checkout_before_task(world,clock,self.rules,self.policy,
                        task_choice['selected'],actor,committed,min(deadline,time.monotonic()+.04)):
                    task_choice=dict(actor=actor.id,selected=None,candidates=[],
                        reason='verified checkout and delivery fits before selected task')
            from .night_roles import admit_task_departure
            task_choice = world.duty_budget.run('task_handoff', lambda budget_end:
                admit_task_departure(world, clock, task_choice, budget_end), min(deadline,time.monotonic()+.02))
            guidance = director.propose(world, clock, task_actor, draft.tasks.active, min(deadline, time.monotonic()+0.12), self.policy, draft.risk,
                                        failed_steps=draft.failed_move_steps(world) if self.policy.return_detour_enabled else None)
            guidance.committed_actions = world.wall_rebuild_actions
            from .wall_policy import purchase_permitted
            from .wall_service import use_permitted
            def service_permit(candidate):
                for label,allowed in [('buy_policy',purchase_permitted(world,self.rules,candidate)),
                                      ('wall_use_policy',use_permitted(world,candidate)),
                                      ('wall_build_policy',build_permitted(world,candidate))]:
                    if not allowed:return guidance._permission(False,candidate,label)
                return True
            guidance.purchase_permit = service_permit
            from . import return_recovery
            recovery_moves, return_recovery_report = return_recovery.propose(
                world,clock,self.policy,min(deadline,time.monotonic()+.04))
            if recovery_moves:
                for c in recovery_moves:
                    guidance.candidates=[old for old in guidance.candidates if old.actor!=c.actor]
                    guidance.candidates.append(c)
                    guidance.roster_transit_actions[c.actor]=[c.command]
                    build_jobs.pop(c.actor,None)
                draft.day_schedule.division.reconcile_assistance(world,build_jobs)
            candidates.extend(draft.tasks.candidates(world, choice=task_choice))
            world.pioneer_trade_stands = guidance.operator_stands
            repairs = world.duty_budget.run('repair', lambda budget_end: draft.repair.prepare(
                world, clock, self.rules, self.policy, budget_end, draft.tasks.active),
                min(deadline,time.monotonic()+.04))
            if self.policy.pioneer_rotation_enabled and clock.phases=={'night'}:
                for c in repairs:
                    guidance.repair_actions.setdefault(c.actor,[]).append(c.command)
                for identity in getattr(world,'repair_holds',{}):
                    guidance.repair_actions.setdefault(identity,[])
            pioneer = draft.night_roster.p
            if (world.task_return_required and pioneer in guidance.roster_transit_actions
                    and not draft.night_roster.traffic
                    and pioneer not in world.roster_yielding
                    and pioneer not in getattr(world, 'fixed_w_transit_actors', ())):
                # A selected repair excursion is part of P's night duty.
                # Admit only this frame's revalidated repair actions, without
                # interrupting an actual teammate passage or releasing trade.
                guidance.roster_transit_actions[pioneer].extend(
                    c.command for c in repairs if c.actor == pioneer)
            world.forage_task_commands=[c.command for c in draft.filter_failures(candidates,world.round)
                                        if c.actor==draft.night_roster.p and c.command.get('action')=='submitAnswer']
            if draft.external_gate.deferred_night:
                gate_candidates=world.duty_budget.run('night_service', lambda budget_end:
                    draft.external_gate.resume_night(world,clock,self.rules,self.policy,budget_end,
                        draft.tasks.active, bool(task_actor or draft.tasks.active or draft.tasks.accept_pending)),
                    min(deadline,time.monotonic()+.06))
                evasion,evasion_report=world.duty_budget.run('exterior_evasion', lambda budget_end:
                    exterior_evasion.propose(world,clock,budget_end,trapped=draft.external_gate.stage=='RETURN_BLOCKED',state=draft.exterior_escape),min(deadline,time.monotonic()+.01))
                if evasion:
                    identity=evasion[0].actor
                    gate_candidates=[c for c in gate_candidates if c.actor!=identity]+evasion
                    draft.external_gate.commands[identity]=[c.command for c in evasion]
                    world.night_forage_commands.pop(identity,None)
                    world.forage_contract=None
            if evasion:
                guidance.survival_actions[evasion[0].actor] = [c.command for c in evasion]
            if draft.external_gate.commands:
                if self.policy.pioneer_rotation_enabled:
                    for identity, commands in draft.external_gate.commands.items():
                        guidance.roster_transit_actions.pop(identity,None)
                        if identity==draft.night_roster.p:
                            moves=[c for c in commands if c.get('action')=='move']
                            world.pioneer_defence_moves.extend(moves)
                            guidance.task_moves.update(tuple(c['targetPos'][0][k] for k in ('x','y')) for c in moves)
                guidance.duty_permit=draft.external_gate.permit
                guidance.candidates=[c for c in guidance.candidates if guidance.permit(c)]
                guidance.return_routes={i:r for i,r in guidance.return_routes.items() if i not in draft.external_gate.commands}
                # Exterior M work must not erase W/P destinations used by
                # shopping and return planners. Only actual firearm holds override.
                guidance.operator_stands.update({i:world.ours[i].pos for i in draft.external_gate.firearms})
                guidance.candidates.extend(gate_candidates)
                guidance.operator_plan_status='external_gate_fixed_guards'
            if draft.external_gate.commands:
                for identity, commands in getattr(world, 'repair_commands', {}).items():
                    if identity in draft.external_gate.firearms:
                        draft.external_gate.commands[identity].extend(commands)
            guidance.candidates.extend(repairs)
            candidates.extend(repairs)
            exterior_repairs=draft.exterior_repair.prepare(world,clock,self.rules,self.policy,guidance,
                min(deadline,time.monotonic()+.025))
            candidates.extend(exterior_repairs)
            guidance.candidates.extend(exterior_repairs)
            cleared_work=draft.night_clear.prepare(world,clock,self.rules,self.policy,guidance,min(deadline,time.monotonic()+.065))
            candidates.extend(cleared_work)
            guidance.candidates.extend(cleared_work)
            # A fixed gun site may initially be occupied by an idle pioneer.
            # Keep the site stable and move the role using real free cells.
            if world.battery_plan and clock.phases == {'day'}:
                from .navigation import distance_field, neighbours
                sites = {p:n for n,p in world.battery_plan['slots']}
                for actor in world.movers:
                    if actor.id == task_actor or actor.pos not in sites:
                        continue
                    rule = self.rules.build_rule(world,sites[actor.pos])
                    if rule is None or world.gold is None or world.gold < rule.gold:
                        continue
                    field = distance_field(world,[actor.pos],actor.pos,min(deadline,time.monotonic()+.01))
                    free = (world.build_interior-world.occupied-set(sites)) & field.keys()
                    if not free:
                        continue
                    goal = min(free,key=lambda p:(field[p],p))
                    clear = director.return_plan(world,clock,{actor.id:goal},replace(self.policy,return_buffer=130),min(deadline,time.monotonic()+.01))
                    if clear is not None:
                        guidance.return_routes.update(clear[0])
                        guidance.candidates = [c for c in guidance.candidates if c.actor!=actor.id]
                        for c in clear[1]:c.reason='clear reserved gun site for construction'
                        guidance.candidates.extend(clear[1])
            # Stage the worker who actually owns the final stone before the
            # gate closes. A different worker cannot spend that inventory.
            if clock.phases == {"day"}:
                from .navigation import distance_field, neighbours
                for identity, job in build_jobs.items():
                    actor = world.ours[identity]
                    if self.policy.pioneer_rotation_enabled:
                        continue  # worker_gate owns the final builder and ordered ingress.
                    if identity not in world.night_defenders or not job.get("gate") or not actor.inventory["stone"]:
                        continue
                    if guidance.return_routes.get(identity,{}).get("yield_for"):
                        continue  # Finish the real gate-traffic clearing route first.
                    entries = set(neighbours(job["target"])) & world.build_interior
                    goals = entries-(world.occupied-{actor.pos})
                    if world.seal_cells and not goals:
                        # The last arrival may occupy the builder's only work
                        # cell. Give it back before sealing: a pioneer cannot
                        # spend the worker's personal stone.
                        for blocker in world.movers:
                            if blocker.id == task_actor or blocker.pos not in entries:
                                continue
                            field = distance_field(world,[blocker.pos],blocker.pos,min(deadline,time.monotonic()+.01))
                            free = (world.build_interior-entries-world.occupied) & field.keys()
                            if not free:
                                continue
                            clear = min(free,key=lambda p:(field[p],p))
                            yielding = director.return_plan(world,clock,{blocker.id:clear},replace(self.policy,return_buffer=130),min(deadline,time.monotonic()+.01))
                            if yielding is not None:
                                guidance.return_routes.update(yielding[0])
                                guidance.candidates = [c for c in guidance.candidates if not (c.actor==blocker.id and c.command.get("action")=="move")]
                                for c in yielding[1]:c.reason="clear final wall builder's work cell"
                                guidance.candidates.extend(yielding[1])
                    if any(u.pos not in world.build_interior for u in world.movers):
                        # The staging worker must leave an inner landing free
                        # for the last returning teammate, not plug the gate.
                        free_entries = entries-(world.occupied-{actor.pos})
                        if len(free_entries) <= 1:
                            goals = (world.build_interior-entries)-(world.occupied-{actor.pos})
                    if not goals and actor.pos in world.build_interior:
                        goals = {actor.pos}  # Keep staging while a teammate clears the landing.
                    field = distance_field(world,[actor.pos],actor.pos,min(deadline,time.monotonic()+.02))
                    reachable = goals & field.keys()
                    if not reachable:
                        continue
                    stand = min(reachable,key=lambda p:(distance(p,job["target"]),field[p],p))
                    # Reserve the stone, not the worker's whole day. The normal
                    # distance/buffer deadline still leaves time to stage/seal.
                    staged = director.return_plan(world,clock,{identity:stand},self.policy,min(deadline,time.monotonic()+.02))
                    if staged is not None:
                        guidance.return_routes.update(staged[0])
                        guidance.candidates = [c for c in guidance.candidates if not (c.actor==identity and c.reason.startswith("execute due return"))]
                        for c in staged[1]:c.reason="stage final stone owner inside gate before sealing"
                        guidance.candidates.extend(staged[1])
            for actor in world.movers:
                guidance.blocked_moves.setdefault(actor.id, set()).update(world.navigation_avoided.get(actor.pos, set()))
            if recovery_moves:
                prior_duty=guidance.duty_permit
                guidance.duty_permit=lambda c, previous=prior_duty: (c.command in world.return_recovery_actions[c.actor]
                    if c.actor in world.return_recovery_actions else previous(c) if previous else None)
            clearing = draft.filter_failures(site_clearance.propose(world,clock,self.rules,guidance,
                min(deadline,time.monotonic()+.02),task_actor),world.round)
            clearing_ids={c.actor for c in clearing}
            guidance.candidates=[c for c in guidance.candidates if c.actor not in clearing_ids]
            for c in clearing:
                guidance.site_clear_actions.setdefault(c.actor,[]).append(c.command)
            guidance.candidates.extend(clearing)
            # Finish a selected task approach before switching to another
            # ordinary trip. Revalidate availability, route and return time
            # every frame; this is not a reservation of an unaccepted task.
            approach_key = None
            selected_task = task_choice and task_choice.get('selected')
            if selected_task:
                offer = selected_task['task']
                approach_key = (offer.get('taskType'), tuple(sorted(world.task_cells(offer))))
                identity = task_choice['actor']
                prior = draft.task_approach
                if ((world.six_task_priority or prior.get('round') == world.round-1 and prior.get('actor') == identity
                        and prior.get('key') == approach_key) and (world.six_task_priority or offer.get('isValid') is True)
                        and (world.six_task_priority or offer.get('coldDownRounds') == 0) and not world.critical_base_ids
                        and identity not in clearing_ids and identity not in draft.external_gate.commands
                        and not guidance.return_routes.get(identity, {}).get('due')):
                    approach = task_choice['candidates'] + draft.tasks.candidates(world, choice=task_choice)
                    old_work=guidance.work_plans.pop(identity,None) if world.six_task_priority else None
                    approach = [c for c in draft.filter_failures(approach, world.round) if guidance.permit(c)]
                    waiting_here=(world.six_task_priority and world.ours[identity].pos==selected_task['goal'] and offer.get('coldDownRounds',0)>0)
                    if not approach and not waiting_here and old_work:guidance.work_plans[identity]=old_work
                    if approach or waiting_here:
                        guidance.work_plans[identity] = dict(owner='task_approach', phase='approach_or_accept',
                            commands=[c.command for c in approach])
                        candidates.extend(approach)
            world.treasure_actions = {}
            # Funding is published before any consumer, including M's sale planner.
            if not draft.tasks.active and not draft.tasks.accept_pending:
                world.pioneer_trade_stands = guidance.operator_stands
                from .robot_targets import cleanup_targets
                trip=draft.intelligence.execution
                trip_actor=world.ours.get(trip.get('actor'))
                underway=bool(draft.intelligence.return_plan or (trip_actor and trip.get('home')
                    and trip_actor.pos!=tuple(trip['home']) and trip.get('stage') in
                    {'procure','travel','prepare_return','wait_open','summon'}))
                cleanup_hold=bool(cleanup_targets(world)) and not underway
                treasure = ([] if cleanup_hold else draft.intelligence.candidates(world,clock,self.policy,min(deadline,time.monotonic()+.15)))
                if cleanup_hold:draft.intelligence.diagnostic=dict(stage='waiting',reason='opponent_cleanup_owns_pioneer')
                report=draft.intelligence.diagnostic
                treasure_actor=report.get('actor')
                clear_treasure=(clock.phases=={'night'} and getattr(world,'own_wave_cleared',False)
                    and treasure_actor==draft.night_roster.p
                    and treasure_actor not in guidance.survival_actions
                    and treasure_actor not in getattr(world,'return_recovery_actions',{})
                    and not getattr(world,'repair_commands',{}).get(treasure_actor)
                    and (treasure or report.get('stage')=='wait_open'))
                if clear_treasure:
                    # Only the completed safe circuit overrides an idle gun
                    # hold. A new wave removes this grant on the next frame.
                    exact=[c.command for c in treasure]
                    previous=guidance.duty_permit
                    def clear_permit(c,identity=treasure_actor,commands=exact,prior=previous):
                        owner=c.command.get('controllerId') if c.command.get('action')=='attack' else c.actor
                        if owner==identity:
                            return c.command in commands or (c.command.get('action')=='use' and c.command.get('name') in {'Medicine','Bomb','DizzyWeapon'})
                        return prior(c) if prior else None
                    guidance.duty_permit=clear_permit
                if self.policy.news_daily_enabled and not world.six_task_priority and (treasure or report.get('stage')=='wait_open'):
                    if guidance.work_plans.get(treasure_actor,{}).get('owner')=='task_approach':
                        guidance.work_plans.pop(treasure_actor)
                treasure = [c for c in draft.filter_failures(treasure,world.round) if guidance.permit(c)]
                task_reserved = bool(task_choice and task_choice.get('selected') and (world.six_task_priority or not self.policy.news_daily_enabled))
                if task_reserved:
                    # The bounded task plan has a stated score and deadline.
                    # Treasure observation still runs, but must not erase that
                    # feasible plan or reserve its funds for an unknown payoff.
                    treasure = []
                report = draft.intelligence.diagnostic
                identity = report.get('actor')
                waiting = (not task_reserved and report.get('stage')=='wait_open' and identity not in guidance.work_plans
                           and identity not in clearing_ids
                           and identity not in guidance.roster_transit_actions
                           and identity not in draft.external_gate.commands
                           and not guidance.return_routes.get(identity,{}).get('due'))
                waiting = waiting or bool(clear_treasure and report.get('stage')=='wait_open')
                if treasure or waiting:
                    if self.policy.news_daily_enabled:
                        world.news_task_hold=True
                    world.treasure_actions[identity] = [c.command for c in treasure]
                    if report.get("cost",0):
                        world.treasure_reserved_gold = max(world.treasure_reserved_gold,report["cost"])
                    guidance.treasure_actions = world.treasure_actions
                    world.pioneer_trade_ids.discard(identity)
                    candidates = [c for c in candidates if not (c.actor==identity and c.command.get('action')=='acceptTask')]
                    if task_choice and task_choice.get('actor')==identity:task_choice=None
                    candidates.extend(treasure)
            helpers = {i:j for i,j in build_jobs.items() if j.get('helper')}
            if helpers:
                helper_choices = economy.ready_construction(world,clock,self.rules,self.policy,
                    min(deadline,time.monotonic()+.04),jobs=helpers)
                helper_choices = [c for c in draft.filter_failures(helper_choices,world.round)
                                  if c.actor in helpers and guidance.permit(c)]
                for identity in helpers:
                    commands = [c.command for c in helper_choices if c.actor==identity]
                    if commands:
                        guidance.work_plans[identity] = dict(owner='mine_batch',
                            phase='harvest' if helpers[identity].get('helper_collect') else 'build',commands=commands)
                candidates.extend(helper_choices)
            market_excluded = set(world.wall_rebuild_actions) | {task_actor} | clearing_ids | set(draft.external_gate.commands) | set(world.treasure_actions) | set(helpers) | set(guidance.work_plans)
            if world.six_task_priority:market_excluded.add(draft.night_roster.p)
            if draft.external_gate.stage.startswith('BACKUP_'):
                market_excluded.update(u.id for u in world.movers)
            if draft.night_roster.traffic:
                market_excluded.update(draft.night_roster.traffic.get(k) for k in ('traveller','blocker'))
            market_excluded.update(c.actor for c in candidates if c.command.get('action')=='acceptTask')
            if draft.tasks.active:
                market_excluded.add(draft.tasks.active.actor)
            if draft.tasks.accept_pending:
                market_excluded.add(draft.tasks.accept_pending.get('actor'))
            if task_choice and task_choice.get('selected'):
                market_excluded.add(task_choice['actor'])
            funding.release_busy(world,market_excluded)
            from .opponent import next_wave_window, SUMMONS, summon_target_status
            summon_window=next_wave_window(clock)
            world.summon_use_remaining=(draft.opponent.remaining if summon_window and clock.phases=={'day'}
                                        and summon_target_status(world)=='alive' else 0)
            world.summon_purchase_slots=max(0,world.summon_use_remaining-
                sum(u.inventory[k] for u in world.movers for k in SUMMONS)-
                sum(p['num'] for p in draft.opponent.pending_buys.values()))
            market = draft.sunset_market.prepare(world,clock,self.rules,self.policy,guidance,build_jobs,
                market_excluded,min(deadline,time.monotonic()+.30))
            candidates.extend(draft.filter_failures(market,world.round))
            recovery_targets = draft.recovery.targets(world, self.rules, self.policy)
            from .night_roles import permits
            from .validation import check_action, Verdict
            adjacent_recovery=draft.recovery.adjacent_night(world,clock,recovery_targets,
                [c for c in candidates+guidance.candidates if guidance.permit(c)],task_actor)
            adjacent_recovery=[c for c in draft.filter_failures(adjacent_recovery,world.round)
                if guidance.permit(c) and permits(world,clock,c)
                and check_action(world,clock,self.rules,c.actor,c.command,
                    task_actor=task_actor,task_moves=guidance.task_moves,
                    allow_task_control=guidance.allow_task_control).verdict==Verdict.VALID]
            candidates.extend(adjacent_recovery)
            world.executable_base_rescue_actors={c.actor for c in candidates
                if c.command.get('action')=='use' and c.command.get('name','').startswith('StationUpgradeVoucher')
                and guidance.permit(c) and permits(world,clock,c)
                and check_action(world,clock,self.rules,c.actor,c.command,
                    task_actor=task_actor,task_moves=guidance.task_moves,
                    allow_task_control=guidance.allow_task_control).verdict==Verdict.VALID}
            urgent_upgrades = draft.filter_failures(economy.procurement.urgent_gatling_upgrades(
                world,self.policy,self.rules,task_actor,priority_ids=recovery_targets),world.round)
            # An exclusive commitment must be executable under this frame's
            # duty rules before it can suppress the checked return incumbent.
            urgent_upgrades = [c for c in urgent_upgrades
                               if permits(world, clock, c)
                               and (guidance.duty_permit is None or guidance.duty_permit(c) is not False)
                               and (c.actor not in guidance.roster_transit_actions or guidance.permit(c))
                               and check_action(world, clock, self.rules, c.actor, c.command,
                                   task_actor=task_actor, task_moves=guidance.task_moves,
                                   allow_task_control=guidance.allow_task_control).verdict == Verdict.VALID]
            for c in urgent_upgrades:
                guidance.urgent_upgrades[c.actor] = c.command
                target = position(c.command['targetPos'][0])
                guidance.upgrading_guns.update(u.id for u in world.weapons if u.pos==target)
            candidates.extend(urgent_upgrades)
            # Check the return/triage incumbent before procurement or other
            # optional preparation can fail. Failed actions are excluded here
            # as well as in the final selection.
            if guidance.candidates or guidance.return_routes or guidance.blocked_moves or guidance.urgent_upgrades:
                early = [c for c in draft.filter_failures(candidates + guidance.candidates, world.round)
                         if guidance.permit(c)]
                incumbent = select(world, clock, self.rules, self.policy, early, time.monotonic()+0.03,
                                   task_actor=task_actor, task_moves=guidance.task_moves,
                                   allow_task_control=guidance.allow_task_control,
                                   incumbent=[c for c in incumbent.selected if guidance.permit(c)])
                fallback = incumbent.response
            upgrade_plans = {}
            upgrade_candidates = economy.procurement.propose(world, self.policy, min(deadline, time.monotonic()+0.08),
                                                              task_actor, plans=upgrade_plans, priority_ids=recovery_targets, rules=self.rules)
            trades = draft.filter_failures(pioneer_trade.candidates(world, clock, self.policy, upgrade_plans,
                min(deadline, time.monotonic()+.04)), world.round)
            trades = [c for c in trades if guidance.permit(c)]
            for c in trades:
                guidance.day_actions.setdefault(c.actor, []).append(c.command)
            candidates.extend(trades)
            recovery = draft.recovery.ready(world, clock, self.policy, upgrade_plans, recovery_targets,
                                            guidance.operator_stands, min(deadline, time.monotonic()+0.08))
            recovery = [c for c in draft.filter_failures(recovery, world.round) if guidance.permit(c)]
            for candidate in recovery:
                guidance.recovery_actions.setdefault(candidate.actor, []).append(candidate.command)
            candidates.extend(recovery)
            funded, funded_budgets = draft.day_schedule.funded_delivery(world,clock,self.policy,build_jobs,
                guidance.operator_stands,upgrade_plans,min(deadline,time.monotonic()+.08))
            for c in draft.filter_failures(funded,world.round):
                if c.actor in guidance.recovery_actions or c.actor in guidance.urgent_upgrades or not guidance.permit(c):continue
                guidance.funded_actions.setdefault(c.actor,[]).append(c.command)
                c.utility=90
                candidates.append(c)
            scheduled = draft.day_schedule.candidates(world,clock,self.policy,build_jobs,guidance.operator_stands,
                upgrade_plans,{task_actor}|set(guidance.recovery_actions)|set(guidance.urgent_upgrades)|set(guidance.funded_actions)|clearing_ids|set(world.sunset_actions),
                min(deadline,time.monotonic()+.12))
            if getattr(world,'economy_first',False):
                for c in scheduled:
                    if c.actor not in guidance.recovery_actions and c.actor not in guidance.urgent_upgrades:
                        guidance.funded_actions.setdefault(c.actor,[]).append(c.command)
            scheduled = [c for c in draft.filter_failures(scheduled,world.round) if guidance.permit(c)]
            for c in scheduled:guidance.day_actions.setdefault(c.actor,[]).append(c.command)
            candidates.extend(scheduled)
            ready = economy.ready_construction(world, clock, self.rules, self.policy, min(deadline, time.monotonic()+0.08), jobs=build_jobs)
            if world.seal_cells and len(economy.battery.missing_walls(world,self.rules)) == 1:
                for c in ready:
                    if c.command.get("action") == "build" and c.command.get("name") == "wall":
                        guidance.seal_builds[c.actor] = c.command
                        c.utility = 1000
                        c.reason = "observed gate permission: seal last wall before night"
            if getattr(world,'economy_first',False):
                for c in ready:
                    if c.actor not in guidance.day_actions and c.actor not in guidance.recovery_actions and c.actor not in guidance.urgent_upgrades:
                        guidance.funded_actions.setdefault(c.actor,[]).append(c.command)
            ready = [c for c in draft.filter_failures(ready, world.round) if guidance.permit(c)]
            for candidate in ready:
                guidance.construction_actions.setdefault(candidate.actor, []).append(candidate.command)
            candidates.extend(ready)
            upgrades = economy.procurement.ready_upgrades(world, clock, self.policy, min(deadline, time.monotonic()+0.08), task_actor, plans=upgrade_plans, rules=self.rules)
            upgrades = [c for c in draft.filter_failures(upgrades, world.round) if guidance.permit(c)]
            for candidate in upgrades:
                guidance.upgrade_actions.setdefault(candidate.actor, []).append(candidate.command)
            candidates.extend(upgrades)
            excluded = {task_actor}
            if draft.tasks.active is not None:
                excluded.add(draft.tasks.active.actor)
            if draft.tasks.accept_pending:
                excluded.add(draft.tasks.accept_pending.get('actor'))
            medical = draft.medical.candidates(world, clock, self.rules, self.policy,
                                               min(deadline, time.monotonic()+0.08), guidance, excluded,
                                               construction_jobs=build_jobs)
            medical = draft.filter_failures(medical, world.round)
            # A validated treatment trip supersedes optional economic steps.
            # Register its exact commands before rechecking final permissions.
            for candidate in medical:
                guidance.medical_actions.setdefault(candidate.actor, []).append(candidate.command)
            medical = [c for c in medical if guidance.permit(c)]
            guidance.medical_actions = {
                identity: [c.command for c in medical if c.actor == identity]
                for identity in {c.actor for c in medical}}
            candidates.extend(medical)
            supply = world.duty_budget.run('repair_supply', lambda budget_end: draft.repair_supply.candidates(
                world, clock, self.rules, self.policy, budget_end, guidance,
                excluded | set(guidance.medical_actions) | set(world.sunset_actions),
                construction_jobs=build_jobs,
                retry_rounds={identity:failure[1]+min(4,failure[0]+1)
                    for (identity,signature),failure in draft.failed.items()
                    if signature==fingerprint(dict(action='buy',name='WallFixer',num=1))}),
                min(deadline,time.monotonic()+.04))
            supply = draft.filter_failures(supply, world.round)
            for candidate in supply:
                guidance.repair_supply_actions.setdefault(candidate.actor,[]).append(candidate.command)
            candidates.extend(supply)
            trips = draft.economic_routes.candidates(world, clock, self.policy, economy.construction_reservations(world, self.rules, jobs=build_jobs),
                                                      min(deadline, time.monotonic()+0.08), task_actor)
            trips = [c for c in draft.filter_failures(trips, world.round) if guidance.permit(c)]
            for candidate in trips:
                guidance.economic_route_actions.setdefault(candidate.actor, []).append(candidate.command)
                guidance.economic_route_goals[candidate.actor] = candidate.route_goal
            candidates.extend(trips)
            candidates.extend(guidance.candidates)
            candidates = [c for c in candidates if guidance.permit(c)]
            # Preserve a checked triage incumbent even if later planning runs out.
            if guidance.candidates or guidance.return_routes or guidance.blocked_moves or guidance.construction_actions or guidance.upgrade_actions or guidance.recovery_actions or guidance.economic_route_actions or guidance.medical_actions or guidance.day_actions or guidance.urgent_upgrades:
                incumbent = select(world, clock, self.rules, self.policy,
                                   draft.filter_failures(candidates, world.round), time.monotonic()+0.03,
                                   task_actor=task_actor, task_moves=guidance.task_moves,
                                   allow_task_control=guidance.allow_task_control,
                                   incumbent=[c for c in incumbent.selected if guidance.permit(c)])
                fallback = incumbent.response
            module_errors = []
            weights = combat.threat_weights(world)
            if guidance.base_critical:
                # Raise threat value once per target, not once per emitted
                # action: a flat crisis bonus rewards redundant consumables.
                weights = {identity: weight*4 for identity, weight in weights.items()}
            for name, planner in (("combat", lambda: combat.propose(world, clock, self.rules, deadline, None if guidance.allow_task_control else task_actor,
                                                                   base_fire_enabled=self.policy.base_fire_enabled and self.policy.joint_fire_enabled,
                                                                   rocket_diversity_enabled=self.policy.rocket_diversity_enabled)),
                                  ("defence", lambda: draft.defence.candidates(world, clock, self.policy, deadline, task_actor,
                                                                              incumbent.selected, guidance)),
                                  ("economy", lambda: economy.propose(world, clock, self.rules, self.policy, deadline, task_actor, guidance.operator_stands, upgrade_candidates=upgrade_candidates, build_jobs=build_jobs, task_choice=task_choice)),
                                  ("opponent", lambda: draft.opponent.candidates(world, clock, self.policy, task_actor, deadline))):
                if time.monotonic() >= deadline:
                    break
                try:
                    proposed = planner()
                    candidates.extend(c for c in proposed if guidance.permit(c))
                    if name == "combat" and proposed:
                        improved = select(world, clock, self.rules, self.policy,
                                          draft.filter_failures(candidates, world.round),
                                          min(deadline, time.monotonic()+0.08), task_actor=task_actor,
                                          task_moves=guidance.task_moves, allow_task_control=guidance.allow_task_control,
                                          weights=weights, incumbent=incumbent.selected)
                        incumbent = improved
                        fallback = improved.response
                except Exception as exc:
                    module_errors.append({"module": name, "error": type(exc).__name__})
                    LOG.exception("planner failed: %s", name)
            if self.policy.news_hold_enabled and world.gold is not None and world.gold > self.policy.reserve_gold:
                for candidate in candidates:
                    if candidate.command["action"] == "sell" and draft.intelligence.hold_ore(candidate.command["name"], clock):
                        if candidate.actor in world.pioneer_trade_ids or candidate.actor in world.sunset_actions:
                            continue
                        actor = world.ours[candidate.actor]
                        if actor.capacity and actor.backpack is not None and len(actor.backpack) < actor.capacity*0.8:
                            candidate.utility = -0.1
            candidates = draft.filter_failures(candidates, world.round)
            if not self.policy.joint_fire_enabled:
                for candidate in candidates:
                    for identity, damage in candidate.damage.items():
                        robot = world.robots[identity]
                        candidate.utility += weights.get(identity, 1) * (min(robot.health, damage) + (12 if damage >= robot.health else 0))
                    candidate.damage = {}
                    candidate.utility += sum(weights.get(identity, 1) * 15 for identity in sorted(candidate.suppression))
                    candidate.suppression = frozenset()
            incumbent_candidates = draft.filter_failures(incumbent.selected, world.round)
            decision = select(world, clock, self.rules, self.policy, candidates, deadline,
                              task_actor=task_actor, weights=weights,
                              task_moves=guidance.task_moves, allow_task_control=guidance.allow_task_control,
                              incumbent=incumbent_candidates, summon_remaining=draft.opponent.remaining,
                              funding_topup=True,
                              admission=lambda c: guidance.permit(c)
                                                  and not draft.backed_off(c.actor, c.command, world.round))
            joint_report = {"status": "inactive"}
            if self.policy.joint_lookahead_enabled:
                try:
                    improved, joint_report, predictions = joint_lookahead.improve(
                        world, clock, self.rules, self.policy, decision, candidates,
                        min(deadline, time.monotonic()+self.policy.joint_lookahead_seconds),
                        memory=draft.joint_risk, permit=guidance.permit,
                        filter_candidates=lambda cs: draft.filter_failures(cs, world.round),
                        task_actor=task_actor,
                        task_cells=draft.tasks.active.cells if draft.tasks.active else (),
                        task_moves=guidance.task_moves, allow_task_control=guidance.allow_task_control,
                        weights=weights)
                    decision = improved
                    if predictions:
                        draft.joint_risk.commit(world, decision.selected, predictions)
                except Exception as exc:
                    joint_report = {"status": "failed", "error": type(exc).__name__}
                    module_errors.append({"module": "joint_lookahead", "error": type(exc).__name__})
                    LOG.exception("joint rollout failed; keeping current validated selection")
            draft.task_approach = {}
            if task_choice and task_choice.get('selected') and approach_key is not None:
                identity = task_choice['actor']
                if any(c.actor == identity and c.reason == 'approach reward-ranked feasible task'
                       for c in decision.selected):
                    draft.task_approach = dict(actor=identity, key=approach_key, round=world.round)
            draft.tasks.finalize(world, decision.response)
            draft.intelligence.finalize(world, clock, draft, decision.response, self.policy)
            draft.opponent.finalize(decision.response, world)
            draft.defence.finalize(world, decision.response)
            draft.guard_stock.finalize(world,clock,decision.response)
            draft.medical.finalize(world, decision.response)
            draft.external_gate.finalize(world,decision.response)
            draft.night_clear.finalize(world,decision.response)
            draft.repair.finalize(world,decision.response)
            draft.wall_rebuild.finalize(world,decision.response)
            draft.repair_supply.finalize(world,decision.response)
            draft.exterior_repair.finalize(world,decision.response)
            draft.sunset_market.finalize(world,decision.response)
            draft.risk.finalize(world, clock, decision.response)
            draft.economic_routes.finalize(world, decision.selected, self.policy)
            validate_response(decision.response)
            # Serialize/check before publishing memory, then store independent copies.
            response = deepcopy(decision.response)
            draft.last_round = world.round
            draft.last_response = deepcopy(response)
            draft.cache[(world.round, digest)] = deepcopy(response)
            while len(draft.cache) > self.policy.cache_entries:
                draft.cache.popitem(last=False)
            self.sessions[key] = draft
            self.active_key = key
            self.sessions.move_to_end(key)
            while len(self.sessions) > self.policy.session_entries:
                self.sessions.popitem(last=False)
            record = {"round": world.round, "epoch": draft.epoch, "side": world.side,
                      "elapsed_ms": (time.monotonic()-start)*1000, "actions": len(response["roleCommandMap"]),
                      "selected": [{"actor": c.actor, "reason": c.reason} for c in decision.selected],
                      "rejected": decision.rejected, "module_errors": module_errors,
                      "selection_report": decision.report,
                      "warnings": world.warnings, "origin": draft.origin,
                      "rule_observation_differences": self.rules.observation_differences(world),
                      "wall_health_levels": draft.wall_health.levels,
                      "build_region_status": self.rules.build_region_status(world),
                      "risk_scenarios": guidance.observations, "operator_stands": guidance.operator_stands,
                      "operator_plan_status": guidance.operator_plan_status,
                      "construction_site_clearance": sorted(guidance.site_clear_actions),
                      "construction_site_reservations": sorted(getattr(world,'operator_excluded_cells',set())),
                      "task_choice": (None if task_choice is None else {
                          'actor':task_choice['actor'],'reason':task_choice['reason'],
                          'selected':task_choice['selected']}),
                      "pioneer_trade": {"actors":sorted(world.pioneer_trade_ids), "reason":world.pioneer_trade_reason},
                      "task_lifecycle": world.task_lifecycle.snapshot(world) if hasattr(world, 'task_lifecycle') else {},
                      "sunset_market": draft.sunset_market.diagnostic,
                      "llm_channel":dict(draft.llm_channel.pending),
                      "wall_upgrade_orders":dict(
                          due=[dict(id=u.id,position=u.pos,level=u.level) for u in wall_policy.daily_upgrade_targets(world)],
                          grants=[r for r in world.funding_plan if r['purpose']=='wall_upgrade'],
                          gold=world.gold,held={u.id:{k:v for k,v in u.inventory.items() if v and k.startswith('WallUpgradeVoucher')}
                              for u in world.movers if u.backpack is not None}),
                      "treasure": draft.intelligence.diagnostic,
                      "rumour_llm": {"used":draft.tasks.budget.attempts,"status":draft.intelligence.llm_status,
                                     "clues":len(draft.intelligence.clues), 'cycle':draft.intelligence.cycle.number,
                                     'cycle_state':draft.intelligence.cycle.status,
                                     'map_treasure':draft.intelligence.terminal,
                                     **draft.intelligence.llm_diagnostic},
                      "return_recovery": return_recovery_report,
                      "return_routes": guidance.return_routes,
                      "navigation_retry_exclusions": {u.id:sorted(world.navigation_avoided.get(u.pos, set())) for u in world.movers},
                      "movement_retry_windows": draft.move_retry_windows(world.round),
                      "construction_jobs": build_jobs,
                      "wall_assistance": getattr(world,'wall_assistance',{}),
                      "work_status":{u.id:{"ore":[u.inventory[k] for k in ("stone","iron","copper")],
                          "job":({"name":build_jobs[u.id]["name"],"gate":build_jobs[u.id].get("gate",False),"stock":build_jobs[u.id].get("stock_target"),"build_steps":build_jobs[u.id].get("construction_steps"),"deferred":build_jobs[u.id].get("defer_build")} if u.id in build_jobs else None),
                          "slack":guidance.return_routes.get(u.id,{}).get("slack_before_buffer"),
                          "schedule":draft.day_schedule.diagnostic.get(u.id),
                          "funded":funded_budgets.get(u.id),
                          "idle":("return_hold" if guidance.return_routes.get(u.id,{}).get("due") else "no_selected_feasible_work")
                              if u.id not in response['roleCommandMap'] and not any(c.get('controllerId')==u.id for c in response['roleCommandMap'].values()) else None,
                          "trip":draft.economic_routes.diagnostic.get(u.id,{}).get("goal"),
                          "upgrade":({k:upgrade_plans[u.id][k] for k in ("name","steps","stage")} if u.id in upgrade_plans else None)}
                          for u in world.movers if u.kind=="worker"},
                      "battery_plan": world.battery_plan,
                      "external_gate":draft.external_gate.diagnostic,
                      "night_clear":draft.night_clear.diagnostic,
                      "maintenance_funding":getattr(world,'maintenance_funding',{}),
                      "duty_budget":world.duty_budget.diagnostic(),
                      "exterior_evasion":evasion_report,
                      "work_plans":guidance.work_plans,
                      "permission_rejections":guidance.permission_rejections,
                      "repair":draft.repair.diagnostic,
                      "wall_rebuild":draft.wall_rebuild.diagnostic,
                      "funding_plan":world.funding_plan,
                      "work_rejections":getattr(world,'work_rejections',[]),
                      "work_events":getattr(world,'work_events',[]),
                      "procurement_works":[dict(work_id=w.work_id,actor=w.actor,purpose=w.purpose,
                          status=w.status,step=w.step,revision=w.revision,deadline=w.deadline,
                          blocked_by=w.blocked_by,reason=w.reason,
                          last_confirmed_progress=w.last_confirmed_progress)
                          for w in getattr(world,'procurement_works',[])],
                      "treasure_inventory":draft.intelligence.inventory_report(world),
                      "guard_stock":getattr(world,'guard_stock_report',{}),
                      "guard_funding":getattr(world,'guard_funding_report',{}),
                      "six_task_deadline":getattr(world,"six_task_deadline",{}),
                      "role_handoff":getattr(world,"role_handoff",{}),
                      "effective_defenders":dict(assigned=len(world.night_defenders),
                          on_station=sum(bool(getattr(world,'task_side_plan',None)) and u.id in world.night_defenders and u.pos in defence_duties.stands(world,u.id) for u in world.movers),
                          repair_capable=sum(bool(getattr(world,'task_side_plan',None)) and u.id==draft.night_roster.w and u.inventory['WallFixer']>0
                              and u.pos in defence_duties.stands(world,u.id) for u in world.movers)),
                      "repair_supply":draft.repair_supply.diagnostic,
                      "exterior_repair":draft.exterior_repair.diagnostic,
                      "wall_service":{f'{p[0]},{p[1]}':v for p,v in draft.wall_service.sites.items()},
                      "caretaker_stand":defence_duties.service_diagnostic(world,guidance.operator_stands),
                      "day_access":world.access_diagnostic,
                      "night_roster": {"w":draft.night_roster.w, "p":draft.night_roster.p,
                          "m":draft.night_roster.m, "defenders":sorted(world.night_defenders),
                          "substituting":draft.night_roster.substituting,
                          "handoff_requested":draft.night_roster.handoff_requested,
                          "yielding":sorted(world.roster_yielding),
                          "exit_pending":dict(draft.night_roster.exit_pending),
                          "traffic": ({"traveller":draft.night_roster.traffic['traveller'],
                              "blocker":draft.night_roster.traffic['blocker'],
                              "stand":draft.night_roster.traffic['stand'],
                              "goals":sorted(draft.night_roster.traffic['goals']),
                              "gate_owned":bool(draft.night_roster.traffic.get('gate_owned'))}
                              if draft.night_roster.traffic else None)},
                      "task_side_layout": ({"status":draft.task_layout.status,
                          **{k:draft.task_layout.plan.get(k) for k in (
                              'layout_mode','layout_revision','permanent_openings','required_wall_cells','c_stands',
                              'c','w','gate','primary_task','task_round_trips','gate_detour_rounds',
                              'suggested_gate_worker','worker_gate_steps','front_repair_coverage',
                              'exposure_upper','exposure_unknown','feasible_candidates','rejected_candidates')}}
                          if draft.task_layout.plan else {"status":draft.task_layout.status,
                              "rejected_candidates":getattr(world,'task_layout_rejections',{})}),
                      "gatling_upgrades": {u.id:{"hp":u.health,"level":u.level,
                          "state":economy.procurement.gatling_upgrade_status(world,u,self.policy,self.rules)[0],
                          "threshold":economy.procurement.gatling_upgrade_status(world,u,self.policy,self.rules)[1]}
                          for u in world.weapons if u.kind=='gatling' and u.level in (1,2)},
                      "wall_supply": {"need":len(economy.battery.missing_walls(world,self.rules)),
                          "goal":len(world.wall_targets) if world.wall_targets is not None else self.rules.wall_limit,
                          "ports":sorted(world.firing_ports),
                          "stage":getattr(world,"wall_stage",None),
                          "facing":getattr(world,"wall_direction_source",None),
                          "assistance":getattr(world,'wall_assistance',{}),
                          "stone":{u.id:u.inventory["stone"] if u.backpack is not None else None for u in world.movers if u.kind=="worker"},
                          "quotas":{i:j.get("stock_target") for i,j in build_jobs.items() if j["name"]=="wall"},
                          "mines":sorted(world.zones.get("stone", ()))[:8],
                          "gaps":sorted(economy.battery.missing_walls(world,self.rules)),
                          "seal_ready":bool(world.seal_cells)},
                      "gun_status":combat.fire_status(world,clock,self.rules,response,candidates),
                      "night_fire_capacity":forage_admission.fire_summary(world,response),
                      "feedback_counts": dict(draft.feedback_counts),
                      "construction_commitments": sorted(guidance.construction_actions),
                      "upgrade_commitments": sorted(guidance.upgrade_actions),
                      "urgent_upgrade_commitments": sorted(guidance.urgent_upgrades),
                      "base_recovery": deepcopy(draft.recovery.diagnostic),
                      "funded_upgrades": funded_budgets,
                      "recovery_commitments": sorted(guidance.recovery_actions),
                      "economic_routes": deepcopy(draft.economic_routes.diagnostic),
                      "opponent": deepcopy(draft.opponent.diagnostic),
                      "defence_procurement": deepcopy(draft.defence.diagnostic),
                      "medical_supply": deepcopy(draft.medical.diagnostic),
                      "joint_lookahead": joint_report,
                      "horizon_risk": guidance.horizon_risk, "risk_comparison": draft.risk.last_comparison,
                      "empty_map_officially_verified": self.rules.empty_actions_verified}
            try:
                self.telemetry.append(record)
                self._diagnostic('news_analysis',team=(world.raw.get('teamOur') or {}).get('teamId'),
                    cycle_id=draft.intelligence.cycle.number,status=draft.intelligence.llm_status,
                    diagnostic=draft.intelligence.llm_diagnostic,fields=draft.intelligence.field_state,
                    rejected=draft.intelligence.invalid_candidates,suspended=draft.intelligence.plans_suspended,
                    treasures=draft.intelligence.treasures,purchases=draft.intelligence.preparations)
                if getattr(world,'task_admission',None):
                    record['task_admission']=world.task_admission
                    self._diagnostic('task_admission',team=world.side,candidates=world.task_admission,
                        selected=selected_task.get('goal') if selected_task else None,deadline=getattr(world,'six_task_deadline',{}))
                record['wall_delivery_waits']=getattr(world,'wall_delivery_waits',{})
                self._diagnostic("decision", **record)
                if self.diagnostics is not None:
                    checks = []
                    for identity, command in response["roleCommandMap"].items():
                        if command["action"] != "attack":
                            continue
                        gun = world.ours[identity]
                        controller = world.ours[command["controllerId"]]
                        checks.append({"weapon":identity, "controller":controller.id, "range":gun.attack_range,
                                       "controller_distance":distance(gun.pos,controller.pos), "cooldown":gun.cooldown,
                                       "level":gun.level, "target_distances":[distance(gun.pos,position(p)) for p in command["targetPos"]]})
                    self._diagnostic("attack_checks", checks=checks, metric="chebyshev")
                if self.diagnostics is not None:
                    task = draft.tasks.active
                    self._diagnostic("task_state", active={name: getattr(task, name, None) for name in
                        ("key", "actor", "phase", "seq", "accept_round", "activation_round", "timeout",
                         "llm_pending", "sandbox_pending", "command_plan", "answer", "submitted", "events", "environment",
                         "uncertain_operations", "executions", "workflow_id", "workflow_results",
                         "statement_names", "statement_path", "statement_ready", "statement_empty", "locate_attempts",
                         "evidence", "diagnostic_events", "metrics")} if task else None,
                        accept_pending=draft.tasks.accept_pending, closed=draft.tasks.closed,
                        budget=draft.tasks.budget)

            except Exception:
                pass  # Optional observations must not invalidate an already committed response.
            return response
        except Exception:
            self._diagnostic("outcome", value="fallback")
            LOG.exception("turn failed; returning structurally checked incumbent")
            validate_response(fallback)
            return deepcopy(fallback)
        finally:
            if acquired:
                self.lock.release()
