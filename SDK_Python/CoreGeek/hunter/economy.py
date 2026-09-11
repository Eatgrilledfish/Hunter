"""Executable route economics, returning operators, and verified-rule construction."""
import time
from collections import Counter

from .arbitration import Candidate
from .navigation import route, interaction_cells, distance_field, neighbours
from .protocol import MINERALS, WEAPONS, distance, pos_json
from . import procurement
from .layout import LayoutGuard
from .weapon_portfolio import WeaponPortfolio


def movement(actor, steps, value, reason, *, route_goal=None):
    return [Candidate(actor.id, {"action": "move", "targetPos": [pos_json(p)]}, value-i*0.01, reason, route_goal=route_goal)
            for i, p in enumerate(steps[:4])]


def construction_cash_reserve(world, policy):
    # Establish two guns before saving for replenishment. A cash reserve with
    # no current shop/Medicine has no executable use and must not block builds.
    if (policy is None or len(world.weapons) < 2 or not world.zones.get("weaponShop")
            or world.shop.get("Medicine", 0) <= 0):
        return 0
    return policy.reserve_gold


def construction_jobs(world, rules, policy=None):
    if policy is not None and policy.weapon_portfolio_enabled:
        try:
            return _construction_jobs(world, rules, portfolio=True, repair_saturation=policy.portfolio_saturation_enabled, reserve_gold=construction_cash_reserve(world, policy))
        except TimeoutError:
            pass  # Discard the whole partial portfolio, retain complete baseline.
    return _construction_jobs(world, rules, reserve_gold=construction_cash_reserve(world, policy))


def _construction_jobs(world, rules, portfolio=False, repair_saturation=False, reserve_gold=0):
    """Assign prospective material jobs within current gold, cells and slots.

    Materials remain personal. This is recomputed from observations every turn;
    it is not a promise that another action cannot spend the same future gold.
    """
    if not world.stations or world.gold is None:
        return {}
    evaluator = WeaponPortfolio(world, rules, time.monotonic()+0.025, repair_saturation=repair_saturation) if portfolio else None
    planned = []
    workers = {u.id: u for u in world.movers if u.kind == "worker"}
    available = {name: (rule, {p for p in rule.cells if world.inside(p) and p not in world.occupied})
                 for name in sorted(WEAPONS | {"wall"}) if (rule := rules.build_rule(world, name)) is not None}
    result, reserved_cells = {}, set()
    gold, slots = max(0, world.gold-reserve_gold), max(0, rules.weapon_limit-len(world.weapons))
    wall_slots = max(0, rules.wall_limit-rules.wall_count(world))
    # Optional walls must not tie up the entire workforce while cash is needed.
    earnable = bool(world.zones.get("vendor")) and any(world.vendor.get(k, 0)>0 and world.zones.get(k) for k in MINERALS)
    wall_workers = max(0, len(workers)-1) if earnable else len(workers)
    while workers:
        options = []
        for name, (rule, cells) in available.items():
            if rule.gold > gold or (name in WEAPONS and not slots) or (name == "wall" and (not wall_slots or wall_workers <= 0)):
                continue
            for identity, actor in workers.items():
                targets = cells-reserved_cells
                if not targets:
                    continue
                deficit = sum(max(0, n-actor.inventory[k]) for k, n in rule.items.items())
                ordered = sorted(targets, key=lambda p: (distance(actor.pos,p),p))
                for target in ordered[:4] if evaluator and name in WEAPONS else ordered[:1]:
                    gain = evaluator.marginal(name,target,planned) if evaluator and name in WEAPONS else 0
                    options.append((name == "wall", deficit, distance(actor.pos,target)-gain/20, identity, name, target))
        if not options:
            break
        _, _, _, identity, name, target = min(options)
        rule = available[name][0]
        result[identity] = {"name": name, "target": target, "items": Counter(rule.items)}
        reserved_cells.add(target)
        gold -= rule.gold
        slots -= int(name in WEAPONS)
        wall_slots -= int(name == "wall")
        wall_workers -= int(name == "wall")
        if name in WEAPONS:
            planned.append((name,1,target))
        workers.pop(identity)
    return result


def construction_reservations(world, rules, *, jobs=None):
    return {identity: job["items"] for identity, job in (construction_jobs(world, rules) if jobs is None else jobs).items()}


def ready_construction(world, clock, rules, policy, deadline, *, jobs=None):
    """Complete funded weapon jobs once personal materials are ready.

    The existing build policy chooses jobs. This prevents monetary bids from
    starving their finishing actions; it does not choose an optimal gun mix.
    """
    if not policy.construction_commitment_enabled or clock.phases != {"day"}:
        return []
    result = []
    for identity, job in (construction_jobs(world, rules, policy) if jobs is None else jobs).items():
        actor = world.ours[identity]
        if (time.monotonic() >= deadline or job["name"] not in WEAPONS or actor.backpack is None
                or any(actor.inventory[name] < amount for name, amount in job["items"].items())):
            continue
        result.extend(construction(world, clock, rules, actor, deadline, names={job["name"]}, target_cell=job["target"], finish_before_night=True, reserve_gold=construction_cash_reserve(world, policy)))
    return result


def construction_materials(world, rules, actor):
    return construction_reservations(world, rules).get(actor.id, Counter())


def immediate(world, rules, task_actor=None, *, jobs=None, policy=None):
    """Cheap, current-snapshot incumbent available before advanced planning."""
    result = []
    reserves = construction_reservations(world, rules, jobs=jobs)
    for actor in world.movers:
        if actor.id == task_actor:
            continue
        materials = reserves.get(actor.id, Counter())
        if actor.inventory["Medicine"]:
            # Mobile maximum HP is specified in the taskbook.
            maximum = 200 if actor.kind == "pioneer" else 220
            keep_stock = policy is not None and policy.medical_stock_enabled
            if actor.health < maximum and (not keep_stock or actor.health*2 <= maximum or actor.inventory['Medicine'] > 1):
                result.append(Candidate(actor.id, {"action": "use", "name": "Medicine"},
                                        (maximum-actor.health)*0.3, "restore known mobile HP"))
        if world.near_zone(actor.pos, "vendor"):
            for mineral in sorted(MINERALS):
                count = max(0, actor.inventory[mineral]-materials[mineral])
                if count and mineral in world.vendor:
                    result.append(Candidate(actor.id, {"action": "sell", "name": mineral, "num": count},
                                            count*world.vendor[mineral], "realize personal ore at current price"))
        if actor.kind == "worker":
            for mineral in sorted(MINERALS):
                for pos in sorted(world.zones.get(mineral, ())):
                    if distance(actor.pos, pos) <= 1:
                        result.append(Candidate(actor.id, {"action": "collect", "targetPos": [pos_json(pos)]},
                                                world.vendor.get(mineral, 0)*0.4 + (8 if actor.inventory[mineral] < materials[mineral] else 0),
                                                "collect current adjacent ore, including verified construction need"))
        for building in world.ours.values():
            if not building.alive or distance(actor.pos, building.pos) > 1:
                continue
            prefix = "Weapon" if building.kind in WEAPONS else "Station" if building.kind == "station" else "Wall" if building.kind == "wall" else None
            if prefix and building.level in {1, 2}:
                name = f"{prefix}UpgradeVoucher{building.level}"
                if actor.inventory[name]:
                    result.append(Candidate(actor.id, {"action": "use", "name": name, "targetPos": [pos_json(building.pos)]},
                                            30 if prefix != "Wall" else 12, "use carried level-matched voucher"))
            max_hp = rules.max_health.get(building.kind, {}).get(building.level)
            if building.kind == "wall" and actor.inventory["WallFixer"] and max_hp and building.health < max_hp:
                result.append(Candidate(actor.id, {"action": "use", "name": "WallFixer", "targetPos": [pos_json(building.pos)]},
                                        (max_hp-building.health)*0.05, "repair against verified maximum HP"))
    return result


def operator_goals(world, actor):
    """Prefer stands adjacent to multiple weapons, preserving future rotations."""
    weapons = world.weapons
    cells = interaction_cells(world, [u.pos for u in weapons], actor.pos)
    return sorted(cells, key=lambda p: (-sum(distance(p, w.pos) <= 1 for w in weapons),
                                      distance(actor.pos, p), p))


def construction(world, clock, rules, actor, deadline, *, names=None, target_cell=None, finish_before_night=False, reserve_gold=0):
    if clock.phases != {"day"} or not world.stations:
        return []
    station = world.stations[0]
    result = []
    layout = LayoutGuard(world, deadline)
    options = []
    for name in sorted(names if names is not None else WEAPONS | {"wall"}):
        rule = rules.build_rule(world, name)
        if rule is None or rule.gold > max(0, (world.gold or 0)-reserve_gold) or any(actor.inventory[k] < n for k, n in rule.items.items()):
            continue
        if name in WEAPONS and len(world.weapons) >= rules.weapon_limit:
            continue
        if name == "wall" and rules.wall_count(world) >= rules.wall_limit:
            continue
        for target in sorted(rule.cells):
            if target_cell is not None and target != target_cell:
                continue
            if world.inside(target) and target not in world.occupied:
                options.append((distance(station.pos, target)+distance(actor.pos, target), name, target))
    for _, name, target in sorted(options)[:12]:
        if time.monotonic() >= deadline:
            break
        blocked = {target}
        stands = interaction_cells(world, [target], actor.pos, blocked)
        if name in WEAPONS and len(stands) < 2:
            continue
        preview = Candidate(actor.id, {"action": "build", "name": name, "targetPos": [pos_json(target)]}, 0, "layout preview")
        if not layout.check([preview])[0]:
            continue
        shared = max((sum(distance(p, w.pos) <= 1 for w in world.weapons) for p in stands), default=0)
        value = (26 if name in WEAPONS else 3) + shared*3
        length, steps = route(world, actor, [target], deadline, blocked)
        if finish_before_night and (length is None or length+1 > clock.until_night):
            continue
        if length == 0:
            result.append(Candidate(actor.id, {"action": "build", "name": name, "targetPos": [pos_json(target)]}, value,
                                    "verified build mask/cost; connected access and operator stands"))
        elif length is not None:
            result.extend(movement(actor, steps, value/(length+1), "approach verified construction site"))
    return result


def propose(world, clock, rules, policy, deadline, task_actor=None, operator_stands=None, *, upgrade_candidates=None, build_jobs=None):
    result = (procurement.propose(world, policy, deadline, task_actor)
              if upgrade_candidates is None else list(upgrade_candidates))
    if build_jobs is None:
        build_jobs = construction_jobs(world, rules, policy)
    reserves = construction_reservations(world, rules, jobs=build_jobs)
    for actor in world.movers:
        if actor.id == task_actor or time.monotonic() >= deadline:
            continue
        if actor.kind == "pioneer":
            # Navigation toward tasks is useful before task eligibility; accepting
            # and holding tasks belongs exclusively to the task engine.
            if not world.phase_task:
                for task in world.tasks:
                    if task.get("isValid") is not True or task.get("coldDownRounds") != 0:
                        continue
                    cells = world.task_cells(task)
                    if not cells:
                        continue
                    length, steps = route(world, actor, cells, deadline)
                    if length:
                        result.extend(movement(actor, steps, 8/(1+length*0.1), "approach available own task"))
            continue
        # Return using actual route length, not a fixed last-day-round trigger.
        defence = ([operator_stands[actor.id]] if operator_stands and actor.id in operator_stands else operator_goals(world, actor))
        defence_field = distance_field(world, defence, actor.pos, deadline) if defence else {}
        return_length = defence_field.get(actor.pos)
        must_return = return_length is not None and (clock.phases != {"day"} or clock.until_night <= return_length+policy.return_buffer)
        if must_return:
            if return_length:
                steps = sorted(p for p in neighbours(actor.pos) if p in defence_field and defence_field[p] < return_length)
                result.extend(movement(actor, steps, 35, "return to weapon before night using path length"))
            # Staying adjacent is an internal hold; never invent a wait action.
            continue
        materials = reserves.get(actor.id, Counter())
        ore_count = sum(max(0, actor.inventory[k]-materials[k]) for k in MINERALS)
        bag_full = actor.capacity is not None and actor.backpack is not None and len(actor.backpack) >= actor.capacity
        if ore_count:
            length, steps = route(world, actor, world.zones.get("vendor", ()), deadline)
            if length:
                carried_value = sum(max(0, actor.inventory[k]-materials[k])*world.vendor.get(k, 0) for k in MINERALS)
                value = carried_value/(length+1) * (1.2 if ore_count >= policy.sell_batch or bag_full else 0.25)
                result.extend(movement(actor, steps, value, "sell route valued by current prices and carried quantity",
                                        route_goal={"purpose":"sell", "zone":"vendor", "targets":tuple(sorted(world.zones.get("vendor", ())))}))
        if not bag_full and not (ore_count >= policy.sell_batch and world.zones.get("vendor")):
            for mineral in sorted(MINERALS):
                for pos in sorted(world.zones.get(mineral, ())):
                    if time.monotonic() >= deadline:
                        break
                    length, steps = route(world, actor, [pos], deadline)
                    if length is None:
                        continue
                    # Estimate selling distance over traversable interaction cells,
                    # never claim to know public mine depletion or future price.
                    vendor_goals = interaction_cells(world, world.zones.get("vendor", ()), actor.pos)
                    sell_field = distance_field(world, vendor_goals, actor.pos, deadline)
                    ore_stands = interaction_cells(world, [pos], actor.pos)
                    sell_length = min((sell_field[p] for p in ore_stands if p in sell_field), default=None)
                    # Construction consumes ore directly. A missing/unreachable
                    # vendor blocks monetization, not a verified material route.
                    needed = actor.inventory[mineral] < materials[mineral]
                    utility = 8/(length+1) if needed else 0
                    if sell_length is not None:
                        horizon = length + policy.sell_batch + sell_length + 1
                        utility += policy.sell_batch*world.vendor.get(mineral, 0)/horizon
                    if length and utility > 0:
                        result.extend(movement(actor, steps, utility, "gather verified building materials" if needed
                                               else "ore route includes harvest and vendor return",
                                               route_goal={"purpose":"collect", "zone":mineral, "targets":(pos,)}))
        if policy.weapon_portfolio_enabled:
            job = build_jobs.get(actor.id)
            if job:
                result.extend(construction(world, clock, rules, actor, deadline, names={job['name']}, target_cell=job['target'], reserve_gold=construction_cash_reserve(world, policy)))
        else:
            result.extend(construction(world, clock, rules, actor, deadline, reserve_gold=construction_cash_reserve(world, policy)))
    return result
