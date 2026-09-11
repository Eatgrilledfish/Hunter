"""Optional summon portfolios: exact bounded quantities, hypothetical utility.

The utility is an uncalibrated policy index, not damage, score or win odds.
Unknown enemy geometry/AI prevents a validated prediction of summon returns.
"""
from dataclasses import dataclass
from functools import lru_cache
import math
import time

from .arbitration import Candidate
from .navigation import route
from .protocol import pos_json

ORDERS = ('SmallRobotSummonOrder', 'MiddleRobotSummonOrder', 'LargeRobotSummonOrder', 'BossRobotSummonOrder')
HP, ATTACK, KILL_POINTS = (40, 60, 500, 800), (5, 10, 20, 40), (1, 2, 4, 10)
ZERO = (0, 0, 0, 0)


@lru_cache(maxsize=11)
def quantities(limit):
    """All four-type nonnegative portfolios with at most limit orders."""
    return tuple(sorted(((a, b, c, d) for a in range(limit+1) for b in range(limit-a+1)
                         for c in range(limit-a-b+1) for d in range(limit-a-b-c+1)),
                        key=lambda q: (sum(q), q)))


def scenario_values(q, gold, actions):
    count = sum(q)
    hp = sum(n*h for n, h in zip(q, HP))
    attack = sum(n*p for n, p in zip(q, ATTACK))
    gift = sum(n*p for n, p in zip(q, KILL_POINTS))
    # Each firing rate, grouping and attack opportunity is an explicit scenario,
    # not an inferred upper bound on hidden defences or robot attack cadence.
    models = (('single_target', 20, 1, 1), ('area_fire', 40, max(1, min(3, count)), 0),
              ('recovered_defence', 90, 1, 0))
    return {name: hp/(fire*group)+.1*attack*opportunities-.5*gift-.01*gold-.25*actions
            for name, fire, group, opportunities in models}


@dataclass(frozen=True)
class Allocation:
    actor: str
    counts: tuple
    bought: int
    gold: int
    actions: int
    first: dict | None


def actor_options(world, actor, limit, turns, budget, purchase_slots, can_buy, deadline):
    if actor.backpack is None:
        return [Allocation(actor.id, ZERO, 0, 0, 0, None)]
    owned = tuple(actor.inventory[name] for name in ORDERS)
    travel, steps = (None, [])
    if can_buy and purchase_slots and actor.capacity is not None:
        travel, steps = route(world, actor, world.zones.get('weaponShop', ()), deadline)
    options = []
    for q in quantities(limit):
        if time.monotonic() >= deadline:
            break
        purchases = tuple(max(0, n-have) for n, have in zip(q, owned))
        held_uses = tuple(min(n, have) for n, have in zip(q, owned))
        bought = sum(purchases)
        if bought > purchase_slots or bought and (not can_buy or travel is None):
            continue
        if any(n and ORDERS[i] not in world.shop for i, n in enumerate(purchases)):
            continue
        gold = sum(n*world.shop.get(ORDERS[i], 0) for i, n in enumerate(purchases))
        if gold > budget:
            continue
        # Use owned items first, then travel once and buy/use each type in batches.
        # No transfer, future income, or same-action buy/use is assumed.
        free = (actor.capacity-len(actor.backpack)+sum(held_uses)) if actor.capacity is not None else 0
        if bought and free <= 0:
            continue
        batches = sum(math.ceil(n/free) for n in purchases if n) if bought else 0
        actions = sum(q)+(travel+batches if bought else 0)
        if actions > turns:
            continue
        first = None
        if sum(held_uses):
            index = next(i for i, n in enumerate(held_uses) if n)
            first = {'action': 'use', 'name': ORDERS[index]}
        elif bought:
            if travel:
                first = {'action': 'move', 'targetPos': [pos_json(steps[0])]}
            else:
                index = next(i for i, n in enumerate(purchases) if n)
                first = {'action': 'buy', 'name': ORDERS[index], 'num': min(free, purchases[index])}
        options.append(Allocation(actor.id, q, bought, gold, actions, first))
    return options


def plan(world, *, remaining, turns, budget, purchase_slots, pending_buys, task_actor, deadline, committed=ZERO):
    remaining = max(0, min(10, remaining))
    actors = [a for a in world.movers if a.id != task_actor][:3]
    buyers = {a.id for a in [a for a in actors if a.id not in pending_buys][:max(0, 8-len(pending_buys))]}
    report = {'status': 'complete', 'quantity_space': len(quantities(remaining)),
              'scenario_basis': 'hypothetical workload minus gift-score, gold and action penalties',
              'policy_index_is_not_game_score': True, 'allocations_considered': 0,
              'reserved_for_wave': dict(zip(ORDERS, committed))}
    baseline = min(scenario_values(committed, 0, 0).values())
    # Pareto labels for (total quantities, new purchases). Costs and actions both
    # matter: a cheaper allocation can require more role opportunities.
    states = {(ZERO, 0): [(0, 0, ())]}
    best = (0.0, ZERO, 0, 0, ())
    stopped = False
    for actor in actors:
        options = actor_options(world, actor, remaining, turns, budget, purchase_slots,
                                actor.id in buyers, deadline)
        if time.monotonic() >= deadline:
            stopped = True
            break
        following = {}
        for (counts, bought), labels in states.items():
            for option in options:
                if sum(counts)+sum(option.counts) > remaining:
                    break
                if time.monotonic() >= deadline:
                    stopped = True
                    break
                new_bought = bought+option.bought
                if new_bought > purchase_slots:
                    continue
                combined = tuple(a+b for a, b in zip(counts, option.counts))
                key = combined, new_bought
                for gold, actions, allocation in labels:
                    cost, work = gold+option.gold, actions+option.actions
                    if cost > budget:
                        continue
                    report['allocations_considered'] += 1
                    group = following.setdefault(key, [])
                    if any(c <= cost and a <= work for c, a, _ in group):
                        continue
                    group[:] = [(c, a, p) for c, a, p in group if not (cost <= c and work <= a)]
                    chosen = allocation+(option,)
                    group.append((cost, work, chosen))
                    total = tuple(a+b for a, b in zip(committed, combined))
                    utility = min(scenario_values(total, cost, work).values())-baseline
                    if (utility, -cost, -work) > (best[0], -best[2], -best[3]):
                        best = utility, combined, cost, work, chosen
                # Local memory cap; a valid best allocation survives interruption.
                if len(following) > 8192:
                    stopped = True
                    break
            if stopped:
                break
        if stopped:
            break
        states = following
    if stopped:
        report['status'] = 'budget_incomplete'
    utility, q, gold, actions, allocation = best
    total = tuple(a+b for a, b in zip(committed, q))
    report.update(counts=dict(zip(ORDERS, q)), gold=gold, actions=actions, utility=utility,
                  scenarios=scenario_values(total, gold, actions), baseline_utility=baseline,
                  allocation=[{'actor': a.actor, 'counts': dict(zip(ORDERS, a.counts)),
                               'gold': a.gold, 'actions': a.actions, 'first': a.first} for a in allocation])
    # Only first steps enter arbitration; later snapshots replan. Individual
    # first steps can be rejected by collision, task, defence or action locks.
    candidates = [Candidate(a.actor, a.first, min(6, 1+utility/max(1, len(allocation))),
                            'optional summon portfolio under explicit defence scenarios')
                  for a in allocation if a.first is not None and utility > 0]
    return candidates, report
