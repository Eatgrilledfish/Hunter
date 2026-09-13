"""Offensive camp policy; never discard robots from physical occupancy.

targetTeam is the team a robot attacks. A missing tag stays unknown; it is
not reclassified as either camp. Known opponent-camp robots are excluded.
"""
from .protocol import distance


def opposing(world, robot):
    return robot.target_team is not None and world.side is not None and robot.target_team != world.side


def area_clear(world, centres):
    return not any(r.alive and opposing(world, r) and any(distance(r.pos, p) <= 1 for p in centres)
                   for r in world.robots.values())


def protected_area(world):
    """Forbidden impact centres; compute once before bounded candidate search."""
    return {(r.pos[0]+dx, r.pos[1]+dy)
            for r in world.robots.values() if r.alive and opposing(world, r)
            for dx in (-1, 0, 1) for dy in (-1, 0, 1)}


def line_clear(world, weapon, endpoint, rules, damage):
    if any(amount > 0 and opposing(world, world.robots[i]) for i, amount in (damage or {}).items()):
        return False
    if weapon.kind == 'railgun' and weapon.level not in rules.rail_energy:
        # Unknown energy must not hide an opponent on the ray behind our target.
        dx, dy = endpoint[0]-weapon.pos[0], endpoint[1]-weapon.pos[1]
        length2 = dx*dx+dy*dy
        for robot in world.robots.values():
            rx, ry = robot.pos[0]-weapon.pos[0], robot.pos[1]-weapon.pos[1]
            if (robot.alive and opposing(world, robot) and dx*ry == dy*rx
                    and 0 < rx*dx+ry*dy <= length2):
                return False
    return True
