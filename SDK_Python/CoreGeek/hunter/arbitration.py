"""Anytime beam selection with global locks and shared damage accounting."""
from dataclasses import dataclass, field
import math
import time

from .protocol import empty_response, validate_response
from .validation import Verdict, Resources, check_action, merge_resources
from .layout import LayoutGuard
from .base_fire import BasePressure


@dataclass
class Candidate:
    actor: str
    command: dict
    utility: float
    reason: str
    damage: dict[str, float] = field(default_factory=dict)
    suppression: frozenset[str] = field(default_factory=frozenset)
    gold_reserve: int = 0
    route_goal: dict | None = None


@dataclass
class Selection:
    response: dict
    selected: list[Candidate]
    rejected: list[dict]
    value: float
    alternatives: list = field(default_factory=list)


def select(world, clock, rules, policy, candidates, deadline, *, task_actor=None, summon_remaining=0,
           weights=None, incumbent=None, task_moves=(), allow_task_control=False, alternatives_limit=0,
           diversity_key=None):
    weights = weights or {}
    pressure = BasePressure(world, clock) if policy.base_fire_enabled and policy.joint_fire_enabled else None
    layout = LayoutGuard(world, deadline)
    rejected, checked = [], []
    layout_rejections = set()

    def layout_allowed(bundle):
        allowed, reason = layout.check(bundle)
        if not allowed and reason not in layout_rejections:
            layout_rejections.add(reason)
            rejected.append({"verdict": "layout_bundle_rejected", "reason": reason,
                             "actors": [c.actor for c in bundle if c.command["action"] == "build"]})
        return allowed
    unique = set()
    for candidate in sorted(candidates, key=lambda c: (-c.utility, c.actor, repr(c.command)))[:policy.max_candidates]:
        key = (candidate.actor, repr(candidate.command))
        if key in unique:
            continue
        unique.add(key)
        check = check_action(world, clock, rules, candidate.actor, candidate.command,
                             task_actor=task_actor, summon_remaining=summon_remaining,
                             task_moves=task_moves, allow_task_control=allow_task_control)
        if (check.verdict != Verdict.VALID or not math.isfinite(candidate.utility)
                or type(candidate.gold_reserve) is not int or candidate.gold_reserve < 0):
            rejected.append({"actor": candidate.actor, "action": candidate.command.get("action"),
                             "verdict": check.verdict.value, "reason": check.reason})
        else:
            checked.append((candidate, check.resources))

    def value(chosen, damage):
        total = sum(c.utility for c in chosen)
        if pressure is not None:
            total += pressure.value(damage)
        for identity, amount in damage.items():
            robot = world.robots.get(identity)
            if robot is None or not robot.alive:
                continue
            weight = weights.get(identity, 1.0)
            total += weight * min(robot.health, amount)
            if amount >= robot.health:
                total += weight * 12
        # A future control opportunity is counted once across the whole bundle.
        # No future suppression credit for already dizzy or predicted-dead units;
        # this does not assert that lethal damage prevents this turn's attack.
        for identity in sorted(set().union(*(c.suppression for c in chosen))):
            robot = world.robots.get(identity)
            if robot and robot.alive and robot.abnormal != "dizzy" and damage.get(identity, 0) < robot.health:
                total += weights.get(identity, 1.0) * 15
        return total

    # (value, selected, resources, predicted damage). Empty remains a structural
    # incumbent, with official waiting semantics explicitly unverified.
    beam = [(0.0, [], Resources(), {})]
    def preserves_reserve(bundle, resources):
        floor = max((c.gold_reserve for c in bundle), default=0)
        return not floor or (world.gold is not None and resources.gold+floor <= world.gold)
    if incumbent:
        resource, good, damage = Resources(), [], {}
        for candidate in incumbent:
            check = check_action(world, clock, rules, candidate.actor, candidate.command,
                                 task_actor=task_actor, summon_remaining=summon_remaining,
                                 task_moves=task_moves, allow_task_control=allow_task_control)
            merged = merge_resources(world, rules, resource, check.resources, summon_remaining)
            if (check.verdict == Verdict.VALID and merged is not None and preserves_reserve(good+[candidate], merged)
                    and layout_allowed(good+[candidate])):
                resource = merged
                good.append(candidate)
                for identity, amount in candidate.damage.items():
                    damage[identity] = damage.get(identity, 0) + amount
        beam.append((value(good, damage), good, resource, damage))
    beam.sort(key=lambda row: -row[0])
    for candidate, resource in checked:
        if time.monotonic() >= deadline:
            break
        expanded = list(beam)
        for _, chosen, used, damage in beam:
            merged = merge_resources(world, rules, used, resource, summon_remaining)
            if merged is None:
                continue
            combined = damage.copy()
            for identity, amount in candidate.damage.items():
                combined[identity] = combined.get(identity, 0) + amount
            bundle = chosen + [candidate]
            if not preserves_reserve(bundle, merged) or not layout_allowed(bundle):
                continue
            expanded.append((value(bundle, combined), bundle, merged, combined))
        # Keep only one instance of each action set (incumbent paths can duplicate).
        seen, ranked = set(), []
        for row in sorted(expanded, key=lambda row: (-row[0], tuple((c.actor, repr(c.command)) for c in row[1]))):
            signature = tuple(sorted((c.actor, repr(c.command)) for c in row[1]))
            if signature not in seen:
                seen.add(signature)
                ranked.append(row)
            if diversity_key is None and len(ranked) >= policy.beam_width:
                break
        if diversity_key is None:
            beam = ranked[:policy.beam_width]
        else:
            # Reserve at most half the slots for distinct tactical effects.
            # Every row has already passed the same bundle/resource checks.
            # The best row is retained; remaining slots preserve value order.
            chosen_indices, effects = [], set()
            for index, row in enumerate(ranked):
                key = diversity_key(row[1])
                if key not in effects:
                    effects.add(key)
                    chosen_indices.append(index)
                if len(chosen_indices) >= max(1, policy.beam_width // 2):
                    break
            selected_indices = set(chosen_indices)
            for index in range(len(ranked)):
                if len(selected_indices) >= policy.beam_width:
                    break
                selected_indices.add(index)
            beam = [ranked[i] for i in sorted(selected_indices)]
    best = beam[0]
    chosen_ids = {id(c) for c in best[1]}
    for candidate, _ in checked:
        if id(candidate) not in chosen_ids:
            rejected.append({"actor": candidate.actor, "action": candidate.command["action"],
                             "verdict": "not_selected", "reason": "resource conflict or lower joint value"})
    response = empty_response()
    response["roleCommandMap"] = {c.actor: c.command for c in best[1]}
    validate_response(response)
    alternatives = []
    for value_, selected_, _, _ in beam[:max(0, min(8, alternatives_limit))]:
        option = empty_response()
        option["roleCommandMap"] = {c.actor: c.command for c in selected_}
        validate_response(option)
        alternatives.append(Selection(option, selected_, [], value_))
    return Selection(response, best[1], rejected[:128], best[0], alternatives)
