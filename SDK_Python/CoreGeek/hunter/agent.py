"""Serial, bounded turn transaction with a validated current-state incumbent."""
from collections import OrderedDict, deque
from dataclasses import replace
from copy import deepcopy
import logging
from pathlib import Path
import threading
import time

from . import combat, economy, director, joint_lookahead
from .arbitration import select
from .protocol import parse_request, fingerprint, empty_response, validate_response, distance, position
from .rules import Rules, Policy, Clock
from .state import Session

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
        if candidates is None:
            candidates = economy.immediate(world, self.rules, task_actor, jobs=economy.construction_jobs(world, self.rules, self.policy), policy=self.policy)
        return select(world, clock, self.rules, self.policy, candidates, time.monotonic()+0.03,
                      task_actor=task_actor)

    def _isolated_response(self, world):
        task_actor = next((u.id for u in world.movers if u.kind == "pioneer" and world.phase_task), None)
        return self._base(world, Clock(world.round, self.rules.round_origin), task_actor).response

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
            task_actor = draft.task_actor(world)
            economy.prepare_wall_cycle(world, clock, self.rules, self.policy)
            build_jobs = economy.construction_jobs(world, self.rules, self.policy)
            immediate = draft.filter_failures(economy.immediate(world, self.rules, task_actor, jobs=build_jobs, policy=self.policy), world.round)
            incumbent = self._base(world, clock, task_actor, immediate)
            fallback = incumbent.response
            candidates = list(immediate)
            candidates.extend(draft.tasks.candidates(world))
            deadline = start + self.policy.planning_seconds
            guidance = director.propose(world, clock, task_actor, draft.tasks.active, min(deadline, time.monotonic()+0.12), self.policy, draft.risk,
                                        failed_steps=draft.failed_move_steps(world) if self.policy.return_detour_enabled else None)
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
                    if not job.get("gate") or not actor.inventory["stone"]:
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
            recovery_targets = draft.recovery.targets(world, self.rules, self.policy)
            urgent_upgrades = draft.filter_failures(economy.procurement.urgent_gatling_upgrades(
                world,self.policy,self.rules,task_actor,priority_ids=recovery_targets),world.round)
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
            recovery = draft.recovery.ready(world, clock, self.policy, upgrade_plans, recovery_targets,
                                            guidance.operator_stands, min(deadline, time.monotonic()+0.08))
            recovery = [c for c in draft.filter_failures(recovery, world.round) if guidance.permit(c)]
            for candidate in recovery:
                guidance.recovery_actions.setdefault(candidate.actor, []).append(candidate.command)
            candidates.extend(recovery)
            ready = economy.ready_construction(world, clock, self.rules, self.policy, min(deadline, time.monotonic()+0.08), jobs=build_jobs)
            if world.seal_cells and len(economy.battery.missing_walls(world,self.rules)) == 1:
                for c in ready:
                    if c.command.get("action") == "build" and c.command.get("name") == "wall":
                        guidance.seal_builds[c.actor] = c.command
                        c.utility = 1000
                        c.reason = "all roles inside: seal last wall before night"
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
            if guidance.candidates or guidance.return_routes or guidance.blocked_moves or guidance.construction_actions or guidance.upgrade_actions or guidance.recovery_actions or guidance.economic_route_actions or guidance.medical_actions or guidance.urgent_upgrades:
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
                                                                   base_fire_enabled=self.policy.base_fire_enabled and self.policy.joint_fire_enabled)),
                                  ("defence", lambda: draft.defence.candidates(world, clock, self.policy, deadline, task_actor,
                                                                              incumbent.selected, guidance)),
                                  ("economy", lambda: economy.propose(world, clock, self.rules, self.policy, deadline, task_actor, guidance.operator_stands, upgrade_candidates=upgrade_candidates, build_jobs=build_jobs)),
                                  ("treasure", lambda: draft.intelligence.candidates(world, clock, self.policy, deadline)),
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
                      "return_routes": guidance.return_routes,
                      "navigation_retry_exclusions": {u.id:sorted(world.navigation_avoided.get(u.pos, set())) for u in world.movers},
                      "movement_retry_windows": draft.move_retry_windows(world.round),
                      "construction_jobs": build_jobs,
                      "work_status":{u.id:{"ore":[u.inventory[k] for k in ("stone","iron","copper")],
                          "upgrade":({k:upgrade_plans[u.id][k] for k in ("name","steps","stage")} if u.id in upgrade_plans else None)}
                          for u in world.movers if u.kind=="worker"},
                      "battery_plan": world.battery_plan,
                      "gatling_upgrades": {u.id:{"hp":u.health,"level":u.level,
                          "state":economy.procurement.gatling_upgrade_status(world,u,self.policy,self.rules)[0],
                          "threshold":economy.procurement.gatling_upgrade_status(world,u,self.policy,self.rules)[1]}
                          for u in world.weapons if u.kind=='gatling' and u.level in (1,2)},
                      "wall_supply": {"need":len(economy.battery.missing_walls(world,self.rules)),
                          "goal":len(world.wall_targets) if world.wall_targets is not None else self.rules.wall_limit,
                          "ports":sorted(world.firing_ports),
                          "stone":{u.id:u.inventory["stone"] if u.backpack is not None else None for u in world.movers if u.kind=="worker"},
                          "quotas":{i:j.get("stock_target") for i,j in build_jobs.items() if j["name"]=="wall"},
                          "mines":sorted(world.zones.get("stone", ()))[:8],
                          "gaps":sorted(economy.battery.missing_walls(world,self.rules)),
                          "seal_ready":bool(world.seal_cells)},
                      "gun_status":combat.fire_status(world,clock,self.rules,response,candidates),
                      "feedback_counts": dict(draft.feedback_counts),
                      "construction_commitments": sorted(guidance.construction_actions),
                      "upgrade_commitments": sorted(guidance.upgrade_actions),
                      "urgent_upgrade_commitments": sorted(guidance.urgent_upgrades),
                      "base_recovery": deepcopy(draft.recovery.diagnostic),
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
                         "statement_names", "statement_path", "statement_ready", "statement_empty", "locate_attempts")} if task else None,
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
