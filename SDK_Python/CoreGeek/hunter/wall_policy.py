"""Staged construction and spending policy; legal build masks stay unchanged."""
from .rules import station_rings


def planned_gate(world):
    plan = getattr(world, 'task_side_plan', None)
    return plan['gate'] if plan else None


def upgrade_targets(world):
    """One permanent target set for purchases, held deliveries and completion."""
    return frozenset(getattr(world, 'front_walls', ())) - {planned_gate(world)}


def front_cells(world, anchor):
    _, yellow = station_rings(anchor)
    enemies = [u for u in world.enemies.values() if u.kind == 'station']
    target = enemies[0].pos if len(enemies) == 1 else ((world.width-1)/2, (world.height-1)/2)
    centre = (anchor[0]+.5, anchor[1]-.5)
    dx, dy = target[0]-centre[0], target[1]-centre[1]
    if dx == dy == 0:
        return frozenset()
    return frozenset(sorted(yellow, key=lambda p: (
        -((p[0]-centre[0])*dx+(p[1]-centre[1])*dy),
        (p[0]-centre[0])*dy-(p[1]-centre[1])*dx))[:10])


def prepare(world, rules, policy):
    world.staged_walls = False
    world.front_walls = frozenset()
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
    enemies = [u for u in world.enemies.values() if u.kind == 'station']
    front = front_cells(world, base.pos)
    if not front:
        return
    # The forward half of the perimeter, exactly ten cells. Two full faces
    # share a corner and would contain eleven. The perpendicular tie breaker
    # preserves the 180-degree mirror between the two camps.
    world.front_walls = front
    world.permanent_front_walls = upgrade_targets(world)
    world.staged_walls = True
    day = getattr(world, 'strategy_day', None)
    # Keep an already expanded ring usable when resuming an older match.
    first_stage = day == 1 and rules.wall_count(world) <= 10
    world.wall_stage = 'front10' if first_stage else 'morning19' if day == 2 else 'replace'
    world.wall_direction_source = 'enemy_base' if len(enemies) == 1 else 'map_centre'
    world.wall_targets = world.front_walls if first_stage else yellow


def priority_units(world):
    """Observed upgrade tier, before matching held vouchers or shop prices."""
    weapons = [u for u in world.weapons if u.level != 3]
    if weapons:
        return weapons
    if getattr(world, 'task_side_plan', None) and len(world.weapons) < 3:
        return []  # Rebuild a missing planned rocket before funding later tiers.
    permanent = upgrade_targets(world)
    front = [u for u in world.ours.values() if u.alive and u.kind == 'wall'
             and u.pos in permanent and u.level != 3]
    if front:
        return front
    observed = {u.pos for u in world.ours.values() if u.alive and u.kind == 'wall'}
    if not permanent <= observed:
        return []  # A destroyed/unbuilt front wall has not completed its upgrade tier.
    return [u for u in world.stations if u.level != 3]
