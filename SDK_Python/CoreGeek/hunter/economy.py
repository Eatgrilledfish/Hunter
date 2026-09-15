"""Executable route economics, returning operators, and verified-rule construction."""
import time
from . import repair_decision
from collections import Counter

from .arbitration import Candidate
from .navigation import route, interaction_cells, distance_field, neighbours
from .protocol import MINERALS, WEAPONS, distance, pos_json
from . import procurement
from .layout import LayoutGuard
from .weapon_portfolio import WeaponPortfolio
from . import battery
from .wall_policy import planned_gate
from .night_roles import defender_ids, economic_endpoints


def movement(actor, steps, value, reason, *, route_goal=None):
    return [Candidate(actor.id, {"action": "move", "targetPos": [pos_json(p)]}, value-i*0.01, reason, route_goal=route_goal)
            for i, p in enumerate(steps[:4])]


def prepare_wall_cycle(world, clock, rules, policy=None):
    """Prepare construction; the external gate planner owns seal admission.

    Daytime access is restored by removing one owned wall; no refund assumed.
    """
    from .rules import station_rings
    world.seal_cells = frozenset()
    world.strategy_day = clock.day
    rule = rules.build_rule(world, "wall")
    if not rule or len(world.stations) != 1:
        return
    blue, yellow = station_rings(world.stations[0].pos)
    if rule.cells != yellow or len(yellow) != rules.wall_limit:
        return
    world.build_interior = blue
    battery.prepare(world, rules, policy)
    world.timed_economy = bool(policy and policy.day_schedule_enabled and len(world.weapons)==3
                              and len(battery.missing_walls(world,rules))<=1)
    if rules.wall_count(world) >= 10:
        world.defence_cells = blue
    # Sealing requires the gate planner's observed two-inside/one-outside
    # permission. The former all-three-inside seal is not a fallback strategy.
    builders = [u for u in world.movers if u.kind=='worker' and u.backpack is not None
                and all(u.inventory[k]>=n for k,n in rule.items.items())]
    # Reserve only imminent, actually funded work. Reserving the whole future
    # ring changes long-distance return budgets before any wall can be built.
    missing = set(battery.missing_walls(world,rules))
    access_gap=getattr(world,'active_access_gap',None)
    if access_gap is not None:missing.discard(access_gap)
    if (len(missing)==1 and getattr(world,"wall_stage",None)!="front10" and not world.seal_cells
            and (planned_gate(world) is None or planned_gate(world) in missing)):
        missing.clear()  # The deliberately open daytime gate is not ready for construction.
    if planned_gate(world) not in world.seal_cells and getattr(world, 'wall_stage', None) != 'front10':
        missing.discard(planned_gate(world))
    world.operator_excluded_cells = ({p for p in missing
                                      if any(distance(u.pos,p)<=1 for u in builders)}
        if policy and policy.construction_site_coordination_enabled and clock.phases == {'day'}
           and world.gold is not None and world.gold>=rule.gold else set())
    builder_ids={u.id for u in builders}
    for actor in world.movers:
        if actor.id not in builder_ids:
            # A role that just yielded must not immediately path back through
            # the imminent build cell and beat the build in joint arbitration.
            # This is a temporary planning reservation, not an observed wall.
            world.navigation_avoided.setdefault(actor.pos,set()).update(
                world.operator_excluded_cells-{actor.pos})


def open_day_gate(world, clock, rules, deadline):
    if clock.phases != {"day"} or len(world.stations) != 1:
        return []
    if (getattr(getattr(world,'strategy_policy',None),'pioneer_rotation_enabled',False)
            and (getattr(world, 'gate_worker_duty', None) or {}).get('round') == world.round):
        return []  # Ordered worker gate duties own opening; do not divert builders.
    from .rules import station_rings
    blue, yellow = station_rings(world.stations[0].pos)
    rule = rules.build_rule(world, "wall")
    walls = {u.pos for u in world.ours.values() if u.alive and u.kind == "wall"}
    if not rule or rule.cells != yellow:
        return []
    # Check each role's permanent topology, not just the presence of a hole.
    # Temporary role jams are handled by the director's outbound yielding.
    outside = {p for cell in yellow for p in neighbours(cell)
               if world.inside(p) and p not in blue and p not in yellow}
    from copy import copy
    topology = copy(world)
    topology.occupied = world.occupied-{u.pos for u in world.movers}
    trapped = [u for u in world.movers if u.pos in blue and
               u.pos not in distance_field(topology, outside, u.pos, deadline)]
    fixed_gate = planned_gate(world)
    if fixed_gate is not None and getattr(world, 'wall_stage', None) != 'front10':
        if clock.until_night <= 10 or fixed_gate not in walls:
            return []
        if any(u.alive and u.kind == 'wall' and u.pos == fixed_gate and u.level != 1
               for u in world.ours.values()):
            return []  # Preserve an unexpectedly upgraded/unknown-level old gate.
        gates = {fixed_gate}
        reason = "open planned task-side gate for daytime access; stone is not refunded"
    elif world.firing_ports:
        gates = walls & world.firing_ports
        reason = "clear reserved Gatling firing port; do not rebuild this wall"
        if not gates and clock.until_night > 10 and not battery.has_exit(world):
            gates = walls & battery.cells(world,'wall',yellow)
            reason = "open one owned wall for daytime access; stone is not refunded"
    else:
        if clock.until_night <= 10 or not trapped:
            return []
        gates = yellow & walls
        reason = "open one owned wall for daytime access; stone is not refunded"
    options = []
    for actor in world.movers:
        if actor.kind != "worker":
            continue
        for gate in sorted(gates):
            if trapped and not world.firing_ports:
                opened = copy(topology)
                opened.occupied = topology.occupied-{gate}
                if not any(u.pos in distance_field(opened, outside, u.pos, deadline) for u in trapped):
                    continue
            length, steps = route(world, actor, [gate], deadline)
            if length is not None:
                options.append((length, actor.id, gate, steps))
    if not options:
        return []
    length, identity, gate, steps = min(options)
    if length == 0:
        return [Candidate(identity, {"action":"remove", "targetPos":[pos_json(gate)]}, 40,
                          reason)]
    return movement(world.ours[identity], steps, 40, reason)


def construction_cash_reserve(world, policy):
    # Establish all three guns before saving for replenishment. A cash reserve with
    # no current shop/Medicine has no executable use and must not block builds.
    if (policy is None or len(world.weapons) < 3 or not world.zones.get("weaponShop")
            or world.shop.get("Medicine", 0) <= 0):
        return 0
    return policy.reserve_gold


def construction_jobs(world, rules, policy=None):
    if policy is not None:
        battery.prepare(world, rules, policy)
    if world.battery_plan is not None:
        # The requested composition and complete gun/port layout have already
        # been chosen. A portfolio timeout must not silently replace the mix.
        return _construction_jobs(world, rules, reserve_gold=construction_cash_reserve(world, policy), prefer_closed_ring=False)
    if policy is not None and policy.weapon_portfolio_enabled:
        try:
            return _construction_jobs(world, rules, portfolio=True, repair_saturation=policy.portfolio_saturation_enabled, reserve_gold=construction_cash_reserve(world, policy), prefer_closed_ring=policy.closed_ring_rockets_enabled)
        except TimeoutError:
            pass  # Discard the whole partial portfolio, retain complete baseline.
    return _construction_jobs(world, rules, reserve_gold=construction_cash_reserve(world, policy), prefer_closed_ring=policy is None or policy.closed_ring_rockets_enabled)


def _construction_jobs(world, rules, portfolio=False, repair_saturation=False, reserve_gold=0, prefer_closed_ring=True):
    """Assign prospective material jobs within current gold, cells and slots.

    Materials remain personal. This is recomputed from observations every turn;
    it is not a promise that another action cannot spend the same future gold.
    """
    if not world.stations or world.gold is None:
        return {}
    evaluator = WeaponPortfolio(world, rules, time.monotonic()+0.025, repair_saturation=repair_saturation) if portfolio else None
    planned = []
    layout = LayoutGuard(world, time.monotonic()+0.04)
    workers = {u.id: u for u in world.movers if u.kind == "worker"}
    available = {name: (rule, {p for p in battery.construction_cells(world,name,rule.cells) if world.inside(p) and p not in world.occupied})
                 for name in sorted(WEAPONS | {"wall"}) if (rule := rules.build_rule(world, name)) is not None}
    if prefer_closed_ring and world.build_interior and world.battery_plan is None:
        xs, ys = zip(*world.build_interior)
        corners = {(x,y) for x in (min(xs),max(xs)) for y in (min(ys),max(ys))}
        for name in WEAPONS:
            if name in available:
                available[name][1].intersection_update(corners)
    result, reserved_cells = {}, set()
    gold, slots = max(0, world.gold-reserve_gold), max(0, rules.weapon_limit-len(world.weapons))
    wall_slots = max(0, rules.wall_limit-rules.wall_count(world))
    # Optional walls must not tie up the entire workforce while cash is needed.
    earnable = bool(world.zones.get("vendor")) and any(world.vendor.get(k, 0)>0 and world.zones.get(k) for k in MINERALS)
    wall_workers = len(workers) if len(world.weapons) >= rules.weapon_limit else (max(0, len(workers)-1) if earnable else len(workers))
    team_wall_work = len(world.weapons) >= rules.weapon_limit
    # Before the battery is complete, keep one builder and one cash worker.
    wall_owner = min(workers, key=lambda i: (not bool(workers[i].inventory["stone"]), i)) if workers else None
    while workers:
        options = []
        for name, (rule, cells) in available.items():
            if rule.gold > gold or (name in WEAPONS and not slots) or (name == "wall" and (not wall_slots or wall_workers <= 0)):
                continue
            for identity, actor in workers.items():
                if name == "wall" and earnable and not team_wall_work and identity != wall_owner:
                    continue
                targets = cells-reserved_cells
                if not targets:
                    continue
                deficit = sum(max(0, n-actor.inventory[k]) for k, n in rule.items.items())
                ordered = sorted(targets, key=lambda p: (distance(actor.pos,p),p))
                for target in ordered[:4] if evaluator and name in WEAPONS else ordered[:1]:
                    gain = evaluator.marginal(name,target,planned) if evaluator and name in WEAPONS else 0
                    # Two short guns leave the rear unable to cover the base's
                    # opposite approach. Finish the battery with verified long
                    # range splash fire when all existing/planned guns are short.
                    short_battery = len(world.weapons)+len(planned) >= 2 and all(
                        (u.attack_range or 0) <= 3 for u in world.weapons) and all(n == "gatling" for n, _, _ in planned)
                    # A full yellow ring intersects outward bullet paths. Its
                    # projectile semantics are unknown; rockets are explicitly
                    # unblocked. Use that guaranteed capability for this layout,
                    # including the timeout fallback, rather than two silent guns.
                    closed_ring = prefer_closed_ring and bool(world.build_interior)
                    preference = int((closed_ring or short_battery) and name in WEAPONS and name != "rocket" and "rocket" in available)
                    if world.battery_plan is not None:
                        preference = int(name == 'rocket' and sum(u.kind=='gatling' for u in world.weapons)+sum(n=='gatling' for n,_,_ in planned)<2)
                    options.append((name == "wall", deficit, preference, distance(actor.pos,target)-gain/20, identity, name, target))
        if not options:
            break
        _, _, _, _, identity, name, target = min(options)
        preview = Candidate(identity, {"action":"build", "name":name, "targetPos":[pos_json(target)]}, 0, "layout preview")
        if not layout.check([preview])[0]:
            available[name][1].discard(target)
            continue
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
    # Keep a material/staging job for the final gate even while the layout
    # policy correctly refuses to close it before the pioneer returns.
    wall_rule = rules.build_rule(world, "wall")
    missing = battery.missing_walls(world,rules)
    enclosing = (getattr(world, "wall_stage", None) != "front10" and
                 (not world.firing_ports or bool(world.battery_plan and world.battery_plan['enclosure'])))
    if (len(missing)==1 and enclosing and world.build_interior and len(world.weapons)>=rules.weapon_limit
            and (planned_gate(world) is None or planned_gate(world) in missing)):
        target = next(iter(missing))
        free_workers = [u for u in world.movers if u.kind=="worker" and (u.id not in result or result[u.id]["name"]=="wall")]
        if free_workers:
            owner = min(free_workers,key=lambda u:(not bool(u.inventory["stone"]),sum(u.inventory[k]*world.vendor.get(k,0) for k in ("iron","copper")),u.id))
            for i in list(result):
                if result[i]["name"]=="wall":del result[i]
            result[owner.id] = {"name":"wall", "target":target, "items":Counter(wall_rule.items), "gate":True}
    # Reserve all remaining wall stone, divided between the assigned builders.
    # Allocate carried stock first (inventories cannot be transferred), then
    # balance outstanding collection rather than repeatedly fetching four stones.
    builders = sorted(i for i,j in result.items() if j["name"] == "wall")
    remaining = min(len(missing),max(0, rules.wall_limit-rules.wall_count(world)))
    quotas = {}
    for i in sorted(builders, key=lambda i: (-world.ours[i].inventory["stone"], i)):
        quotas[i] = min(remaining, world.ours[i].inventory["stone"])
        remaining -= quotas[i]
    for _ in range(remaining):
        if not builders:
            break
        i = min(builders, key=lambda i:(quotas[i],i))
        quotas[i] += 1
    for i in builders:
        if quotas[i] == 0:
            del result[i]  # Another worker already carries all needed stone.
        else:
            result[i]["stock_target"] = quotas[i]
    return result


def construction_reservations(world, rules, *, jobs=None):
    return {identity: Counter({**job["items"], **({"stone":job["stock_target"]} if job["name"]=="wall" and "stock_target" in job else {})})
            for identity, job in (construction_jobs(world, rules) if jobs is None else jobs).items()}


def ready_construction(world, clock, rules, policy, deadline, *, jobs=None):
    """Complete assigned buildings, including the wall material/return pipeline.

    The existing build policy chooses jobs. This prevents monetary bids from
    starving their finishing actions; it does not choose an optimal gun mix.
    """
    if not policy.construction_commitment_enabled or clock.phases != {"day"}:
        return []
    opening = open_day_gate(world, clock, rules, deadline)
    if opening:
        return opening
    result = []
    jobs = construction_jobs(world, rules, policy) if jobs is None else jobs
    ore_jobs = harvest_jobs(world, construction_reservations(world, rules, jobs=jobs))
    claimed = set()
    for identity, job in jobs.items():
        actor = world.ours[identity]
        if time.monotonic() >= deadline or actor.backpack is None:
            continue
        if job.get('helper'):
            from .rules import station_rings
            blue, yellow = station_rings(world.stations[0].pos)
            blocked = blue | yellow | world.stations[0].cells
            if actor.inventory['stone'] and not job.get('helper_collect', False):
                goals = set(job['helper_goals'])
                action = dict(action='build',name='wall',targetPos=[pos_json(job['target'])])
            else:
                mine = job.get('helper_mine')
                if mine is None:
                    continue
                goals = interaction_cells(world,[mine],actor.pos)-blocked
                action = dict(action='collect',targetPos=[pos_json(mine)])
            reach = distance_field(world,goals,actor.pos,deadline,extra_blocked=blocked)
            if actor.pos in goals:
                result.append(Candidate(identity,action,80,'exterior miner assists delayed wall construction'))
            elif actor.pos in reach:
                steps = sorted(p for p in neighbours(actor.pos) if reach.get(p,float('inf')) < reach[actor.pos])
                result.extend(movement(actor,steps,80,'exterior miner approaches its own wall work stand'))
            continue
        if job.get("economy_first"):
            # A collection/build commitment cannot spend the final return
            # window. The gate actor is already inside when it seals.
            return_goals=interaction_cells(world,[u.pos for u in world.weapons],actor.pos) & world.build_interior
            return_field=distance_field(world,return_goals,actor.pos,deadline)
            if not (job.get('gate') and world.seal_cells) and return_field.get(actor.pos,0)>0 and clock.until_night<=return_field[actor.pos]+2:
                job['return_only']=True
                continue
            deficit = max(0,job['stock_target']-actor.inventory['stone'])
            if deficit and (clock.until_night > deficit+5 or not actor.inventory['stone']):
                options=[]
                for mine in world.zones.get('stone',()):
                    if getattr(world, 'batch_mine_owners', {}).get(mine, identity) != identity:
                        continue
                    length,steps=route(world,actor,[mine],deadline)
                    back=min((return_field[p] for p in neighbours(mine) if p in return_field),default=None)
                    if length is not None and back is not None and length+1+back+2 < clock.until_night:
                        options.append((length,mine,steps))
                if options:
                    length,mine,steps=min(options)
                    if length==0:
                        result.append(Candidate(identity,{'action':'collect','targetPos':[pos_json(mine)]},40,
                                                'designated supplier: collect only remaining wall quota'))
                    else:
                        result.extend(movement(actor,steps,40,'designated supplier: nearest reachable stone for wall quota'))
                    continue
            if job.get('defer_build'):
                continue
        if job.get("gate") and actor.inventory["stone"] and not world.seal_cells:
            continue
        rule = rules.build_rule(world, job["name"])
        if rule is None:
            continue
        if job["name"] == "wall":
            mineral = next((k for k, n in rule.items.items() if actor.inventory[k] < n), None)
            # Fill the personal project quota while at the mine, then build.
            # This is a policy stock target, not a change to the one-stone cost.
            if not job.get('economy_first') and mineral is None and actor.inventory["stone"] < min(job.get("stock_target", 4), max(1, (clock.until_night-policy.return_buffer)//2)):
                if world.near_zone(actor.pos, "stone"):
                    mineral = "stone"
            if mineral is not None:
                build_goals = interaction_cells(world, [job["target"]], actor.pos, {job["target"]})
                home = distance_field(world, build_goals, actor.pos, deadline)
                options = []
                for mine in world.zones.get(mineral, ()):
                    if getattr(world, 'batch_mine_owners', {}).get(mine, identity) != identity:
                        continue
                    length, steps = route(world, actor, [mine], deadline)
                    back = min((home[p] for p in interaction_cells(world, [mine], actor.pos) if p in home), default=None)
                    if length is not None and back is not None and length+back+5+policy.return_buffer < clock.until_night:
                        options.append((mine in claimed, ore_jobs.get(identity) != (mineral, mine), length+back, mine, length, steps))
                if options:
                    _, _, _, mine, length, steps = min(options)
                    claimed.add(mine)
                    if length == 0:
                        result.append(Candidate(actor.id, {"action":"collect", "targetPos":[pos_json(mine)]}, 18,
                                                "wall builder: stock stone before returning to build"))
                    else:
                        result.extend(movement(actor, steps, 18, "wall builder: reserved stone route then construction"))
                    continue
        if any(actor.inventory[name] < amount for name, amount in rule.items.items()):
            continue
        result.extend(construction(world, clock, rules, actor, deadline, names={job["name"]}, target_cell=job["target"], finish_before_night=True, reserve_gold=construction_cash_reserve(world, policy)))
    return result


def construction_materials(world, rules, actor):
    return construction_reservations(world, rules).get(actor.id, Counter())


def harvest_jobs(world, reserves):
    """Allocate reachable mines once by stable worker order, with material priority.

    Sharing is allowed only when no distinct useful mine remains. Route length
    beats straight-line proximity; no remaining ore quantity is invented.
    """
    workers = sorted((u for u in world.movers if u.kind == "worker"), key=lambda u: (
        not any(u.inventory[k] < n for k, n in reserves.get(u.id, {}).items()), u.id))
    chosen, claimed = {}, set()
    deadline = time.monotonic()+0.04
    for actor in workers:
        field = distance_field(world, [actor.pos], actor.pos, deadline)
        options = []
        for mineral in sorted(MINERALS):
            need = actor.inventory[mineral] < reserves.get(actor.id, {}).get(mineral, 0)
            price = world.vendor.get(mineral, 0)
            if not need and price <= 0:
                continue
            for mine in sorted(world.zones.get(mineral, ())):
                if getattr(world, 'batch_mine_owners', {}).get(mine, actor.id) != actor.id:
                    continue
                length = min((field[p] for p in neighbours(mine) if p in field), default=None)
                if length is not None:
                    options.append((not need, (mineral, mine) in claimed, (length+6)/max(1, price), mineral, mine))
        if options:
            *_, mineral, mine = min(options)
            chosen[actor.id] = (mineral, mine)
            claimed.add((mineral, mine))
    return chosen


def immediate(world, rules, task_actor=None, *, jobs=None, policy=None, local_only=False):
    """Cheap, current-snapshot incumbent available before advanced planning."""
    result = []
    planned_repairs = policy and policy.repair_plan_enabled and getattr(world, "task_side_plan", None)
    if local_only:
        # Early error recovery must not depend on construction or path search.
        # Only assign mines that an observed worker can already interact with.
        reserves, ore_jobs, claimed = {}, {}, set()
        for actor in sorted((u for u in world.movers if u.kind == 'worker'), key=lambda u: u.id):
            adjacent = [(name, point) for name in sorted(MINERALS) if world.vendor.get(name, 0) > 0
                        for point in sorted(world.zones.get(name, ())) if distance(actor.pos, point) <= 1]
            if adjacent:
                mine = min(adjacent, key=lambda item: (item in claimed, -world.vendor[item[0]], item))
                ore_jobs[actor.id] = mine
                claimed.add(mine)
    else:
        reserves = construction_reservations(world, rules, jobs=jobs)
        ore_jobs = harvest_jobs(world, reserves)
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
                    if ore_jobs.get(actor.id) == (mineral, pos) and distance(actor.pos, pos) <= 1:
                        held = sum(max(0,actor.inventory[k]-materials[k]) for k in MINERALS)
                        if world.build_interior and not getattr(world,'timed_economy',False) and held >= 10 and actor.inventory[mineral] >= materials[mineral]:
                            continue  # One mine's stock is enough until it can be sold.
                        result.append(Candidate(actor.id, {"action": "collect", "targetPos": [pos_json(pos)]},
                                                world.vendor.get(mineral, 0)*0.4 + (8 if actor.inventory[mineral] < materials[mineral] else 0),
                                                "collect current adjacent ore, including verified construction need"))
        if local_only:
            continue  # Building investment/repair needs the prepared layout and duties.
        for building in world.ours.values():
            if not building.alive or distance(actor.pos, building.pos) > 1:
                continue
            prefix = "Weapon" if building.kind in WEAPONS else "Station" if building.kind == "station" else "Wall" if building.kind == "wall" else None
            if prefix and building.level in {1, 2}:
                name = f"{prefix}UpgradeVoucher{building.level}"
                if actor.inventory[name] and procurement.upgrade_allowed(world,building,policy,rules):
                    result.append(Candidate(actor.id, {"action": "use", "name": name, "targetPos": [pos_json(building.pos)]},
                                            30 if prefix != "Wall" else 12, "use carried level-matched voucher"))
            max_hp = rules.max_health.get(building.kind, {}).get(building.level)
            if not planned_repairs and actor.inventory["WallFixer"] and repair_decision.eligible(world, building, rules, policy):
                result.append(Candidate(actor.id, {"action": "use", "name": "WallFixer", "targetPos": [pos_json(building.pos)]},
                                        (max_hp-building.health)*0.05, "repair wall at configured verified HP threshold"))
    return result


def operator_goals(world, actor):
    """Prefer stands adjacent to multiple weapons, preserving future rotations."""
    weapons = world.weapons
    cells = interaction_cells(world, [u.pos for u in weapons], actor.pos)
    cells -= getattr(world,'operator_excluded_cells',set())
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
        for target in sorted(battery.construction_cells(world,name,rule.cells)):
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


def propose(world, clock, rules, policy, deadline, task_actor=None, operator_stands=None, *, upgrade_candidates=None, build_jobs=None, task_choice=None):
    result = (procurement.propose(world, policy, deadline, task_actor, rules=rules)
              if upgrade_candidates is None else list(upgrade_candidates))
    if build_jobs is None:
        build_jobs = construction_jobs(world, rules, policy)
    reserves = construction_reservations(world, rules, jobs=build_jobs)
    ore_jobs = harvest_jobs(world, reserves)
    for actor in world.movers:
        if actor.id == task_actor or time.monotonic() >= deadline:
            continue
        if actor.kind == "pioneer":
            if clock.phases != {'day'} or actor.id in getattr(world, 'pioneer_trade_ids', ()):
                continue
            if task_choice is not None and actor.id == task_choice['actor']:
                result.extend(task_choice['candidates'])
                continue
            # Navigation toward tasks is useful before task eligibility; accepting
            # and holding tasks belongs exclusively to the task engine.
            if not world.phase_task:
                for task in getattr(world, 'available_tasks', world.tasks):
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
        is_defender = actor.id in defender_ids(world)
        if not is_defender and clock.phases != {"day"}:
            # Night mining is admitted by the external economy state machine.
            # No fallback gun route or unguarded speculative mining.
            continue
        defence = ([operator_stands[actor.id]] if operator_stands and actor.id in operator_stands else operator_goals(world, actor)) if is_defender else economic_endpoints(world, actor)
        defence_field = distance_field(world, defence, actor.pos, deadline) if defence else {}
        return_length = defence_field.get(actor.pos)
        must_return = is_defender and return_length is not None and (clock.phases != {"day"} or clock.until_night <= return_length+policy.return_buffer)
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
            vendor_stands = interaction_cells(world, world.zones.get("vendor", ()), actor.pos)
            home_from_vendor = min((defence_field[p] for p in vendor_stands if p in defence_field), default=None)
            sale_actions = sum(actor.inventory[k] > materials[k] for k in MINERALS)
            sale_fits = (not world.build_interior or (home_from_vendor is not None and length is not None and
                length+sale_actions+home_from_vendor+policy.return_buffer+8 <= clock.until_night))
            if length and sale_fits:
                carried_value = sum(max(0, actor.inventory[k]-materials[k])*world.vendor.get(k, 0) for k in MINERALS)
                value = carried_value/(length+1) * (1.2 if ore_count >= min(10,policy.sell_batch) or bag_full else 0.25)
                if ore_count >= min(10,policy.sell_batch) or (home_from_vendor is not None and length+sale_actions+home_from_vendor+policy.return_buffer+12 >= clock.until_night):
                    value = max(value, 12)  # Realize a depleted mine's load before another detour.
                result.extend(movement(actor, steps, value, "sell route valued by current prices and carried quantity",
                                        route_goal={"purpose":"sell", "zone":"vendor", "targets":tuple(sorted(world.zones.get("vendor", ())))}))
        if not bag_full and not (ore_count >= policy.sell_batch and world.zones.get("vendor")):
            for mineral in sorted(MINERALS):
                for pos in sorted(world.zones.get(mineral, ())):
                    if ore_jobs.get(actor.id) != (mineral, pos):
                        continue
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
                        home = min((defence_field[p] for p in vendor_goals if p in defence_field), default=None)
                        batch = policy.sell_batch
                        if world.build_interior:
                            batch = min(batch, clock.until_night-length-sell_length-1-(home or 0)-policy.return_buffer-8)
                        horizon = length + max(1,batch) + sell_length + 1
                        if batch > 0 and (not world.build_interior or (home is not None and horizon+home+policy.return_buffer+8 <= clock.until_night)):
                            utility += batch*world.vendor.get(mineral, 0)/horizon
                        elif world.build_interior and home is not None and ore_count < 10:
                            # A nearby bounded stock trip can fit when the vendor
                            # detour cannot. Keep a verified future cash path and
                            # tomorrow's sell priority; never assume future prices.
                            mine_home = min((defence_field[p] for p in ore_stands if p in defence_field),default=None)
                            stock = min(10-ore_count,clock.until_night-length-(mine_home or 0)-policy.return_buffer-8)
                            if mine_home is not None and stock > 0:
                                utility += stock*world.vendor.get(mineral,0)/(length+stock+mine_home+sell_length+home+2)*0.5
                    if length and utility > 0:
                        result.extend(movement(actor, steps, utility, "gather verified building materials" if needed
                                               else "bounded ore route; cash trip when feasible, otherwise stock for next day",
                                               route_goal={"purpose":"collect", "zone":mineral, "targets":(pos,)}))
        if policy.weapon_portfolio_enabled:
            job = build_jobs.get(actor.id)
            if job and not job.get('defer_build') and not job.get('return_only'):
                result.extend(construction(world, clock, rules, actor, deadline, names={job['name']}, target_cell=job['target'], reserve_gold=construction_cash_reserve(world, policy)))
        else:
            job = build_jobs.get(actor.id)
            if job and not job.get('defer_build') and not job.get('return_only'):
                result.extend(construction(world, clock, rules, actor, deadline, names={job["name"]},
                                           target_cell=job["target"], reserve_gold=construction_cash_reserve(world, policy)))
    return result
