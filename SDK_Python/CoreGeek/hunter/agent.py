"""Serial, bounded turn transaction with a validated current-state incumbent."""
from collections import OrderedDict, deque
from dataclasses import replace
from copy import deepcopy
import logging
from pathlib import Path
import threading
import time

from . import combat, economy, director, joint_lookahead, task_schedule, site_clearance, exterior_evasion, forage_admission
from .arbitration import select
from .protocol import parse_request, fingerprint, empty_response, validate_response, distance, position
from .rules import Rules, Policy, Clock
from .state import Session
from .duty_budget import DutyBudget

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
            clock = draft.reconcile(world)
            world.strategy_policy = self.policy
            from . import pioneer_trade
            pioneer_trade.prepare(world, clock)
            task_actor = draft.task_actor(world)
            fallback = self._early_base(world, clock, task_actor, draft).response
            draft.task_layout.prepare(world, self.rules, self.policy,
                                      min(start + self.policy.planning_seconds, time.monotonic() + .04))
            world.duty_budget = DutyBudget(start+self.policy.planning_seconds)
            draft.night_roster.prepare(world)
            world.night_foraging_enabled=self.policy.night_foraging_enabled
            economy.prepare_wall_cycle(world, clock, self.rules, self.policy)
            daily = draft.sunset_market.caretaker_day
            world.worker_close_requested = bool(self.policy.pioneer_rotation_enabled
                and daily.day == clock.day and daily.phase in {'close','use'}
                and not (world.phase_task or draft.tasks.active or draft.tasks.accept_pending))
            world.worker_upgrade_use_steps = daily.use_budget if world.worker_close_requested else 0
            gate_candidates=world.duty_budget.run('gate', lambda budget_end: draft.external_gate.prepare(
                world,clock,self.rules,self.policy,budget_end,
                task_busy=bool(task_actor or draft.tasks.active or draft.tasks.accept_pending),defer_regular_night=True),
                min(start+self.policy.planning_seconds,time.monotonic()+.06))
            evasion_report={'status':'emergency gate plan active'}
            if not draft.external_gate.emergency_active and not draft.external_gate.deferred_night:
                evasion,evasion_report=world.duty_budget.run('exterior_evasion', lambda budget_end:
                    exterior_evasion.propose(world,clock,budget_end,trapped=draft.external_gate.stage=='RETURN_BLOCKED'), min(start+self.policy.planning_seconds,time.monotonic()+.01))
                if evasion:
                    identity=evasion[0].actor
                    gate_candidates=[c for c in gate_candidates if c.actor!=identity]+evasion
                    draft.external_gate.commands[identity]=[c.command for c in evasion]
                    world.night_forage_commands.pop(identity,None)
            build_jobs = economy.construction_jobs(world, self.rules, self.policy)
            build_jobs = draft.day_schedule.division.assign(world,clock,self.rules,self.policy,build_jobs,time.monotonic()+.25)
            if draft.external_gate.commands:
                build_jobs={i:j for i,j in build_jobs.items() if i not in draft.external_gate.commands}
            immediate = draft.filter_failures(economy.immediate(world, self.rules, task_actor, jobs=build_jobs, policy=self.policy), world.round)
            incumbent = self._base(world, clock, task_actor, immediate)
            fallback = incumbent.response
            candidates = list(immediate)
            deadline = start + self.policy.planning_seconds
            task_choice = task_schedule.choose(world,clock,self.policy,min(deadline,time.monotonic()+.04),draft.tasks.timing)
            if (self.policy.pioneer_rotation_enabled and clock.phases=={'day'} and task_choice
                    and task_choice.get('selected') and not draft.tasks.active and not draft.tasks.accept_pending):
                selected=task_choice['selected'];offer=selected.get('task',{})
                waiting=offer.get('isValid') is not True or offer.get('coldDownRounds',0)>0
                demands,rank,restricted,_=economy.procurement.upgrade_demand(world,self.policy,rules=self.rules)
                funded=any((not restricted or d['rank']==rank) and d['name'] in world.shop
                    and (world.gold or 0)>=world.shop[d['name']] for d in demands.values())
                if waiting and not funded:
                    from .supply_basket import requirements
                    funded=any(0 < world.shop.get(r['name'],0) <= (world.gold or 0)
                               for r in requirements(world,self.rules,self.policy))
                    actor=world.ours.get(task_choice['actor'])
                    funded |= bool(actor and actor.backpack is not None and actor.capacity is not None
                        and len(actor.backpack)<actor.capacity and any(
                            actor.inventory[name]<limit and 0 < world.shop.get(name,0) <= (world.gold or 0)
                            for name,limit in (('Medicine',2),('Bomb',60))))
                if draft.sunset_market.upgrade_owner==task_choice['actor'] or waiting and funded:
                    task_choice=dict(actor=task_choice['actor'],selected=None,candidates=[],
                        reason='funded upgrade checkout precedes waiting for a future task')
            from .night_roles import admit_task_departure
            task_choice = world.duty_budget.run('task_handoff', lambda budget_end:
                admit_task_departure(world, clock, task_choice, budget_end), min(deadline,time.monotonic()+.02))
            guidance = director.propose(world, clock, task_actor, draft.tasks.active, min(deadline, time.monotonic()+0.12), self.policy, draft.risk,
                                        failed_steps=draft.failed_move_steps(world) if self.policy.return_detour_enabled else None)
            from . import return_recovery
            recovery_moves, return_recovery_report = return_recovery.propose(
                world,clock,self.policy,min(deadline,time.monotonic()+.04))
            if recovery_moves:
                for c in recovery_moves:
                    guidance.candidates=[old for old in guidance.candidates if old.actor!=c.actor]
                    guidance.candidates.append(c)
                    guidance.roster_transit_actions[c.actor]=[c.command]
                    build_jobs.pop(c.actor,None)
            candidates.extend(draft.tasks.candidates(world, choice=task_choice))
            world.pioneer_trade_stands = guidance.operator_stands
            repairs = world.duty_budget.run('repair', lambda budget_end: draft.repair.prepare(
                world, clock, self.rules, self.policy, budget_end, draft.tasks.active),
                min(deadline,time.monotonic()+.04))
            if self.policy.pioneer_rotation_enabled and clock.phases=={'night'}:
                for c in repairs:
                    guidance.repair_actions.setdefault(c.actor,[]).append(c.command)
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
                    exterior_evasion.propose(world,clock,budget_end,trapped=draft.external_gate.stage=='RETURN_BLOCKED'),min(deadline,time.monotonic()+.01))
                if evasion:
                    identity=evasion[0].actor
                    gate_candidates=[c for c in gate_candidates if c.actor!=identity]+evasion
                    draft.external_gate.commands[identity]=[c.command for c in evasion]
                    world.night_forage_commands.pop(identity,None)
                    world.forage_contract=None
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
            world.treasure_actions = {}
            world.treasure_reserved_gold = 0
            if not draft.tasks.active and not draft.tasks.accept_pending:
                world.pioneer_trade_stands = guidance.operator_stands
                treasure = draft.intelligence.candidates(world,clock,self.policy,min(deadline,time.monotonic()+.15))
                treasure = [c for c in draft.filter_failures(treasure,world.round) if guidance.permit(c)]
                report = draft.intelligence.diagnostic
                identity = report.get('actor')
                waiting = (report.get('stage')=='wait_open' and identity not in clearing_ids
                           and identity not in guidance.roster_transit_actions
                           and identity not in draft.external_gate.commands
                           and not guidance.return_routes.get(identity,{}).get('due'))
                if treasure or waiting:
                    world.treasure_actions[identity] = [c.command for c in treasure]
                    if report.get("cost",0):
                        world.treasure_reserved_gold = report["cost"]+self.policy.reserve_gold
                    guidance.treasure_actions = world.treasure_actions
                    world.pioneer_trade_ids.discard(identity)
                    candidates = [c for c in candidates if not (c.actor==identity and c.command.get('action')=='acceptTask')]
                    if task_choice and task_choice.get('actor')==identity:task_choice=None
                    candidates.extend(treasure)
            market_excluded = {task_actor} | clearing_ids | set(draft.external_gate.commands) | set(world.treasure_actions)
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
            market = draft.sunset_market.prepare(world,clock,self.rules,self.policy,guidance,build_jobs,
                market_excluded,min(deadline,time.monotonic()+.15))
            candidates.extend(draft.filter_failures(market,world.round))
            recovery_targets = draft.recovery.targets(world, self.rules, self.policy)
            urgent_upgrades = draft.filter_failures(economy.procurement.urgent_gatling_upgrades(
                world,self.policy,self.rules,task_actor,priority_ids=recovery_targets),world.round)
            from .night_roles import permits
            from .validation import check_action, Verdict
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
                                               min(deadline, time.monotonic()+0.08), guidance, excluded)
            medical = [c for c in draft.filter_failures(medical, world.round) if guidance.permit(c)]
            for candidate in medical:
                guidance.medical_actions.setdefault(candidate.actor, []).append(candidate.command)
            candidates.extend(medical)
            supply = world.duty_budget.run('repair_supply', lambda budget_end: draft.repair_supply.candidates(
                world, clock, self.rules, self.policy, budget_end, guidance,
                excluded | set(guidance.medical_actions) | set(world.sunset_actions)
                | ({identity for identity, job in build_jobs.items() if not job.get('gate')}
                   if self.policy.pioneer_rotation_enabled else set())), min(deadline,time.monotonic()+.04))
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
                              incumbent=incumbent_candidates, summon_remaining=draft.opponent.remaining)
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
            draft.tasks.finalize(world, decision.response)
            draft.intelligence.finalize(world, clock, draft, decision.response, self.policy)
            draft.opponent.finalize(decision.response, world)
            draft.defence.finalize(world, decision.response)
            draft.medical.finalize(world, decision.response)
            draft.external_gate.finalize(world,decision.response)
            draft.repair.finalize(world,decision.response)
            draft.repair_supply.finalize(world,decision.response)
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
                      "warnings": world.warnings, "origin": draft.origin,
                      "rule_observation_differences": self.rules.observation_differences(world),
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
                      "treasure": draft.intelligence.diagnostic,
                      "rumour_llm": {"used":draft.tasks.budget.attempts,"status":draft.intelligence.llm_status,
                                     "clues":len(draft.intelligence.clues), **draft.intelligence.llm_diagnostic},
                      "return_recovery": return_recovery_report,
                      "return_routes": guidance.return_routes,
                      "navigation_retry_exclusions": {u.id:sorted(world.navigation_avoided.get(u.pos, set())) for u in world.movers},
                      "movement_retry_windows": draft.move_retry_windows(world.round),
                      "construction_jobs": build_jobs,
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
                      "duty_budget":world.duty_budget.diagnostic(),
                      "exterior_evasion":evasion_report,
                      "repair":draft.repair.diagnostic,
                      "repair_supply":draft.repair_supply.diagnostic,
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
                          **{k:draft.task_layout.plan[k] for k in (
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
                         "evidence", "diagnostic_events")} if task else None,
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
