"""Risk-aware macro intents, keeping scenario bounds separate from game facts.

Robot target choice and attack cadence are not supplied. The lower damage bound
is therefore zero; the upper is one attack opportunity from each visible robot
in range. It is a triage scenario, not a claimed next-round damage forecast.
"""
from dataclasses import dataclass, field, replace
from copy import copy
from itertools import product
import time

from .arbitration import Candidate
from .navigation import neighbours, interaction_cells, distance_field
from .protocol import distance, pos_json
from .rules import Policy
from . import lookahead
from .night_roles import defender_ids, operators, fixed_stands, transit_stands
from .lookahead import ROBOT_DAMAGE
from .robot_threats import active as active_threats
from .firing_lanes import FiringLanes
from .operator_cycles import OperatorCycles
from .rocket_rotation import advance as advance_rockets


def exposure(world, clock, pos):
    threats = active_threats(world)
    if "night" not in clock.phases:
        threats = []
    near = [r for r in threats if r.attack_range is not None and distance(r.pos, pos) <= r.attack_range]
    upper = sum(r.attack_power or 0 for r in near)
    return {"lower": 0, "upper_per_attack_opportunity": upper,
            "nearest": min((distance(r.pos, pos) for r in threats), default=world.width+world.height),
            "sources": [r.id for r in near],
            "unknown_robot_damage": any(r.kind not in ROBOT_DAMAGE and r.attack_power is None for r in near)}


@dataclass
class Directive:
    duty_permit: object = None
    market_permit: object = None
    candidates: list = field(default_factory=list)
    task_moves: set = field(default_factory=set)
    allow_task_control: bool = False
    base_critical: bool = False
    observations: dict = field(default_factory=dict)
    operator_stands: dict = field(default_factory=dict)
    return_routes: dict = field(default_factory=dict)
    horizon_risk: dict = field(default_factory=dict)
    construction_actions: dict = field(default_factory=dict)
    day_actions: dict = field(default_factory=dict)
    treasure_actions: dict = field(default_factory=dict)
    funded_actions: dict = field(default_factory=dict)
    repair_supply_actions: dict = field(default_factory=dict)
    seal_builds: dict = field(default_factory=dict)
    upgrade_actions: dict = field(default_factory=dict)
    recovery_actions: dict = field(default_factory=dict)
    medical_actions: dict = field(default_factory=dict)
    economic_route_actions: dict = field(default_factory=dict)
    economic_route_goals: dict = field(default_factory=dict)
    operator_plan_status: str = "unplanned"
    blocked_moves: dict = field(default_factory=dict)
    urgent_upgrades: dict = field(default_factory=dict)
    upgrading_guns: set = field(default_factory=set)
    site_clear_actions: dict = field(default_factory=dict)
    roster_transit_actions: dict = field(default_factory=dict)
    repair_actions: dict = field(default_factory=dict)

    def permit(self, candidate):
        """A due return is a macro commitment, not a price-dependent bid.

        Medical/combat triage may override it. Reaching a stand permits stationary
        work, while ordinary movement cannot spend an already exhausted buffer.
        """
        if self.market_permit is not None and not self.market_permit(candidate):
            return False
        if self.duty_permit is not None:
            allowed=self.duty_permit(candidate)
            if allowed is not None:
                return allowed
        if candidate.command.get("action") == "move":
            targets = candidate.command.get("targetPos", [])
            if len(targets) == 1 and (targets[0].get("x"), targets[0].get("y")) in self.blocked_moves.get(candidate.actor, set()):
                return False
        if candidate.actor in self.roster_transit_actions:
            return candidate.command in self.roster_transit_actions[candidate.actor] or (
                candidate.command.get('action') == 'use'
                and candidate.command.get('name') in {'Medicine', 'Bomb', 'DizzyWeapon'})
        if candidate.command.get('action')=='attack' and candidate.command.get('controllerId') in self.repair_actions:
            return False
        if candidate.actor in self.repair_actions:
            return candidate.command in self.repair_actions[candidate.actor] or (
                candidate.command.get('action')=='use' and candidate.command.get('name') in {'Medicine','Bomb','DizzyWeapon'})
        if candidate.command.get('action') == 'attack' and (
                candidate.actor in self.upgrading_guns or candidate.command.get('controllerId') in self.urgent_upgrades):
            return False
        if candidate.actor in self.urgent_upgrades:
            return candidate.command == self.urgent_upgrades[candidate.actor]
        if candidate.actor in self.site_clear_actions:
            return candidate.command in self.site_clear_actions[candidate.actor] or (
                candidate.command['action']=='use' and candidate.command.get('name') in {'Medicine','Bomb','DizzyWeapon'})
        if candidate.actor in self.repair_supply_actions:
            medical = candidate.command['action']=='use' and candidate.command.get('name') in {'Medicine','Bomb','DizzyWeapon'}
            return medical or candidate.command in self.repair_supply_actions[candidate.actor]
        if candidate.actor in self.treasure_actions:
            medical = candidate.command['action']=='use' and candidate.command.get('name') in {'Medicine','Bomb','DizzyWeapon'}
            return medical or candidate.command in self.treasure_actions[candidate.actor]
        if candidate.actor in self.funded_actions:
            medical = candidate.command['action']=='use' and candidate.command.get('name') in {'Medicine','Bomb','DizzyWeapon'}
            return medical or candidate.command in self.funded_actions[candidate.actor]
        for commitments in (self.recovery_actions, self.construction_actions, self.upgrade_actions, self.medical_actions, self.day_actions):
            if candidate.actor in commitments and not any(candidate is c for c in self.candidates):
                medical = candidate.command['action'] == 'use' and candidate.command.get('name') in {'Medicine','Bomb','DizzyWeapon'}
                if not medical and candidate.command not in commitments[candidate.actor]:
                    return False
        if candidate.actor in self.economic_route_actions and not any(candidate is c for c in self.candidates):
            if candidate.command["action"] in {"collect", "sell"}:
                return False
            if candidate.route_goal is not None:
                if (candidate.route_goal != self.economic_route_goals[candidate.actor] or
                        candidate.command not in self.economic_route_actions[candidate.actor]):
                    return False
        route = self.return_routes.get(candidate.actor)
        if not route or not route["due"] or any(candidate is c for c in self.candidates):
            return True
        action = candidate.command["action"]
        if action == "attack" or candidate.command == self.seal_builds.get(candidate.actor):
            return True
        if action == "move":
            target = candidate.command["targetPos"][0]
            return (target["x"], target["y"]) in route["steps"]
        if route["length"] == 0:
            return True
        return action == "use" and candidate.command.get("name") in {"Medicine", "Bomb", "DizzyWeapon"}


def matching_size(masks):
    """Maximum distinct guns operable by up to three role coverage masks."""
    states = {0}
    for mask in masks:
        following = set(states)
        for used in states:
            available = mask & ~used
            while available:
                bit = available & -available
                following.add(used | bit)
                available -= bit
        states = following
    return max((used.bit_count() for used in states), default=0)


def defence_roles(world, include_pioneer, task_actor, allow_task_control):
    roles = operators(world, include_pioneer, task_actor, allow_task_control)
    return roles, {u.id for u in roles if u.id == task_actor}


def cooling_handoff_gaps(weapons, operators, stands, fields):
    """Count staffed cooling rockets whose proposed replacement arrives late.

    Distances are current reachable walking actions, not a guarantee against
    future collisions. The role at the gun has no permanent assignment.
    """
    return sum(not any(p is not None and distance(p, gun.pos) <= 1
                       and fields[actor.id].get(p, float('inf')) <= gun.cooldown
                       for actor, p in zip(operators, stands))
               for gun in weapons if gun.kind == 'rocket' and gun.cooldown in (1, 2, 3)
               and any(distance(actor.pos, gun.pos) <= 1 for actor in operators))


def fully_staffed(weapons, operators):
    """Only protect a handoff when every gun already has a distinct role.

    With fewer usable roles, rotation between cooling and ready weapons is
    intentional; do not treat that resource shortage as an avoidable handoff.
    """
    masks = [sum(1 << i for i, gun in enumerate(weapons) if distance(actor.pos, gun.pos) <= 1)
             for actor in operators]
    return bool(weapons) and matching_size(masks) == len(weapons)


def fallback_stands(world, *, include_pioneer, task_actor, allow_task_control, handoff_enabled=False):
    """Small deterministic incumbent, independent of the optional search clock.

    At most three BFS traversals on the supplied 41x32 board, then at most
    11^3 combinations (three nearest approaches per gun, current cell, None).
    Three approaches suffice to avoid two other assigned stands. No previous
    frame's position or route is reused. Oversized compatibility maps skip this
    reserve rather than allowing an unbounded fallback search.
    """
    if world.width * world.height > 41 * 32:
        return {}
    planned = fixed_stands(world, float("inf"), include_pioneer, task_actor, allow_task_control)
    if planned is not None:
        return planned
    weapons = world.weapons[:3]
    operators, fixed = defence_roles(world, include_pioneer, task_actor, allow_task_control)
    if not weapons or not operators:
        return {}
    handoff_enabled = handoff_enabled and fully_staffed(weapons, operators)
    choices, fields = [], {}
    for actor in operators:
        distances = distance_field(world, [actor.pos], actor.pos)
        fields[actor.id] = distances
        cells = set()
        if actor.id in fixed:
            cells.add(actor.pos)
        else:
            for gun in weapons:
                reachable = interaction_cells(world, [gun.pos], actor.pos) & distances.keys()
                reachable -= getattr(world,'operator_excluded_cells',set())
                if world.defence_cells:
                    reachable &= world.defence_cells
                cells.update(sorted(reachable, key=lambda p: (distances[p], p))[:3])
            if actor.pos not in getattr(world,'operator_excluded_cells',set()) and (not world.defence_cells or actor.pos in world.defence_cells) and any(distance(actor.pos, gun.pos) <= 1 for gun in weapons):
                cells.add(actor.pos)
        choices.append(sorted(cells) + [None])
    masks = {p: sum(1 << i for i, gun in enumerate(weapons) if distance(p, gun.pos) <= 1)
             for cells in choices for p in cells if p is not None}
    masks[None] = 0
    ready = sum(1 << i for i, gun in enumerate(weapons)
                if gun.cooldown == 0 or (gun.cooldown is None and gun.kind != "rocket"))
    best, capacities = None, {}
    for stands in product(*choices):
        occupied = [p for p in stands if p is not None]
        if len(set(occupied)) != len(occupied):
            continue
        coverage = tuple(masks[p] for p in stands)
        firing = tuple(mask & ready for mask in coverage)
        for key in (coverage, firing):
            if key not in capacities:
                capacities[key] = matching_size(key)
        travel = sum(fields[u.id][p] for u, p in zip(operators, stands) if p is not None)
        gaps = cooling_handoff_gaps(weapons, operators, stands, fields) if handoff_enabled else 0
        key = (-capacities[coverage], gaps, -capacities[firing], -len(occupied), travel,
               tuple(p if p is not None else (-1, -1) for p in stands))
        if best is None or key < best[0]:
            best = (key, stands)
    return {u.id: p for u, p in zip(operators, best[1]) if p is not None}


def return_plan(world, clock, stands, policy, deadline=float("inf"), *, failed_steps=None, force_due=False):
    """Publish all routes together; an interrupted refinement returns None."""
    routes, candidates = {}, []
    for actor in world.movers:
        stand = stands.get(actor.id)
        if stand is None:
            continue
        if time.monotonic() >= deadline:
            return None
        field = ({stand: 0} if stand == actor.pos else
                 distance_field(world, [stand], actor.pos, deadline))
        length = field.get(actor.pos)
        if length is None or time.monotonic() >= deadline:
            return None
        steps = sorted(p for p in neighbours(actor.pos) if p in field and field[p] < length)
        avoided = set((failed_steps or {}).get(actor.id, ())) if policy.return_detour_enabled else set()
        extra = 0
        if avoided.intersection(steps):
            # At most one extra board traversal per role. Exclusions describe
            # temporary retry policy, not newly observed walls. A short detour
            # can start sideways, while still having a complete route to stand.
            detour = distance_field(world, [stand], actor.pos, deadline, extra_blocked=avoided)
            if time.monotonic() >= deadline:
                return None
            alternative_length = detour.get(actor.pos)
            if alternative_length is not None and alternative_length <= length + 4:
                extra = alternative_length - length
                field, length = detour, alternative_length
                steps = sorted(p for p in neighbours(actor.pos) if p in field and field[p] < length)
        if policy.operator_safety_enabled and exposure(world, clock, actor.pos)["upper_per_attack_opportunity"]:
            steps.sort(key=lambda p: (exposure(world, clock, p)["upper_per_attack_opportunity"], p))
        # Near-complete walls require time for gate traffic and sealing after arrival.
        seal_buffer = 8 if world.defence_cells else 0
        slack = clock.until_night-length-policy.return_buffer-seal_buffer
        due = force_due or clock.phases != {"day"} or slack <= 0
        routes[actor.id] = {"stand": stand, "length": length, "steps": steps,
                           "slack_before_buffer": slack, "due": due}
        if avoided:
            routes[actor.id].update(avoided_steps=sorted(avoided), detour_extra_steps=extra)
        if due and length:
            candidates.extend(Candidate(actor.id, {"action": "move", "targetPos": [pos_json(p)]},
                                        35-i*.01, "execute due return route before optional economic work")
                              for i, p in enumerate(steps[:4]))
    return routes, candidates


def assign_operator_stands(world, clock, deadline, *, include_pioneer=True,
                           task_actor=None, allow_task_control=False, safety_enabled=True,
                           firing_lanes_enabled=False, handoff_enabled=False, cycle_enabled=False):
    """Joint placement for the two admitted defenders.

    Active task actors are fixed at their observed cell. Current occupancy is
    never treated as vacated, even when another unit is assigned a future stand.
    """
    planned = fixed_stands(world, deadline, include_pioneer, task_actor, allow_task_control)
    if planned is not None:
        return planned
    weapons = world.weapons[:3]  # The supplied rules cap the team at three guns.
    cycle_enabled = cycle_enabled and clock.phases == {'night'} and all(
        w.cooldown is not None and w.attack_range is not None for w in weapons)
    handoff_enabled = handoff_enabled and clock.phases == {'night'}
    operators, fixed = defence_roles(world, include_pioneer, task_actor, allow_task_control)
    if not operators or not weapons:
        return {}
    handoff_enabled = handoff_enabled and fully_staffed(weapons, operators)
    ready = sum(1 << i for i, w in enumerate(weapons)
                if w.cooldown in (0, None) and not (w.kind == "rocket" and w.cooldown is None))
    worker_ids = [u.id for u in operators if u.kind == "worker"]
    baseline = (assign_operator_stands(world, clock, deadline, include_pioneer=False,
                                      safety_enabled=safety_enabled, firing_lanes_enabled=firing_lanes_enabled,
                                      handoff_enabled=handoff_enabled, cycle_enabled=cycle_enabled)
                if not cycle_enabled and any(u.kind == "pioneer" for u in operators) and worker_ids else None)
    lanes = FiringLanes(world, clock, weapons, operators, deadline) if firing_lanes_enabled else None
    try:
        cycles = OperatorCycles(world, clock, weapons, operators, deadline) if cycle_enabled else None
    except TimeoutError:
        return {}
    current_open = lanes.available(u.pos for u in operators) if lanes else 0
    firing_now = {u.id for u in operators if any(current_open & (1 << i) and distance(u.pos, w.pos) <= 1
                                               for i, w in enumerate(weapons))}
    current_exposure = {u.id: exposure(world, clock, u.pos) for u in operators} if lanes else {}
    choices, fields, features = [], {}, {}
    for operator in operators:
        if time.monotonic() >= deadline:
            return {}
        distances = distance_field(world, [operator.pos], operator.pos, deadline)
        fields[operator.id] = distances
        cells = (set([operator.pos]) if operator.id in fixed else
                 interaction_cells(world, [w.pos for w in weapons], operator.pos))
        cells = [p for p in cells if p in distances and (operator.id in fixed or not world.defence_cells or p in world.defence_cells)]
        if operator.id not in fixed:
            cells = [p for p in cells if p not in getattr(world,'operator_excluded_cells',set())]
        for p in cells:
            if p not in features:
                adjacent = [w for w in weapons if distance(p, w.pos) <= 1]
                risk = exposure(world, clock, p)
                # Only strengthen avoidance when every adjacent gun has a
                # known range and none reaches this attacker. Cooldown/unknown
                # rays do not prove that abandoning a firing position is useful.
                uncovered = sum(ROBOT_DAMAGE.get(r.kind, r.attack_power or 0)
                                for r in world.robots.values()
                                if r.id in risk["sources"] and adjacent
                                and all(w.attack_range is not None and distance(w.pos, r.pos) > w.attack_range
                                        for w in adjacent))
                features[p] = (sum(1 << i for i, w in enumerate(weapons) if w in adjacent),
                               risk["upper_per_attack_opportunity"], uncovered, risk["nearest"])
        cells.sort(key=lambda p: (-features[p][0].bit_count(), distances[p], features[p][1], p))
        bounded = cells[:12]
        # Keep a reachable approach for every gun: three distant guns can be
        # missed when all twelve nearest cells belong to only the first two.
        for index in range(len(weapons)):
            approaches = [p for p in cells if features[p][0] & (1 << index)]
            if approaches:
                point = min(approaches, key=lambda p: (distances[p], features[p][1], p))
                if point not in bounded:
                    bounded.append(point)
                if safety_enabled:
                    safer = min(approaches, key=lambda p: (features[p][2], distances[p], p))
                    if safer not in bounded:
                        bounded.append(safer)
                if lanes and lanes.guns[index]:
                    clearer = min(approaches, key=lambda p: (lanes.mask(p).bit_count(), distances[p], p))
                    if clearer not in bounded:
                        bounded.append(clearer)
        if operator.pos in cells and operator.pos not in bounded:
            bounded.append(operator.pos)
        choices.append(bounded or [None])
    best, matches, stable = None, {}, {}
    for stands in product(*choices):
        if time.monotonic() >= deadline:
            if cycle_enabled:
                return {}  # Never publish a partly compared cycle placement.
            break
        occupied = [p for p in stands if p is not None]
        if len(set(occupied)) != len(occupied):
            continue
        masks = tuple(features[p][0] if p is not None else 0 for p in stands)
        ready_masks = tuple(mask & ready for mask in masks)
        # Missing assignments retain the role's observed position. Only this
        # placement scenario changes occupancy; combat still uses the snapshot.
        future_open = lanes.available(p if p is not None else u.pos for u, p in zip(operators, stands)) if lanes else 0
        firing_masks = tuple(mask & future_open for mask in masks)
        for key_masks in (ready_masks, masks, firing_masks):
            if key_masks not in matches:
                matches[key_masks] = matching_size(key_masks)
        coverage = 0
        for mask in masks:
            coverage |= mask
        travel = sum(fields[u.id].get(p, world.width+world.height) for u, p in zip(operators, stands))
        risk = sum(features[p][1] for p in occupied)
        uncovered = sum(features[p][2] for p in occupied)
        # A short walk can prevent repeated out-of-range attacks. This prices
        # one visible attack opportunity, not an assumed robot attack frequency.
        score = matches[ready_masks]*45 + coverage.bit_count()*20 - travel*2 - risk*.25
        if cycles:
            try:
                opportunity = cycles.value(stands, fields)
            except TimeoutError:
                return {}
            if opportunity is not None:
                score += (opportunity-matches[ready_masks])*45
        gaps = cooling_handoff_gaps(weapons, operators, stands, fields) if handoff_enabled else 0
        score -= gaps*45
        if safety_enabled:
            score -= uncovered*3.75
        # Clearing a hypothetical future lane must not pull a currently useful
        # controller away or lure a mover closer to visible robot threats.
        # These are static scenario constraints, not a survival guarantee.
        lane_capacity = matches[firing_masks] if lanes and all(
            p is None or p == u.pos or (u.id not in firing_now
                and features[p][1] <= current_exposure[u.id]["upper_per_attack_opportunity"]
                and features[p][3] >= current_exposure[u.id]["nearest"])
            for u, p in zip(operators, stands)) else 0
        score += lane_capacity*20  # Local placement utility, not predicted damage.
        tie = tuple(p if p is not None else (-1, -1) for p in stands)
        key = (-score, tie)
        if best is None or key < best[0]:
            best = (key, stands, (matches[masks], matches[ready_masks]), uncovered, lane_capacity, gaps)
        # Adding a third operator should not reshuffle the workers when the
        # same full/ready matching capacity is available around their baseline.
        if baseline is not None and all(p == baseline.get(u.id) for u, p in zip(operators, stands) if u.id in worker_ids):
            capacity = (matches[masks], matches[ready_masks])
            if capacity not in stable or key < stable[capacity][0]:
                stable[capacity] = (key, stands, capacity, uncovered, lane_capacity, gaps)
    if (not cycle_enabled and best is not None and best[2] in stable
            and (not safety_enabled or stable[best[2]][3] <= best[3])
            and stable[best[2]][4] >= best[4]
            and stable[best[2]][5] <= best[5]):
        best = stable[best[2]]
    return {operator.id: pos for operator, pos in zip(operators, best[1]) if pos is not None} if best else {}


def triage(world, clock, task_actor, task, policy=None, risk_memory=None):
    """Immediate rescue candidates shared by real and hypothetical decisions.

    No future movement, observation mutation, or operator-layout search occurs
    here. Callers still arbitrate all actions and enforce their task constraints.
    """
    policy = policy or Policy()
    result = Directive()
    for station in world.stations:
        # Anchor distance is conservative here; multi-cell attack targeting itself
        # is still a separate unknown rule, not inferred from this risk heuristic.
        risk = exposure(world, clock, station.pos)
        result.observations[station.id] = risk
        result.base_critical |= risk["upper_per_attack_opportunity"] >= station.health
    result.allow_task_control = result.base_critical or (task is not None and task.answer is None)
    for actor in world.movers:
        risk = exposure(world, clock, actor.pos)
        result.observations[actor.id] = risk
        upper = risk["upper_per_attack_opportunity"]
        observed_loss = max((loss for r, loss in (risk_memory.recent_hp_losses.get(actor.id, []) if risk_memory else [])
                             if world.round-2 <= r <= world.round), default=0)
        if (policy.lethal_entry_guard_enabled or actor.kind == "pioneer" or ((observed_loss > 0 or (risk_memory and actor.id in risk_memory.damaged_actors)) and actor.health <= 110)) and clock.phases == {"night"} and upper < actor.health:
            blocked = []
            for pos in neighbours(actor.pos):
                if not world.inside(pos) or pos in world.occupied:
                    continue
                destination = exposure(world, clock, pos)
                if destination["upper_per_attack_opportunity"] >= actor.health:
                    result.blocked_moves.setdefault(actor.id, set()).add(pos)
                    blocked.append({"pos": pos_json(pos), **destination})
            risk["blocked_lethal_entries"] = blocked
        if not upper:
            continue
        critical = upper >= actor.health
        # These thresholds are triage policy, not calibrated mortality estimates.
        # A travelling pioneer often cannot return fire; waiting for half HP
        # before offering escape reproduced the first-night death in the report.
        can_control = any(distance(actor.pos, gun.pos) <= 1 and gun.cooldown in (0, None)
                          and gun.attack_range is not None and any(
                              robot.alive and distance(robot.pos, gun.pos) <= gun.attack_range
                              for robot in world.robots.values()) for gun in world.weapons)
        travelling_pioneer = (actor.kind == "pioneer" and actor.id != task_actor
                              and (world.navigation_avoided.get(actor.pos) or (not can_control and upper*4 >= actor.health)))
        # The sum is a worst-case opportunity bound, not damage actually aimed
        # at this role. Healthy defenders keep the gun online under pressure;
        # otherwise three independent escape bids dismantle the whole battery.
        covering = [gun for gun in world.weapons if distance(actor.pos, gun.pos) <= 1
                    and gun.attack_range is not None and any(
                        robot.alive and distance(robot.pos, gun.pos) <= gun.attack_range
                        for robot in world.robots.values())]
        largest_hit = max((ROBOT_DAMAGE.get(world.robots[i].kind, world.robots[i].attack_power or 0)
                           for i in risk["sources"]), default=0)
        withdrawal_hp = max(2*largest_hit, 2*observed_loss+largest_hit, (200 if actor.kind == "pioneer" else 220)*0.3)
        hold_fire = bool(covering) and actor.health > withdrawal_hp
        risk.update(defence_hold=hold_fire, withdrawal_hp=withdrawal_hp, observed_hp_loss=observed_loss,
                    covering_guns=[gun.id for gun in covering])
        if hold_fire:
            # Treatment remains useful; do not spend a healthy firing action on
            # a small scratch merely because many robots are visible.
            if actor.inventory["Medicine"] and actor.health <= (200 if actor.kind == "pioneer" else 220)*0.5:
                result.candidates.append(Candidate(actor.id, {"action":"use", "name":"Medicine"},
                                                    1200, "defender: heal then resume fire"))
            continue
        if not critical and upper*2 < actor.health and not travelling_pioneer:
            continue
        if actor.inventory["Medicine"]:
            maximum = 200 if actor.kind == "pioneer" else 220
            if actor.health < maximum and (not policy.medical_stock_enabled or critical or actor.health*2 <= maximum):
                result.candidates.append(Candidate(actor.id, {"action": "use", "name": "Medicine"},
                                                    1200+maximum-actor.health, "triage: heal exposed role using known maximum HP"))
        # When one opportunity is survivable, prefer submitting supported work
        # before departure. The next turn must reevaluate the actual snapshot.
        if actor.id == task_actor and task and task.answer and not critical:
            continue
        for pos in neighbours(actor.pos):
            if not world.inside(pos) or pos in world.occupied:
                continue
            future = exposure(world, clock, pos)
            improves = (future["upper_per_attack_opportunity"], -future["nearest"]) < (upper, -risk["nearest"])
            if not improves:
                continue
            reduction = upper-future["upper_per_attack_opportunity"]
            retains_gun = any(distance(pos, gun.pos) <= 1 for gun in covering)
            value = (1000 if critical else 150) + reduction*2 + future["nearest"]
            if retains_gun:
                value += 100  # Prefer a safer operating stand over abandoning fire.
            within_task = actor.id == task_actor and task and any(distance(pos, p) <= 1 for p in task.cells)
            if within_task:
                value += 10  # Preserve a task when escape quality is comparable.
            result.candidates.append(Candidate(actor.id, {"action": "move", "targetPos": [pos_json(pos)]}, value,
                                                "triage: reduce exposed attack opportunities; task exit if necessary"))
            if actor.id == task_actor:
                result.task_moves.add(pos)
    return result


def _pioneer_return_due(world, clock, policy, deadline):
    """Real return distance can end daytime work; night always owns defence."""
    roster = world.night_roster
    pioneer = world.ours.get(roster.p)
    if not pioneer or not pioneer.alive or not world.stations:
        return False
    traffic = roster.traffic
    if (traffic and not traffic.get('gate_owned') and traffic.get('blocker') == pioneer.id
            and (traffic.get('kind') == 'fixed_w_return' or clock.phases != {'day'})):
        return True  # A failed real yield remains due even while P is at C.
    plan = getattr(world,'task_side_plan',None)
    from .defence_duties import stands
    goals = (stands(world, pioneer.id) if plan else set(world.defence_cells))
    if not goals:
        goals = interaction_cells(world,[p for station in world.stations for p in station.cells],pioneer.pos)
    if pioneer.pos in goals:
        return False
    if clock.phases != {'day'}:
        return True
    if not policy.return_commitment_enabled or not policy.pioneer_defence_enabled:
        return False
    if time.monotonic() >= deadline:
        return False
    actual = distance_field(world,goals-world.occupied,pioneer.pos,deadline)
    length = actual.get(pioneer.pos)
    if length is None and time.monotonic() < deadline:
        # A role blocking the corridor cannot erase the departure deadline.
        # This relaxed distance only requests return; every issued move still
        # uses the observed occupied map and explicit clearance below.
        relaxed = copy(world)
        relaxed.occupied = world.occupied-{u.pos for u in world.movers}
        length = distance_field(relaxed,goals-relaxed.occupied,pioneer.pos,deadline).get(pioneer.pos)
    return (time.monotonic() < deadline and length is not None
            and clock.until_night <= length+policy.return_buffer+(8 if world.defence_cells else 0))


def propose(world, clock, task_actor, task, deadline, policy=None, risk_memory=None, *, failed_steps=None):
    policy = policy or Policy()
    world.night_clock = clock
    world.task_return_required = False
    world.pioneer_defence_moves = []
    defender_ids(world)
    world.task_return_required = bool(getattr(world,"ordered_ingress_due",False) or _pioneer_return_due(world,clock,policy,deadline))
    if world.task_return_required and clock.phases != {'day'}:
        pioneer = world.ours[world.night_roster.p]
        threats = [r for r in world.robots.values() if r.alive and r.abnormal != 'dizzy']
        blocked = set()
        complete = True
        for x in range(world.width):
            if time.monotonic() >= deadline:
                complete = False;break
            for y in range(world.height):
                point = x,y
                sources = [r for r in threats if r.attack_range is None or distance(point,r.pos)<=r.attack_range]
                if (any(r.attack_power is None or r.attack_range is None for r in sources)
                        or 2*sum(r.attack_power for r in sources) >= pioneer.health):
                    blocked.add(point)
        if not complete:
            # Expiry cannot turn every free neighbour into an observed wall.
            # Check only the next possible step (at most eight cells) using
            # the same threat facts; unknown range/power remains unsafe.
            blocked = set()
            for point in neighbours(pioneer.pos):
                sources = [r for r in threats if r.attack_range is None or distance(point,r.pos)<=r.attack_range]
                if (any(r.attack_power is None or r.attack_range is None for r in sources)
                        or 2*sum(r.attack_power for r in sources) >= pioneer.health):
                    blocked.add(point)
        world.navigation_avoided.setdefault(pioneer.pos,set()).update(blocked-{pioneer.pos})
    if world.task_return_required:
        # Preserve task state until an actual departure is observed, but its
        # old neighbourhood cannot pin the night C operator outside the base.
        task_actor, task = None, None
    include_pioneer = policy.pioneer_defence_enabled or clock.phases != {'day'}
    result = triage(world, clock, task_actor, task, policy, risk_memory)
    triage_candidates = list(result.candidates)
    # Reserve a complete current-state route bundle before optional placement
    # optimization can exhaust its time budget. Keep triage candidates separate.
    if policy.return_commitment_enabled:
        result.operator_stands = fallback_stands(world, include_pioneer=include_pioneer,
                                                 task_actor=task_actor, allow_task_control=result.allow_task_control,
                                                 handoff_enabled=policy.operator_handoff_enabled and clock.phases == {'night'})
        baseline = return_plan(world, clock, result.operator_stands, policy, failed_steps=failed_steps)
        result.operator_plan_status = "fallback"
    else:
        baseline = ({}, [])
    refined_stands = assign_operator_stands(world, clock, deadline, include_pioneer=include_pioneer,
                                                    task_actor=task_actor, allow_task_control=result.allow_task_control,
                                                    safety_enabled=policy.operator_safety_enabled,
                                                    firing_lanes_enabled=policy.firing_lanes_enabled,
                                                    handoff_enabled=policy.operator_handoff_enabled,
                                                    cycle_enabled=policy.operator_cycle_enabled)
    before_rotation = refined_stands
    if policy.rocket_rotation_enabled and not getattr(world, "task_side_plan", None):
        refined_stands = advance_rockets(world, clock, refined_stands, deadline,
            lambda p: exposure(world,clock,p), task_actor=task_actor,
            allow_task_control=result.allow_task_control,
            unavailable={c.actor for c in result.candidates}, failed_steps=failed_steps)
    refined = (return_plan(world, clock, refined_stands, policy, deadline, failed_steps=failed_steps)
               if policy.return_commitment_enabled else ({}, []))
    if refined is not None and result.operator_stands.keys() <= refined_stands.keys():
        result.operator_stands = refined_stands
        baseline = refined
        result.operator_plan_status = "optimized_cooldown_advance" if refined_stands != before_rotation else "optimized"
    result.return_routes = baseline[0]
    result.candidates.extend(baseline[1])
    # If the gun stand is unreachable, enter an available interior cell first.
    # Preserve an existing reachable stand route: it may briefly cross a yellow
    # gate cell. Replacing it there with the nearest interior cell can send the
    # role back to its previous position forever instead of reaching the gun.
    if world.defence_cells:
        for actor in world.movers:
            if actor.id not in defender_ids(world) or actor.id == task_actor or actor.pos in world.defence_cells or actor.id in result.return_routes:
                continue
            goals = set(world.defence_cells)-world.occupied
            field = distance_field(world, [actor.pos], actor.pos, deadline)
            reachable = goals & field.keys()
            if not reachable:
                continue
            entry = min(reachable, key=lambda p:(field[p],p))
            inward = return_plan(world, clock, {actor.id:entry}, policy, deadline, failed_steps=failed_steps)
            if inward is not None:
                old = baseline[1]
                result.candidates = [c for c in result.candidates if not (c.actor==actor.id and any(c is b for b in old))]
                result.return_routes.update(inward[0])
                result.candidates.extend(inward[1])
    # With fewer guns than roles, an unassigned pioneer still needs a return
    # destination. The base is a landmark, not assumed invulnerability.
    if world.stations and include_pioneer:
        for actor in world.movers:
            if actor.id not in defender_ids(world) or (actor.kind != "pioneer" and not world.defence_cells) or actor.id == task_actor or actor.id in result.return_routes:
                continue
            goals = (set(world.defence_cells)-(world.occupied-{actor.pos}) if world.defence_cells else interaction_cells(world, [p for station in world.stations for p in station.cells], actor.pos))
            field = distance_field(world, [actor.pos], actor.pos, deadline)
            reachable = goals & field.keys()
            if not reachable:
                continue
            stand = min(reachable, key=lambda p:(exposure(world, clock, p)["upper_per_attack_opportunity"], field[p], p))
            shelter = return_plan(world, clock, {actor.id:stand}, policy, deadline, failed_steps=failed_steps)
            if shelter is not None:
                result.return_routes.update(shelter[0])
                result.candidates.extend(shelter[1])
    # A teammate can temporarily make the one-gate return route unreachable.
    # Diagnose that using a relaxed map, but move only blockers with a real,
    # currently unobstructed route. Never send a move into an occupied cell.
    if world.defence_cells and clock.phases == {"day"}:
        relaxed = copy(world)
        relaxed.occupied = world.occupied-{u.pos for u in world.movers}
        for actor in world.movers:
            if actor.id not in defender_ids(world) or actor.id == task_actor or actor.pos in world.defence_cells or actor.id in result.return_routes:
                continue
            field = distance_field(relaxed,world.defence_cells,actor.pos,deadline)
            if actor.pos not in field:
                continue
            carrying_upgrade = any('UpgradeVoucher' in name and count > 0 for name,count in actor.inventory.items())
            if clock.until_night > field[actor.pos]+policy.return_buffer+8 and not carrying_upgrade:
                continue  # An outbound worker has no current right-of-way to return.
            path, cursor = [], actor.pos
            while field[cursor] > 0:
                cursor = min((p for p in neighbours(cursor) if p in field and field[p]<field[cursor]),key=lambda p:(field[p],p))
                path.append(cursor)
            for blocker in world.movers:
                prior = result.return_routes.get(blocker.id)
                if blocker.id == task_actor or blocker.pos not in path or not prior:
                    continue
                stand = prior["stand"]
                from .gate_traffic import outward_clearance
                outward = outward_clearance(world, clock, blocker, path, policy, deadline)
                if outward is not None:
                    stand = outward
                elif not prior["length"]:
                    # The only inner landing may itself be a gun's current
                    # stand. Vacate it, including diagonal movement permitted
                    # by the rules; holding there cannot clear gate traffic.
                    real = distance_field(world,[blocker.pos],blocker.pos,deadline)
                    available = (set(world.defence_cells)-world.occupied-set(path)) & real.keys()
                    if not available:
                        continue
                    stand = min(available,key=lambda p:(real[p],p))
                yielding = return_plan(world,clock,{blocker.id:stand},replace(policy,return_buffer=130),deadline)
                if yielding is not None:
                    yielding[0][blocker.id]["yield_for"] = actor.id
                    for candidate in yielding[1]:
                        candidate.reason = ("clear gate outward before returning teammate arrives" if outward is not None
                                            else "clear teammate's blocked gate return route")
                    result.return_routes.update(yielding[0]);result.candidates.extend(yielding[1])
    # Daytime outbound traffic needs the same cooperation as night return.
    # Use a relaxed path only to identify a blocker, then issue a real legal
    # step off that path. Do not dismantle more walls for a temporary role jam.
    if world.defence_cells and clock.phases == {"day"} and clock.until_night > 12:
        from .rules import station_rings
        blue, yellow = station_rings(world.stations[0].pos)
        outside = {p for q in yellow for p in neighbours(q)
                   if world.inside(p) and p not in blue|yellow}
        relaxed = copy(world)
        relaxed.occupied = world.occupied-{u.pos for u in world.movers}
        free_field = distance_field(relaxed,outside,None,deadline)
        for actor in world.movers:
            if actor.id == task_actor or actor.pos not in blue or actor.pos not in free_field:
                continue
            if actor.pos in distance_field(world,outside,actor.pos,deadline):
                continue
            path, cursor = [], actor.pos
            while free_field[cursor] > 0:
                cursor = min((p for p in neighbours(cursor) if p in free_field and free_field[p]<free_field[cursor]),
                             key=lambda p:(free_field[p],p))
                path.append(cursor)
            for blocker in world.movers:
                if blocker.id == task_actor or blocker.pos not in path:
                    continue
                if any('UpgradeVoucher' in name and count > 0 for name,count in blocker.inventory.items()):
                    continue  # A delivery must not be pushed back out of the gate.
                forward = [p for p in neighbours(blocker.pos) if p not in world.occupied
                           and p in free_field and free_field[p] < free_field[blocker.pos]]
                available = forward or [p for p in neighbours(blocker.pos) if world.inside(p)
                             and p not in world.occupied and p not in path and p != actor.pos]
                if not available:
                    continue
                stand = min(available,key=lambda p:(p not in outside,free_field.get(p,999),p))
                yielding = return_plan(world,clock,{blocker.id:stand},replace(policy,return_buffer=130),deadline)
                if yielding is not None:
                    yielding[0][blocker.id]['yield_for'] = actor.id
                    result.return_routes.update(yielding[0])
                    result.candidates = [c for c in result.candidates if c.actor!=blocker.id]
                    for c in yielding[1]:c.reason='clear teammate outbound path through daytime gate'
                    result.candidates.extend(yielding[1])
                    break
            else:
                continue
            break  # One controlled yield, recompute from next observed positions.
    transit = transit_stands(world, clock, deadline)
    transit_candidates = []
    if transit:
        ongoing_fixed = getattr(world,'night_roster',None) and world.night_roster.traffic.get('kind')=='fixed_w_return'
        routed = return_plan(world, clock, transit, policy, deadline, failed_steps=failed_steps,
                             force_due=bool(ongoing_fixed))
        if routed is not None:
            result.return_routes.update(routed[0])
            for candidate in routed[1]:
                candidate.reason = 'observed roster handoff or exterior economic endpoint'
            result.candidates.extend(routed[1])
            transit_candidates = routed[1]
    roster = getattr(world, 'night_roster', None)
    traffic = roster.traffic if roster else {}
    for identity in getattr(world,'fixed_w_transit_actors',()):
        result.roster_transit_actions[identity] = [c.command for c in
            transit_candidates + triage_candidates if c.actor == identity]
    if (clock.phases != {'day'} and roster and traffic and not traffic.get('gate_owned')
            and {traffic.get('traveller'),traffic.get('blocker')} == {roster.m,roster.p}):
        # Physical P return/M exit owns this short corridor until the observed
        # traveller passes. No task or unrelated delivery may reverse it.
        for identity in (roster.m,roster.p):
            result.roster_transit_actions[identity] = [c.command for c in
                transit_candidates + triage_candidates if c.actor == identity]
    for actor in world.movers:
        actor_task = task if actor.id == task_actor else None
        proposed, observation = lookahead.propose(world, clock, actor, actor_task, risk_memory,
                                                   policy, min(deadline, time.monotonic()+.025))
        result.candidates.extend(proposed)
        result.horizon_risk[actor.id] = observation
        if actor_task:
            result.task_moves.update((c.command["targetPos"][0]["x"], c.command["targetPos"][0]["y"]) for c in proposed)
    if world.task_return_required and roster and roster.p:
        route = result.return_routes.get(roster.p,{})
        steps = set(route.get('steps',()))
        allowed = [c.command for c in result.candidates if c.actor == roster.p and (
            c in triage_candidates or c.command.get('action') == 'move' and
            tuple(c.command['targetPos'][0][k] for k in ('x','y')) in steps)]
        result.roster_transit_actions[roster.p] = allowed
        result.candidates = [c for c in result.candidates if c.actor != roster.p or c.command in allowed]
        result.task_moves.update(tuple(c['targetPos'][0][k] for k in ('x','y'))
                                 for c in allowed if c.get('action') == 'move')
    if roster and roster.p:
        route_steps = set(result.return_routes.get(roster.p,{}).get('steps',()))
        transit_commands = result.roster_transit_actions.get(roster.p,())
        world.pioneer_defence_moves = [c.command for c in result.candidates
            if c.actor == roster.p and c.command.get('action') == 'move' and (
                c in triage_candidates or c.command in transit_commands or
                tuple(c.command['targetPos'][0][k] for k in ('x','y')) in route_steps)]
    return result
