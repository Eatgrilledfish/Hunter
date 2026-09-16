"""Staged construction and spending policy; legal build masks stay unchanged."""
from collections import Counter

from .rules import station_rings


def critical_bases(world, rules):
    """35% remaining HP is an urgency policy, not an additional game rule."""
    return frozenset(u.id for u in world.stations
                     if u.level in (1, 2, 3) and u.health is not None
                     and (maximum := rules.max_health.get('station', {}).get(u.level, 0)) > 0
                     and u.health <= maximum*.35)


def upgrade_rank(world, unit):
    critical = getattr(world, 'critical_base_ids', ())
    if unit.kind == 'station' and unit.id in critical:
        return 0
    if (critical and any(u.id in critical and u.level == 3 for u in world.stations)
            and unit.kind == 'wall' and unit.pos in upgrade_targets(world)):
        return .5
    if unit.kind=='wall':
        loss = getattr(world,'observed_wall_losses',{}).get(unit.id,0)
        if loss>0 and unit.health is not None and unit.health<=loss*2:return 1.5
        return 2+(unit.level-1)*.1
    return 1 if unit.kind in {'rocket', 'gatling', 'railgun'} else 3


def planned_gate(world):
    plan = getattr(world, 'task_side_plan', None)
    return plan['gate'] if plan else None


def upgrade_targets(world):
    """One permanent target set for purchases, held deliveries and completion."""
    return frozenset(getattr(world, 'monster_front_walls', ())) - {planned_gate(world)}


def monster_direction(world, anchor):
    # User-confirmed spawn side: upper-left base faces east; lower-right west.
    # This defines a side, not an invented spawn coordinate or enemy-base ray.
    centre_x = anchor[0]+.5
    return 1 if centre_x < (world.width-1)/2 else -1


def monster_face(world, anchor):
    _, yellow = station_rings(anchor)
    x = anchor[0]+3 if monster_direction(world, anchor) > 0 else anchor[0]-2
    return frozenset(p for p in yellow if p[0] == x)


def front_cells(world, anchor):
    _, yellow = station_rings(anchor)
    centre = (anchor[0]+.5, anchor[1]-.5)
    dx, dy = monster_direction(world, anchor), 0
    return frozenset(sorted(yellow, key=lambda p: (
        -((p[0]-centre[0])*dx+(p[1]-centre[1])*dy),
        (p[0]-centre[0])*dy-(p[1]-centre[1])*dx))[:10])


def prepare(world, rules, policy):
    world.critical_base_ids = critical_bases(world, rules)
    from .recovery import restoration_needed
    weapon_demand = any(u.level in (1, 2) for u in world.weapons)
    world.base_restore_ids = frozenset(u.id for u in world.stations
        if policy and policy.base_recovery_enabled and restoration_needed(
            u, rules.max_health.get('station', {}).get(u.level, 0), weapon_demand))
    world.staged_walls = False
    world.front_walls = frozenset()
    world.monster_front_walls = frozenset()
    world.permanent_front_walls = frozenset()
    world.wall_stage = None
    if (not policy or not policy.staged_walls_enabled or world.firing_ports or len(world.stations) != 1
            or getattr(world, "strategy_day", None) is None):
        return
    base = world.stations[0]
    _, yellow = station_rings(base.pos)
    rule = rules.build_rule(world, 'wall')
    if not rule or rule.cells != yellow or len(yellow) != 20:
        return
    front = front_cells(world, base.pos)
    if not front:
        return
    # The forward half of the perimeter, exactly ten cells. Two full faces
    # share a corner and would contain eleven. The perpendicular tie breaker
    # preserves the 180-degree mirror between the two camps.
    world.front_walls = front
    world.monster_front_walls = monster_face(world, base.pos)
    world.permanent_front_walls = upgrade_targets(world)
    world.staged_walls = True
    day = getattr(world, 'strategy_day', None)
    # Keep an already expanded ring usable when resuming an older match.
    first_stage = day == 1 and rules.wall_count(world) <= 10
    world.wall_stage = 'front10' if first_stage else 'morning19' if day == 2 else 'replace'
    world.wall_direction_source = 'monster_east' if monster_direction(world, base.pos) > 0 else 'monster_west'
    world.wall_targets = world.front_walls if first_stage else yellow


def priority_units(world):
    """Observed upgrade tier, before matching held vouchers or shop prices."""
    emergency = [u for u in world.stations if u.id in getattr(world, 'critical_base_ids', ()) and u.level in (1, 2)]
    if emergency:
        return emergency
    walls = [u for u in world.ours.values() if u.alive and u.kind == 'wall'
             and u.pos in upgrade_targets(world) and u.level != 3]
    if any(u.level == 3 and u.id in getattr(world, 'critical_base_ids', ()) for u in world.stations):
        return walls
    weapons = [u for u in world.weapons if u.level != 3]
    if weapons:
        if any(u.level not in (1, 2) for u in weapons):
            return weapons  # Unknown tiers cannot establish a fully funded upgrade budget.
        restoration = [u for u in world.stations if u.id in getattr(world, 'base_restore_ids', ())]
        held = sum((u.inventory for u in world.movers if u.backpack is not None), Counter())
        needed = Counter(f'WeaponUpgradeVoucher{level}' for u in weapons for level in range(u.level,3)) - held
        if (restoration and world.gold is not None
                and (not getattr(world,'task_side_plan',None) or len(world.weapons)>=3)
                and all(world.shop.get(name,0)>0 for name in needed)):
            reserve = sum(world.shop[name]*count for name,count in needed.items())
            cost = sum(world.shop.get(f'StationUpgradeVoucher{u.level}',float('inf'))
                       for u in restoration if not held[f'StationUpgradeVoucher{u.level}'])
            if reserve+cost<=world.gold:
                return weapons+restoration  # Jointly funded, still weapon-first in the use tour.
        return weapons
    if getattr(world, 'task_side_plan', None) and len(world.weapons) < 3:
        return []  # Rebuild a missing planned rocket before funding later tiers.
    restoration = [u for u in world.stations if u.id in getattr(world, 'base_restore_ids', ())]
    if restoration:
        return restoration  # Actual injury must not wait for every wall to max out.
    permanent = upgrade_targets(world)
    front = walls
    if front:
        return front
    observed = {u.pos for u in world.ours.values() if u.alive and u.kind == 'wall'}
    if not permanent <= observed:
        return []  # A destroyed/unbuilt front wall has not completed its upgrade tier.
    return [u for u in world.stations if u.level != 3]


def investment_fund(world):
    """Observed next-priority quote, without spending hypothetical sale income."""
    if not getattr(getattr(world, 'strategy_policy', None), 'upgrade_commitment_enabled', True):
        return 0, None
    # Held vouchers already fund their own tiers. Reserving their price again
    # can reject the basket's last stock item and strand the carrier at checkout.
    held = Counter()
    for actor in world.movers:
        if actor.backpack is not None:
            held.update(actor.inventory)
    quotes = []
    units = list(priority_units(world))
    # Funding continues into the wall stage when the weapon stage is prepaid.
    # A destroyed position still owes its reconstruction upgrade; it must not
    # become optional cash merely because there is no current building ID.
    if not getattr(world,'critical_base_ids',()):
        ids = {u.id for u in units}
        units += [u for u in world.ours.values() if u.alive and u.kind=='wall'
                  and u.pos in upgrade_targets(world) and u.level in (1,2) and u.id not in ids]
    from .wall_pressure import priority
    for unit in sorted(units, key=lambda u: (upgrade_rank(world,u),u.level,priority(world,u),u.id)):
        prefix = 'Weapon' if unit.kind in {'rocket', 'gatling', 'railgun'} else 'Wall' if unit.kind == 'wall' else 'Station'
        for level in range(unit.level, 3):
            name = f'{prefix}UpgradeVoucher{level}'
            if held[name]:
                held[name] -= 1
                continue
            price = world.shop.get(name)
            if price is None or price <= 0:
                return max(0, world.gold or 0), None
            quotes.append((upgrade_rank(world,unit),level,price,name))
    observed = {u.pos for u in world.ours.values() if u.alive and u.kind=='wall'}
    if not getattr(world,'critical_base_ids',()):
        for point in sorted(upgrade_targets(world)-observed):
            for level in (1,2):
                name = f'WallUpgradeVoucher{level}'
                if held[name]:
                    held[name] -= 1
                    continue
                price = world.shop.get(name)
                if price is None or price<=0:return max(0,world.gold or 0),None
                quotes.append((2,level,price,name))
    if not quotes:
        return 0, None
    _, _, price, name = min(quotes)
    return price, name


def pressure_ready(world):
    """An observed defence milestone, independent of unspent voucher prices."""
    if not getattr(world,'staged_walls',False):return True
    if len(world.weapons)!=3 or any(g.level!=3 for g in world.weapons):return False
    walls = {u.pos:u for u in world.ours.values() if u.alive and u.kind=='wall'}
    if any(p not in walls or walls[p].level!=3 for p in upgrade_targets(world)):return False
    missing = set(getattr(world,'wall_targets',()) or ())-walls.keys()
    gate = planned_gate(world)
    roster = getattr(world,'night_roster',None)
    worker = world.ours.get(roster.w) if roster else None
    if missing:
        from .day_access import gate as access_gate
        opening=access_gate(world)
        clock=getattr(world,'strategy_clock',None)
        if (missing!={opening} or opening in upgrade_targets(world) or not clock
                or clock.phases!={'day'} or world.phase_task or not worker
                or worker.backpack is None or worker.inventory['stone']<1):return False
        # A deliberate daytime doorway is not an unfunded breach. Verify
        # personal stone and both return walks before releasing optional cash.
        from .navigation import distance_field, neighbours
        from .rules import station_rings
        from copy import copy
        import time
        deadline=time.monotonic()+.01
        view=copy(world);view.occupied=world.occupied-{u.pos for u in world.movers}
        blue,_=station_rings(world.task_side_plan['anchor'])
        entries=(set(neighbours(opening))&blue)-{world.task_side_plan['w']}
        wwalk=distance_field(view,entries,worker.pos,deadline).get(worker.pos)
        pioneer=world.ours.get(roster.p)
        pwalk=(distance_field(view,{world.task_side_plan['w']},pioneer.pos,deadline).get(pioneer.pos)
               if pioneer and pioneer.alive else None)
        if (time.monotonic()>=deadline or wwalk is None or pwalk is None
                or wwalk+pwalk+2+world.strategy_policy.return_buffer>clock.until_night):return False
    if world.shop.get('WallFixer',0)>0 and (not worker or worker.backpack is None or not worker.inventory['WallFixer']):
        return False
    return True


def purchase_units(world):
    """Allow next-stage checkout once weapon purchases are fully funded."""
    current = priority_units(world)
    if (getattr(world,'critical_base_ids',()) or
            any(u.id in getattr(world,'base_restore_ids',()) for u in current)):
        return current
    held = Counter()
    for actor in world.movers:
        if actor.backpack is not None:held.update(actor.inventory)
    needed = Counter(f'WeaponUpgradeVoucher{level}' for g in world.weapons for level in range(g.level,3))
    if len(world.weapons)<3 or any(held[n]<count for n,count in needed.items()):return current
    from .wall_service import pending_targets
    front = [u for u in world.ours.values() if u.alive and u.kind=='wall'
             and u.pos in upgrade_targets(world) and u.level in (1,2)]
    front += pending_targets(world)
    return front or current


def purchase_permitted(world, rules, candidate):
    """One investment gate for all day/night and personal supply producers."""
    command=candidate.command
    if (command.get('action') != 'buy' or not getattr(world, 'staged_walls', False)
            or not getattr(getattr(world, 'strategy_policy', None), 'upgrade_commitment_enabled', True)):
        return True
    name=command.get('name','');actor=world.ours.get(candidate.actor)
    if not actor:return False
    if 'UpgradeVoucher' in name:
        prefix=name.split('UpgradeVoucher')[0]
        allowed={'Weapon' if u.kind in {'rocket','gatling','railgun'} else 'Wall' if u.kind=='wall' else 'Station'
                 for u in purchase_units(world)}
        return prefix in allowed
    # Genuine treatment can interrupt investment; healthy stock cannot.
    if name=='Medicine':
        from .medical import needs_treatment
        clock=getattr(world,'strategy_clock',None)
        if actor.health <= (200 if actor.kind=='pioneer' else 220)*.5 or clock and needs_treatment(world,actor,clock):
            return True
    if name=='WallFixer' and getattr(world,'critical_base_ids',()):return True
    if name.endswith('SummonOrder') and not pressure_ready(world):return False
    reserve,item=investment_fund(world)
    candidate.gold_reserve=max(candidate.gold_reserve,reserve)
    candidate.gold_reserve_item=item
    price=world.shop.get(name)
    return price is not None and (world.gold or 0)-price*command.get('num',1)>=reserve
