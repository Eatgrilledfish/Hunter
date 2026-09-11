"""Match current upgrade demand to carried vouchers before buying new stock."""
import time

from .arbitration import Candidate
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import WEAPONS, pos_json


def propose(world, policy, deadline, task_actor=None, *, plans=None, priority_ids=()):
    plans = {} if plans is None else plans
    plans.clear()
    actors = {u.id: u for u in world.movers if u.id != task_actor}
    targets = {}
    for unit in sorted(world.ours.values(), key=lambda u: (u.kind == "wall", u.id)):
        if not unit.alive or unit.level not in {1, 2}:
            continue
        prefix = "Weapon" if unit.kind in WEAPONS else "Station" if unit.kind == "station" else "Wall" if unit.kind == "wall" else None
        if prefix:
            targets[unit.id] = {"unit": unit, "name": f"{prefix}UpgradeVoucher{unit.level}",
                                "priority": 12 if prefix == "Wall" else 30,
                                "rank": 0 if unit.id in priority_ids else 1 if prefix == "Weapon" else 2 if prefix == "Station" else 3}
        if len(targets) == 16:
            break
    if not actors or not targets:
        return []
    fields = {}

    def field(actor, target):
        key = (actor.id, target["unit"].id)
        if key not in fields:
            fields[key] = distance_field(world, interaction_cells(world, [target["unit"].pos], actor.pos),
                                         actor.pos, deadline)
        return fields[key]

    supply = {identity: actor.inventory.copy() for identity, actor in actors.items()}
    jobs, result = {}, []
    # Reserve even a carrier's second matching voucher for a second target. Only
    # its first delivery is emitted this turn; another worker must not rebuy it.
    while targets and time.monotonic() < deadline:
        options = []
        for target_id, target in targets.items():
            for identity, actor in actors.items():
                if not supply[identity][target["name"]] or time.monotonic() >= deadline:
                    continue
                length = field(actor, target).get(actor.pos)
                if length is not None:
                    options.append((target["rank"], length, identity, target_id))
        if not options:
            break
        _, length, identity, target_id = min(options)
        target = targets.pop(target_id)
        supply[identity][target["name"]] -= 1
        jobs.setdefault(identity, (target, length))
    for identity, (target, length) in jobs.items():
        begin = len(result)
        actor = actors[identity]
        if length == 0:
            result.append(Candidate(identity, {"action": "use", "name": target["name"],
                                               "targetPos": [pos_json(target["unit"].pos)]}, target["priority"],
                                    "apply personal voucher to current matching building"))
        else:
            route_field = field(actor, target)
            steps = sorted(p for p in neighbours(actor.pos) if p in route_field and route_field[p] < length)
            result.extend(Candidate(identity, {"action": "move", "targetPos": [pos_json(p)]},
                                    target["priority"]*.6/(1+length*.1)-i*.01,
                                    "deliver carried upgrade voucher; do not buy replacement stock")
                          for i, p in enumerate(steps[:4]))
        plans[identity] = {"target": target["unit"].id, "name": target["name"], "steps": length+1,
                           "stage": "deliver", "candidates": result[begin:]}
    if world.gold is None or not world.zones.get("weaponShop") or not any(t["name"] in world.shop for t in targets.values()):
        return result
    gold = max(0, world.gold-policy.reserve_gold)
    buyers = {k: u for k, u in actors.items() if k not in jobs and u.kind == "worker"
              and u.capacity is not None and u.backpack is not None and len(u.backpack) < u.capacity}
    start_fields = {}
    while targets and buyers and time.monotonic() < deadline:
        options = []
        for identity, actor in buyers.items():
            if identity not in start_fields:
                start_fields[identity] = distance_field(world, [actor.pos], actor.pos, deadline)
            start = start_fields[identity]
            shops = interaction_cells(world, world.zones.get("weaponShop", ()), actor.pos)
            for target_id, target in targets.items():
                price = world.shop.get(target["name"])
                if price is None or price > gold or time.monotonic() >= deadline:
                    continue
                delivery = field(actor, target)
                paths = [(start[p]+delivery[p], start[p], p) for p in shops if p in start and p in delivery]
                if paths:
                    total, to_shop, stand = min(paths)
                    options.append((target["rank"], total, price, identity, target_id, to_shop, stand))
        if not options:
            break
        _, total, price, identity, target_id, length, stand = min(options)
        actor, target = buyers.pop(identity), targets.pop(target_id)
        begin = len(result)
        gold -= price
        if length == 0:
            result.append(Candidate(identity, {"action": "buy", "name": target["name"], "num": 1},
                                    target["priority"]/3, "buy one unfilled building upgrade after matching carried supply",
                                    gold_reserve=policy.reserve_gold))
        else:
            shop_field = distance_field(world, [stand], actor.pos, deadline)
            steps = sorted(p for p in neighbours(actor.pos) if p in shop_field and shop_field[p] < length)
            result.extend(Candidate(identity, {"action": "move", "targetPos": [pos_json(p)]},
                                    target["priority"]*.2/(1+total*.1)-i*.01,
                                    "procure assigned upgrade using shop and delivery path lengths")
                          for i, p in enumerate(steps[:4]))
        plans[identity] = {"target": target_id, "name": target["name"], "steps": total+2,
                           "stage": "procure", "candidates": result[begin:]}
    return result


def ready_upgrades(world, clock, policy, deadline, task_actor=None, *, plans=None):
    """Promote a feasible current weapon-upgrade chain above optional ore work."""
    if not policy.upgrade_commitment_enabled or clock.phases != {"day"} or time.monotonic() >= deadline:
        return []
    if plans is None:
        plans = {}
        propose(world, policy, deadline, task_actor, plans=plans)
    return [candidate for plan in plans.values()
            if plan["name"].startswith("WeaponUpgradeVoucher") and plan["steps"]+policy.return_buffer <= clock.until_night
            for candidate in plan["candidates"]]
