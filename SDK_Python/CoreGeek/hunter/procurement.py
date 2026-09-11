"""Match current upgrade demand to carried vouchers before buying new stock."""
import time
from itertools import product

from .arbitration import Candidate
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import WEAPONS, distance, pos_json


def gatling_upgrade_status(world, unit, policy, rules):
    """Policy eligibility, separate from voucher legality and current stock."""
    if unit.kind != 'gatling':
        return 'normal', None
    if unit.level == 3:
        return 'maxed', None
    if unit.level not in (1,2):
        return 'unknown_level', None
    if world.battery_plan is None or policy is None:
        return 'normal', None
    maximum = rules.max_health.get('gatling',{}).get(unit.level) if rules else None
    threshold = maximum*policy.gatling_upgrade_health_fraction if maximum else None
    if threshold is not None and unit.health is not None and unit.health <= threshold:
        return 'heal', threshold
    if unit.level == 1:
        # The first upgrade extends the short initial firing range. Holding a
        # healthy level-1 gun for later healing would leave that range deficit
        # in place through the entire first night.
        return 'range_priority', threshold
    rockets = [u for u in world.weapons if u.kind == 'rocket']
    if rockets and all(u.level == 3 for u in rockets):
        return 'rocket_maxed', threshold
    return 'hold_for_health', threshold


def upgrade_allowed(world, unit, policy, rules):
    if (world.battery_plan is not None and unit.kind == 'rocket' and unit.level == 1
            and any(g.kind == 'gatling' and g.level == 1 for g in world.weapons)):
        # WeaponUpgradeVoucher1 is shared. An incidental stop beside the rear
        # rocket must not consume a voucher being delivered to the front guns.
        # Already held WeaponUpgradeVoucher2 cannot upgrade a level-1 Gatling,
        # so a level-2 rocket remains free to use that separate stock.
        return False
    return gatling_upgrade_status(world,unit,policy,rules)[0] not in {'hold_for_health','maxed','unknown_level'}


def urgent_gatling_upgrades(world, policy, rules, task_actor=None, *, priority_ids=()):
    """Commit held vouchers to adjacent emergency/range upgrades in either phase.

    Eligibility alone cannot beat the attack selector. The caller must install
    these actions as emergency commitments, including both actor and gun locks.
    A carrier with a matching voucher for a critical base remains available to
    the base-recovery plan. No future purchase, transfer, or healing is assumed.
    """
    critical = [world.ours[i] for i in priority_ids if i in world.ours
                and world.ours[i].alive and world.ours[i].kind == 'station'
                and world.ours[i].level in (1, 2)]
    actors = sorted((u for u in world.movers if u.id != task_actor and u.backpack is not None
                     and not any(u.inventory[f'StationUpgradeVoucher{b.level}'] for b in critical)),
                    key=lambda u:u.id)[:3]
    def priority(gun):
        state, _ = gatling_upgrade_status(world,gun,policy,rules)
        maximum = rules.max_health.get('gatling',{}).get(gun.level) if rules else None
        return (state != 'heal', gun.health/maximum if maximum else 1, gun.id)
    guns = sorted((g for g in world.weapons if g.kind == 'gatling' and g.level in (1, 2)
                   and g.health is not None
                   and gatling_upgrade_status(world,g,policy,rules)[0] in {'heal','range_priority'}),
                  key=priority)[:3]
    if not guns or not actors:
        return []
    choices = [[None]+[u.id for u in actors if distance(u.pos,g.pos) <= 1
                       and u.inventory[f'WeaponUpgradeVoucher{g.level}']]
               for g in guns]
    best = None
    for identities in product(*choices):
        used = [i for i in identities if i is not None]
        if len(used) != len(set(used)):
            continue
        # Cover as many upgradeable guns as possible, then emergency restoration
        # before initial range upgrades and the lowest current health ratio.
        # Deterministic matching avoids a flexible carrier taking the only
        # voucher/stand available for the other gun.
        rank = (-len(used), tuple(i is None for i in identities),
                tuple(i or '' for i in identities))
        if best is None or rank < best[0]:
            best = rank, identities
    return [Candidate(identity, {'action':'use','name':f'WeaponUpgradeVoucher{gun.level}',
                                 'targetPos':[pos_json(gun.pos)]}, 1000,
                      'upgrade low-health Gatling with held voucher before firing'
                      if gatling_upgrade_status(world,gun,policy,rules)[0]=='heal'
                      else 'upgrade Gatling to level 2 for initial firing range')
            for gun,identity in zip(guns,best[1]) if identity is not None]


def propose(world, policy, deadline, task_actor=None, *, plans=None, priority_ids=(), rules=None):
    plans = {} if plans is None else plans
    plans.clear()
    actors = {u.id: u for u in world.movers if u.id != task_actor}
    targets = {}
    for unit in sorted(world.ours.values(), key=lambda u: (u.kind == "wall", u.id)):
        if not unit.alive or unit.level not in {1, 2}:
            continue
        if not upgrade_allowed(world,unit,policy,rules):
            continue
        gatling_state, _ = gatling_upgrade_status(world,unit,policy,rules)
        prefix = "Weapon" if unit.kind in WEAPONS else "Station" if unit.kind == "station" else "Wall" if unit.kind == "wall" else None
        if prefix:
            targets[unit.id] = {"unit": unit, "name": f"{prefix}UpgradeVoucher{unit.level}",
                                "priority": 12 if prefix == "Wall" else 30,
                                "rank": 0 if unit.id in priority_ids else .5 if gatling_state == 'heal'
                                        else .75 if gatling_state == 'range_priority'
                                        else 1 if prefix == "Weapon" and (unit.kind == "rocket" or not world.build_interior)
                                        else 2 if prefix == "Weapon" else 3 if prefix == "Station" else 4}
        if len(targets) == 16:
            break
    if not actors or not targets:
        return []
    # Fix the purchasing tier before assigning held stock. Held Gatling range
    # vouchers and rocket vouchers are not observed upgrades yet; their pending
    # deliveries must not release this tier's budget to lower-priority work.
    critical = {i:t for i,t in targets.items() if i in priority_ids and t['name'] in world.shop}
    purchase_rank = min((t['rank'] for t in critical.values()), default=None)
    restrict_purchases = bool(critical) or world.battery_plan is not None or bool(world.build_interior and policy.closed_ring_rockets_enabled)
    if restrict_purchases and purchase_rank is None:
        # A missing listing is not permission to spend the leading upgrade fund
        # on cheaper walls. Wait for a current price/listing instead of inventing
        # an offline purchase or quietly changing the requested priority.
        purchase_rank = min((t['rank'] for t in targets.values()), default=None)
        rockets = [u for u in world.weapons if u.kind == 'rocket']
        # An absent/unknown-level rocket cannot be treated as fully upgraded.
        # Continue emergency restoration and the initial Gatling range upgrades,
        # but wait for its construction/current level before spending the
        # routine budget on lower-priority buildings.
        if (not rockets or any(u.level not in (1,2,3) for u in rockets)) and (
                purchase_rank is None or purchase_rank >= 1):
            purchase_rank = None
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
    # Do not turn the base's restoration fund into cheap wall vouchers while
    # saving for a damaged base. Carried vouchers above are still delivered.
    if restrict_purchases:
        targets = {i:t for i,t in targets.items() if purchase_rank is not None and t['rank'] == purchase_rank}
    purchase_reserve = policy.reserve_gold
    if ((world.battery_plan is not None or world.build_interior and policy.closed_ring_rockets_enabled) and targets and all(
            identity in priority_ids or target['unit'].kind in WEAPONS and target['rank'] <= 1
            for identity,target in targets.items())):
        # The current first-range/emergency/rocket priority IS the defensive
        # purpose of this cash. Requiring another generic 20 gold would make
        # exactly 100 fail to buy its 100-gold voucher indefinitely. Ordinary
        # wall/base spending and independent medical stock retain their floor.
        purchase_reserve = 0
    gold = max(0, world.gold-purchase_reserve)
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
                                    gold_reserve=purchase_reserve))
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


def ready_upgrades(world, clock, policy, deadline, task_actor=None, *, plans=None, rules=None):
    """Promote a feasible current weapon-upgrade chain above optional ore work."""
    if not policy.upgrade_commitment_enabled or clock.phases != {"day"} or time.monotonic() >= deadline:
        return []
    if plans is None:
        plans = {}
        propose(world, policy, deadline, task_actor, plans=plans, rules=rules)
    return [candidate for plan in plans.values()
            if plan["steps"]+policy.return_buffer+(8 if world.defence_cells else 0) <= clock.until_night
            for candidate in plan["candidates"]
            if plan["name"].startswith("WeaponUpgradeVoucher") or (world.build_interior and
                world.ours[candidate.actor].health >= (100 if world.ours[candidate.actor].kind == "pioneer" else 110))]
