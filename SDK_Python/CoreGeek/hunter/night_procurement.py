"""One observed night purchase, with a separately labelled dawn delivery proof.

Only the current topology generates commands. Removing the planned level-one
gate in a copy is an estimate for tomorrow's delivery, never a move permission.
The caller owns purchase receipts, role permissions and its existing gate state.
"""
from copy import copy
from dataclasses import dataclass, field
import time

from .arbitration import Candidate
from .day_schedule import weighted_field
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import MINERALS, pos_json
from .rules import station_rings
from .task_side_layout import BudgetExpired
from . import procurement


@dataclass
class NightPurchase:
    candidate: Candidate | None = None
    diagnostic: dict = field(default_factory=dict)
    # Costs include one buy, actual return to G, one dawn remove and the caller's
    # existing margin. This is a current-topology field, not the dawn copy.
    shop_tail: dict | None = None
    target: str | None = None
    name: str | None = None
    price: int | None = None
    reserve: int = 0
    assignments: tuple = ()


def _check(deadline):
    if time.monotonic() >= deadline:
        raise BudgetExpired


def _weighted(world, actor, costs, deadline):
    _check(deadline)
    result = weighted_field(world, costs, actor, deadline)
    if result is None:
        raise BudgetExpired
    _check(deadline)
    return result


def _view(world, blocked):
    view = copy(world)
    view.occupied = set(blocked)
    return view


def prepare(world, clock, rules, policy, actor, reach, home, blocked, remaining,
            deadline, *, task_actor=None, open_exit=None, night_boundary=frozenset()):
    """Return one funded buy/shop move and a reusable vendor-to-shop tail.

    ``reach`` and ``home`` must be complete current-topology M distance maps;
    home has zero-cost exterior G neighbours. ``blocked`` includes the gate
    caller's observed danger/navigation exclusions. Expiration raises
    BudgetExpired, allowing the existing gate transaction to discard all work.

    An unfunded/full-bag plan may still expose shop_tail for sale_tail's
    *estimate*. It never creates a buy against expected mining or sale income.
    No action or field in this result opens/traverses the conditional gate.
    """
    _check(deadline)
    answer = NightPurchase(diagnostic={'status': 'not an economic night window'})
    plan = getattr(world, 'task_side_plan', None)
    if (clock.phases != {'night'} or clock.day is None or not 1 <= clock.day < 10
            or not plan or not actor or not actor.alive or actor.kind != 'worker'
            or actor.id in getattr(world, 'night_defenders', ())):
        if clock.day == 10:
            answer.diagnostic['status'] = 'no next dawn in the final half-day'
        return answer
    if actor.backpack is None or actor.capacity is None or world.gold is None:
        answer.diagnostic['status'] = 'personal stock capacity or current gold unknown'
        return answer
    blue, yellow = station_rings(plan['anchor'])
    gate = plan['gate'] if open_exit is None else open_exit
    gate_walls = [u for u in world.ours.values()
                  if u.alive and u.kind == 'wall' and u.pos == gate]
    opened = open_exit is not None and gate in yellow and gate not in world.occupied
    if not opened and (len(gate_walls) != 1 or gate_walls[0].level != 1):
        answer.diagnostic['status'] = 'no observed removable level-one gate'
        return answer
    # Do not remove a second object sharing G, a failed navigation exclusion,
    # another wall or a currently occupied character from the conditional map.
    gate_wall = gate_walls[0] if gate_walls else None
    other_blocker = any(gate in u.cells and u.blocks and (gate_wall is None or u.id != gate_wall.id)
                        for group in (world.ours, world.enemies, world.robots)
                        for u in group.values())
    other_blocker |= any(gate in cells for cells in world.zones.values())
    if other_blocker or gate in world.navigation_avoided.get(actor.pos, ()):
        answer.diagnostic['status'] = 'gate remains blocked after removing its wall'
        return answer
    exterior = {p for p, cost in home.items() if cost == 0 and p in neighbours(gate)
                and p not in blue | yellow and p in reach and p not in blocked}
    if actor.pos not in home or actor.pos not in reach or not exterior:
        answer.diagnostic['status'] = 'no actual economic-worker dawn opener route'
        return answer

    actual = _view(world, (set(blocked) | world.occupied) - {actor.pos})
    projected = copy(world)
    projected.occupied = (world.occupied if opened else world.occupied - {gate}) | (
        set(blocked) - world.occupied - set(night_boundary))
    # All roles and their exact personal counts remain present. Only M's route
    # lookup may use the conditional gate; W/P's stock needs an actual route.
    targets, rank, restricted, priority_ids = procurement.upgrade_demand(
        world, policy, rules=rules)
    actors = {u.id: u for u in world.movers if u.id != task_actor}
    fields = {}

    def delivery(carrier, target):
        key = carrier.id, target['unit'].id
        if key not in fields:
            _check(deadline)
            view = projected if carrier.id == actor.id else world
            fields[key] = distance_field(
                view, interaction_cells(view, [target['unit'].pos], carrier.pos),
                carrier.pos, deadline)
            _check(deadline)  # distance_field may otherwise publish a partial BFS.
        return fields[key]

    remaining_targets, _, allocations = procurement.match_carried_supply(
        targets, actors, deadline, delivery)
    _check(deadline)
    carried = [target for identity, target, _ in allocations if identity == actor.id]
    from .purchase_roles import permitted
    remaining_targets = {i:t for i,t in remaining_targets.items() if permitted(world, actor.id, t['name'])}

    def dawn_budget(entries):
        circuits = [delivery(actor, entry) for entry in entries]
        costs = [int(not opened) + 2 + sum(2 * route[p] + 1 for route in circuits)
                 for p in exterior if all(p in route for route in circuits)]
        return max(costs) if len(costs) == len(exterior) else None

    held_dawn_required = dawn_budget(carried)
    if held_dawn_required is None or held_dawn_required > 70:
        answer.diagnostic['status'] = 'current personal delivery stock has no complete dawn budget'
        return answer
    answer.assignments = tuple({'actor': identity, 'target': target['unit'].id,
                                'name': target['name'], 'steps': steps,
                                'conditional_dawn': identity == actor.id and not opened,
                                'dawn_required': held_dawn_required if identity == actor.id else None,
                                'gate_id': gate_wall.id if identity == actor.id and gate_wall else None}
                               for identity, target, steps in allocations)
    remaining_targets = {key: target for key, target in remaining_targets.items()
                         if not restricted or rank is not None and target['rank'] == rank}
    reserve = procurement.purchase_floor(world, policy, remaining_targets, priority_ids)
    shops = interaction_cells(actual, world.zones.get('weaponShop', ()), actor.pos)
    seeds = {p: 1 + home[p] + int(not opened) + policy.return_buffer
             for p in shops if p in home and p in reach}
    if not seeds:
        answer.diagnostic.update(status='no actual shop and exterior gate route',
                                 held_assignments=list(answer.assignments))
        return answer
    options = []
    for identity, target in remaining_targets.items():
        _check(deadline)
        price = world.shop.get(target['name'])
        if price is None or price < 0:
            continue
        # Each reserved coupon has a concrete target and one complete external
        # G -> use -> G excursion. Summing them is conservative and also covers
        # multiple observed coupons without inventing a transfer or new level.
        dawn_required = dawn_budget(carried + [target])
        if dawn_required is None:
            continue  # The caller may return to any of its actual zero-cost ends.
        if dawn_required > 70:
            continue
        night_required, stand = min((reach[p] + cost, p) for p, cost in seeds.items())
        options.append((target['rank'], night_required + dawn_required, price,
                        identity, stand, target, dawn_required, night_required))
    if not options:
        answer.diagnostic.update(status='no uncovered current-tier deliverable voucher',
                                 held_assignments=list(answer.assignments))
        return answer
    funded_options = [row for row in options
                      if row[2] + reserve <= world.gold and row[-1] < remaining]
    _, _, price, identity, stand, target, dawn_required, night_required = min(funded_options or options)
    answer.target, answer.name, answer.price, answer.reserve = identity, target['name'], price, reserve
    answer.shop_tail = _weighted(actual, actor, seeds, deadline)
    funded = world.gold >= price + reserve
    capacity = len(actor.backpack) < actor.capacity
    answer.diagnostic = dict(
        status='planned dawn delivery', target=identity, name=answer.name,
        target_level=target['unit'].level, target_pos=target['unit'].pos,
        gate_id=gate_wall.id if gate_wall else None, gate=gate, opened_observed=opened,
        conditional_dawn=not opened, dawn_required=dawn_required,
        held_assignments=list(answer.assignments), price=price, gold=world.gold,
        reserve=reserve, funded_observed=funded, capacity_observed=capacity,
        required=night_required, remaining=remaining, return_steps=home[stand],
        shop_stand=stand, sale_income_spendable=False)
    if not funded or not capacity:
        answer.diagnostic['status'] = 'requires observed sale income or personal capacity'
        return answer
    if night_required >= remaining:
        answer.diagnostic['status'] = 'purchase and return exceed night deadline'
        return answer
    if actor.pos in seeds and seeds[actor.pos] < remaining:
        command = {'action': 'buy', 'name': answer.name, 'num': 1}
        answer.diagnostic.update(required=seeds[actor.pos], return_steps=home[actor.pos],
                                 shop_stand=actor.pos, status='buy with actual gold')
    else:
        # This field contains only the currently observed closed topology.
        to_shop = _weighted(actual, actor, {stand: 0}, deadline)
        steps = sorted(p for p in neighbours(actor.pos)
                       if p in to_shop and to_shop[p] < to_shop.get(actor.pos, 0))
        if not steps:
            answer.diagnostic['status'] = 'no current legal step to shop'
            return answer
        command = {'action': 'move', 'targetPos': [pos_json(steps[0])]}
        answer.diagnostic['status'] = 'walk to observed shop'
    _check(deadline)
    answer.candidate = Candidate(actor.id, command, 100,
                                 'night purchase with conditional dawn delivery and actual gate return',
                                 gold_reserve=reserve)
    return answer


def sale_tail(world, actor, purchase, home, blocked, stock, policy, deadline, *,
              extra_mineral=None):
    """Price-aware vendor -> optional shop -> G cost, with no spend permission.

    ``stock`` is current personally saleable inventory after door-stone reserve.
    Optional extra_mineral budgets exactly one prospective collect, including
    its possible new sell action; it never changes observed funds or inventory.
    Return ``(complete_cost_field, diagnostic)``. The field starts *after* a
    collect, so its caller adds the real path to that mine and one collect.
    """
    _check(deadline)
    valid = {name: min(count, actor.inventory[name]) for name, count in stock.items()
             if name in MINERALS and isinstance(count, int) and count > 0
             and actor.inventory[name] > 0 and world.vendor.get(name, 0) > 0}
    if (extra_mineral is not None and extra_mineral in MINERALS
            and world.vendor.get(extra_mineral, 0) > 0):
        valid[extra_mineral] = valid.get(extra_mineral, 0) + 1
    value = sum(count * world.vendor[name] for name, count in valid.items())
    sale_actions = len(valid)
    capacity_after = (actor.capacity is not None and actor.backpack is not None
                      and len(actor.backpack) + int(extra_mineral is not None)
                      - sum(valid.values()) < actor.capacity)
    via_shop = bool(purchase.shop_tail is not None and purchase.price is not None
                    and world.gold is not None and capacity_after
                    and world.gold + value >= purchase.price + purchase.reserve)
    tail = purchase.shop_tail if via_shop else {
        p: cost + 1 + policy.return_buffer for p, cost in home.items()}
    view = _view(world, (set(blocked) | world.occupied) - {actor.pos})
    vendors = interaction_cells(view, world.zones.get('vendor', ()), actor.pos)
    result = (_weighted(view, actor, {p: tail[p] + sale_actions for p in vendors if p in tail}, deadline)
              if valid else dict(tail))
    _check(deadline)
    return result, dict(via_shop=via_shop, sale_actions=sale_actions,
                        quoted_stock_value=value, prospective_mineral=extra_mineral,
                        expected_income_spendable=False, target=purchase.target if via_shop else None)
