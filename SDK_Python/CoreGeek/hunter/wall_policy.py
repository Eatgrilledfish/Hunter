"""Staged construction and spending policy; legal build masks stay unchanged."""
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
    return 1 if unit.kind in {'rocket', 'gatling', 'railgun'} else 2 if unit.kind == 'wall' else 3


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
        return weapons
    if getattr(world, 'task_side_plan', None) and len(world.weapons) < 3:
        return []  # Rebuild a missing planned rocket before funding later tiers.
    permanent = upgrade_targets(world)
    front = walls
    if front:
        return front
    observed = {u.pos for u in world.ours.values() if u.alive and u.kind == 'wall'}
    if not permanent <= observed:
        return []  # A destroyed/unbuilt front wall has not completed its upgrade tier.
    return [u for u in world.stations if u.level != 3]
