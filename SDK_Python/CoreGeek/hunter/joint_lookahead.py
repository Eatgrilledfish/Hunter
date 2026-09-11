"""Bounded joint tactical rollouts, never an official robot simulator.

Roots are whole, validated action sets. Each continuation jointly allocates
existing roles/weapons, then advances positions, HP, cooldowns and suppression.
Unknown robot movement/cadence/lethal-turn behavior is kept in named scenarios.
Only a fully compared first action set can replace the current incumbent.
"""
from dataclasses import dataclass, field, replace
import time
import math
from collections import defaultdict
from typing import NamedTuple

from . import combat
from .arbitration import Candidate, select
from .navigation import neighbours
from .protocol import distance, pos_json
from .rules import Clock
from .lookahead import ROBOT_DAMAGE

class Scenario(NamedTuple):
    name: str
    approaches: bool
    cadence: int
    lethal_acts: bool
    targets: str = 'all'
    anchor_distance: bool = False
    phase: int = 0


# Target selection and base interaction geometry remain hypotheses. Include
# alternatives that do not mistake nearby guns for the robots' intended target.
SCENARIOS = (Scenario("fixed", False, 1, True), Scenario("chase", True, 1, True),
             Scenario("alternate", True, 2, True, phase=1), Scenario("lethal_stops", True, 1, False),
             Scenario("nearest_anchor", True, 1, True, 'mobile_base', True),
             Scenario("base_anchor", True, 1, True, 'base', True))
TACTICAL = {"move", "attack"}


def tactical(candidate):
    c = candidate.command
    return c['action'] in TACTICAL or (c['action'] == 'use' and c.get('name') in {'Medicine', 'Bomb', 'DizzyWeapon'})


def tactical_effects(selected):
    """Group movement variants while retaining fire, controller and item choices."""
    return tuple(sorted((c.actor, c.command['action'], c.command.get('controllerId', ''),
                         c.command.get('name', ''), tuple(sorted(c.damage.items())),
                         tuple(sorted(c.suppression)))
                        for c in selected if c.command['action'] != 'move'))


@dataclass
class Memory:
    errors: dict = field(default_factory=dict)
    samples: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)

    def weights(self):
        return {s.name: math.exp(-4*self.errors.get(s.name, 0)) for s in SCENARIOS}

    def observe(self, world):
        pending, self.pending = self.pending, {}
        if pending.get('round') != world.round-1:
            return
        comparisons = {name: [] for name in self.weights()}
        for identity, old in pending.get('assets', {}).items():
            unit = world.ours.get(identity)
            if (unit is None or unit.health is None or unit.pos != old['pos']
                    or unit.health > old['hp'] or old['healing']):
                continue
            actual = old['hp']-unit.health
            for name, predicted in old['losses'].items():
                if name in comparisons:
                    comparisons[name].append((actual, predicted))
        # One aggregate comparison per observed turn. Zero-loss bystanders must
        # not erase a wrong-target prediction; unit iteration order is irrelevant.
        for name, pairs in comparisons.items():
            if not pairs:
                continue
            error = min(1, sum(abs(a-p) for a,p in pairs) /
                        max(5, sum(a for a,_ in pairs), sum(p for _,p in pairs)))
            self.errors[name] = .8*self.errors.get(name, 0)+.2*error
            self.samples[name] = self.samples.get(name, 0)+1

    def commit(self, world, selected, predictions):
        healing = {c.actor for c in selected if c.command.get('name') == 'Medicine'}
        assets = {}
        for u in world.ours.values():
            if not u.alive:
                continue
            positions = {w.ours[u.id].pos for w in predictions.values()}
            if len(positions) != 1:
                continue
            assets[u.id] = {'hp': u.health, 'pos': positions.pop(), 'healing': u.id in healing,
                            'losses': {name: max(0, u.health-w.ours[u.id].health)
                                       for name, w in predictions.items()}}
        self.pending = {'round': world.round, 'assets': assets}


def rebuild(world, ours, robots, next_round):
    # Keep unknown/partial entities, neutral cells and visible enemies blocked.
    dynamic = set().union(*(u.cells for u in list(world.ours.values())+list(world.robots.values()) if u.alive))
    occupied = world.occupied-dynamic
    # Static layers must survive even if an inconsistent dynamic entity overlaps.
    occupied |= set().union(*world.zones.values(), *(u.cells for u in world.enemies.values() if u.blocks))
    for u in list(ours.values())+list(robots.values()):
        if u.alive:
            occupied |= u.cells
    return replace(world, round=next_round, ours=ours, robots=robots, occupied=occupied)


def advance(world, clock, selected, scenario, depth, suppression=None):
    """Advance one already validated tactical bundle in a named local scenario.

    Damage uses current-snapshot candidates; lethal robots may still act in the
    conservative scenarios. Friendly movement never vacates a cell for another
    friendly move in this step. The input World/Units and commands are untouched.
    """
    approaches, cadence, lethal_acts = scenario.approaches, scenario.cadence, scenario.lethal_acts
    ours, robots = dict(world.ours), dict(world.robots)
    suppression = {k: n-1 for k, n in (suppression or {}).items() if n > 1}
    outgoing, fired = {}, set()
    for c in selected:
        action = c.command['action']
        if action == 'move':
            p = c.command['targetPos'][0]
            ours[c.actor] = replace(ours[c.actor], pos=(p['x'], p['y']))
        elif action == 'attack':
            fired.add(c.actor)
            for identity, amount in c.damage.items():
                outgoing[identity] = outgoing.get(identity, 0)+amount
        elif action == 'use':
            u = ours[c.actor]
            name = c.command['name']
            bag = list(u.backpack or ())
            bag.remove(name)
            ours[c.actor] = replace(u, backpack=tuple(bag))
            if name == 'Medicine':
                ours[c.actor] = replace(ours[c.actor], health=220 if u.kind == 'worker' else 200)
            for identity, amount in c.damage.items():
                outgoing[identity] = outgoing.get(identity, 0)+amount
            for identity in c.suppression:
                suppression[identity] = 5
    for u in world.weapons:
        cooldown = 3 if u.id in fired and u.kind == 'rocket' else (max(0, u.cooldown-1) if u.cooldown is not None else None)
        ours[u.id] = replace(ours[u.id], cooldown=cooldown)
    incoming = {}
    occupied = rebuild(world, ours, robots, world.round).occupied
    if clock.phases == {'night'}:
        for r in sorted(robots.values(), key=lambda r: r.id):
            if not r.alive or suppression.get(r.id, 0) or (r.abnormal == 'dizzy' and depth == 0):
                continue
            if not lethal_acts and outgoing.get(r.id, 0) >= r.health:
                continue
            # targetTeam is intent, not ownership; unknown intent is modelled
            # against us, known opposing intent is left as an occupied unit.
            if r.target_team and r.target_team != world.side:
                continue
            targets = [u for u in ours.values() if u.alive and
                       (scenario.targets == 'all' or u.kind == 'station' or
                        scenario.targets == 'mobile_base' and u.kind in {'worker', 'pioneer'})]
            if not targets:
                continue
            target_cells = lambda u: {u.pos} if scenario.anchor_distance else u.cells
            gap = lambda u: min(distance(r.pos, p) for p in target_cells(u))
            target = min(targets, key=lambda u: (gap(u), u.id))
            reach = r.attack_range if r.attack_range is not None else 3
            if gap(target) <= reach:
                if world.round % cadence == scenario.phase:
                    incoming[target.id] = incoming.get(target.id, 0)+ROBOT_DAMAGE.get(r.kind, r.attack_power or 0)
            elif approaches:
                choices = [p for p in neighbours(r.pos) if world.inside(p) and p not in occupied]
                if choices:
                    p = min(choices, key=lambda p: (min(distance(p, t) for t in target_cells(target)), p))
                    if min(distance(p, t) for t in target_cells(target)) < gap(target):
                        occupied.remove(r.pos)
                        occupied.add(p)
                        robots[r.id] = replace(r, pos=p)
    for identity, amount in outgoing.items():
        if identity in robots:
            robots[identity] = replace(robots[identity], health=max(0, robots[identity].health-amount))
    for identity, amount in incoming.items():
        ours[identity] = replace(ours[identity], health=max(0, ours[identity].health-amount))
    robots = {k: replace(r, abnormal='dizzy' if suppression.get(k, 0) > 1 else '')
              for k, r in robots.items()}
    next_clock = Clock(world.round+1, clock.origin)
    if next_clock.phases == {'day'}:
        robots = {k: replace(r, health=0) for k, r in robots.items()}
    return rebuild(world, ours, robots, world.round+1), suppression


def movements(world, task_actor=None, task_cells=()):
    """All eight first steps; score only a small positioning tie preference.

    The rollout, not this distance hint, determines final root value. Future
    path segments also remain inside an active task's interaction neighbourhood.
    """
    result = []
    for u in world.movers:
        guns = [w for w in world.weapons if w.cooldown is not None]
        before = min((max(0, distance(u.pos, w.pos)-1) for w in guns), default=0)
        for p in neighbours(u.pos):
            if not world.inside(p) or p in world.occupied:
                continue
            if u.id == task_actor and not any(distance(p, t) <= 1 for t in task_cells):
                continue
            after = min((max(0, distance(p, w.pos)-1) for w in guns), default=0)
            result.append(Candidate(u.id, {'action': 'move', 'targetPos': [pos_json(p)]},
                                    .05+2*(before-after), 'joint rollout positioning'))
    return result


def health_cost(before, after):
    total = 0
    for u in before.ours.values():
        if not u.alive:
            continue
        v = after.ours[u.id]
        weight = 3 if u.kind == 'station' else 1
        total += max(0, u.health-v.health)*weight
        if not v.alive:
            total += 5000 if u.kind == 'station' else 250
    return total


def gain(world, selection):
    damage = {}
    for c in selection.selected:
        for identity, amount in c.damage.items():
            damage[identity] = damage.get(identity, 0)+amount
    return sum(min(world.robots[k].health, n)+(12 if n >= world.robots[k].health else 0)
               for k, n in damage.items() if world.robots[k].alive)


def relative_advantage(values, baseline, weights, scenarios=None):
    """Equal target-family influence; variant count is not independent evidence.

    Discounts are deliberately not renormalized. Repeatedly inaccurate model
    families contribute less, and can recover through subsequent observations.
    """
    families = defaultdict(list)
    for scenario in SCENARIOS if scenarios is None else scenarios:
        families[(scenario.targets, scenario.anchor_distance)].append(scenario.name)
    if not families:
        return 0.0
    return sum(sum((values[n]-baseline[n])*weights[n] for n in names)/len(names)
               for names in families.values())/len(families)


def continuation(world, clock, rules, policy, deadline, task_actor=None, task_cells=()):
    """Joint tactical next step with the real scheduler's immediate rescue logic.

    Hypothetical task exits cannot acquire new permissions: constrain rescue
    moves to the known task neighbourhood and keep its controller lock. Future
    task submissions, shopping and macro-plan transitions remain outside scope.
    """
    from .director import triage
    guidance = triage(world, clock, task_actor, None, policy)
    options = movements(world, task_actor, task_cells) + guidance.candidates
    options += [c for c in combat.propose(world, clock, rules, deadline, task_actor,
                base_fire_enabled=policy.base_fire_enabled and policy.joint_fire_enabled) if tactical(c)]
    permitted = []
    task_moves = set()
    for candidate in options:
        if not guidance.permit(candidate):
            continue
        if candidate.actor == task_actor and candidate.command['action'] == 'move':
            target = candidate.command['targetPos'][0]
            point = target['x'], target['y']
            if not any(distance(point, cell) <= 1 for cell in task_cells):
                continue
            task_moves.add(point)
        permitted.append(candidate)
    return select(world, clock, rules, policy, permitted, deadline, task_actor=task_actor,
                  task_moves=task_moves, weights=combat.threat_weights(world))


def improve(world, clock, rules, policy, incumbent, candidates, deadline, *, memory,
            permit=lambda c: True, filter_candidates=lambda cs: cs, task_actor=None,
            task_cells=(), task_moves=(), allow_task_control=False, weights=None,
            memoize=True):
    report = {'status': 'inactive', 'scope': 'joint tactical hypothetical rollouts; joint continuations with shared immediate triage'}
    if not policy.joint_lookahead_enabled or clock.phases != {'night'} or not world.robots:
        return incumbent, report, {}
    if len(world.movers) > 3 or len(world.weapons) > 3 or len(world.robots) > 128:
        report['status'] = 'size_limit'
        return incumbent, report, {}
    if any(not tactical(c) for c in incumbent.selected):
        report['status'] = 'protected_non_tactical_action'
        return incumbent, report, {}
    report['status'] = 'budget_incomplete'
    if time.monotonic() >= deadline:
        return incumbent, report, {}
    bounded = replace(policy, beam_width=8, max_candidates=96)
    pool = [c for c in candidates if tactical(c)] + movements(world, task_actor, task_cells)
    pool = filter_candidates([c for c in pool if permit(c)])
    root_selection = select(world, clock, rules, bounded, pool, deadline,
                            task_actor=task_actor, task_moves=task_moves, allow_task_control=allow_task_control,
                            weights=weights, incumbent=incumbent.selected, alternatives_limit=8,
                            diversity_key=tactical_effects)
    roots = [incumbent]
    signatures = {repr(sorted(incumbent.response['roleCommandMap'].items()))}
    # Compare distinct tactical bundles before spending roots on movement-only
    # variants of the same volley. The original incumbent always remains root0.
    effects = {tactical_effects(incumbent.selected)}
    diverse, remaining = [], []
    for root in root_selection.alternatives:
        effect = tactical_effects(root.selected)
        if effect not in effects:
            effects.add(effect)
            diverse.append(root)
        else:
            remaining.append(root)
    for root in diverse + remaining:
        signature = repr(sorted(root.response['roleCommandMap'].items()))
        if signature not in signatures:
            signatures.add(signature)
            roots.append(root)
        if len(roots) == 6:
            break
    horizon = max(4, min(8, policy.lookahead_horizon))
    scenario_weights = memory.weights()
    evaluated = []
    outcomes = []
    predictions = []
    continuation_cache = {}
    cache_hits = 0
    continuation_evaluations = 0
    for root in roots:
        values, first_worlds = {}, {}
        for scenario in SCENARIOS:
            state, suppressed = advance(world, clock, root.selected, scenario, 0)
            first_worlds[scenario[0]] = state
            value = -health_cost(world, state)
            for depth in range(1, horizon):
                if time.monotonic() >= deadline:
                    return incumbent, report, {}
                step_clock = Clock(state.round, clock.origin)
                if step_clock.phases != {'night'} or not any(r.alive for r in state.robots.values()):
                    break
                # All other World fields, policy, clock origin and task context
                # are constant inside this invocation. Unit values are frozen;
                # include every changing unit and occupancy layer, not a nearby
                # subset. Cache never crosses callbacks or model-memory updates.
                key = (state.round, tuple(sorted(state.ours.items())),
                       tuple(sorted(state.robots.items())), frozenset(state.occupied))
                step = continuation_cache.get(key) if memoize else None
                if step is None:
                    step = continuation(state, step_clock, rules, bounded, deadline, task_actor, task_cells)
                    continuation_evaluations += 1
                    if memoize and time.monotonic() < deadline:
                        continuation_cache[key] = step
                else:
                    cache_hits += 1
                after, suppressed = advance(state, step_clock, step.selected, scenario, depth, suppressed)
                value += (.85**depth)*(gain(state, step)-health_cost(state, after))
                state = after
            values[scenario[0]] = value
        outcomes.append(values)
        # Discount improvements relative to the intact incumbent. Normalizing
        # by the weight sum would cancel a universal loss of model reliability.
        advantage = relative_advantage(values, outcomes[0], scenario_weights)
        evaluated.append(root.value+policy.lookahead_weight*advantage)
        predictions.append(first_worlds)
    if time.monotonic() >= deadline:
        return incumbent, report, {}
    best = max(range(len(roots)), key=lambda i: (evaluated[i], -i))
    report.update(status='complete', horizon=horizon, roots=len(roots), values=evaluated,
                  root_tactical_effects=len({tactical_effects(r.selected) for r in roots}),
                  continuation_evaluations=continuation_evaluations,
                  continuation_cache_hits=cache_hits,
                  immediate_values=[r.value for r in roots],
                  selected_root=best, weights=scenario_weights, scenario_values=outcomes,
                  scenarios=[s[0] for s in SCENARIOS], assumptions='target choice/base anchor or footprint/greedy movement/absolute cadence phase/lethal-turn behavior are alternatives, not rules')
    return roots[best], report, predictions[best]
