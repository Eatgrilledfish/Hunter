"""Offensive camp policy; never discard robots from physical occupancy.

targetTeam is the team a robot attacks. A missing tag stays unknown; it is
not reclassified as either camp. Known opponent-camp robots are excluded.
"""
from .protocol import distance


def opposing(world, robot):
    return robot.target_team is not None and world.side is not None and robot.target_team != world.side


def cleanup(world):
    return bool(getattr(world, 'own_wave_cleared', False)
                and getattr(world, 'strategy_clock', None)
                and world.strategy_clock.phases == {'night'})


def protected(world, robot):
    return opposing(world, robot) and not cleanup(world)


def eligible(world, robot):
    return robot.alive and not protected(world, robot)


def cleanup_targets(world):
    return [r for r in world.robots.values() if eligible(world, r)
            and any(g.attack_range is not None and distance(g.pos,r.pos)<=g.attack_range
                    for g in world.weapons)] if cleanup(world) else []


def area_clear(world, centres):
    return not any(r.alive and protected(world, r) and any(distance(r.pos, p) <= 1 for p in centres)
                   for r in world.robots.values())


def protected_area(world):
    """Forbidden impact centres; compute once before bounded candidate search."""
    return {(r.pos[0]+dx, r.pos[1]+dy)
            for r in world.robots.values() if r.alive and protected(world, r)
            for dx in (-1, 0, 1) for dy in (-1, 0, 1)}


def line_clear(world, weapon, endpoint, rules, damage):
    if any(amount > 0 and protected(world, world.robots[i]) for i, amount in (damage or {}).items()):
        return False
    if weapon.kind == 'railgun' and weapon.level not in rules.rail_energy:
        # Unknown energy must not hide an opponent on the ray behind our target.
        dx, dy = endpoint[0]-weapon.pos[0], endpoint[1]-weapon.pos[1]
        length2 = dx*dx+dy*dy
        for robot in world.robots.values():
            rx, ry = robot.pos[0]-weapon.pos[0], robot.pos[1]-weapon.pos[1]
            if (robot.alive and protected(world, robot) and dx*ry == dy*rx
                    and 0 < rx*dx+ry*dy <= length2):
                return False
    return True
