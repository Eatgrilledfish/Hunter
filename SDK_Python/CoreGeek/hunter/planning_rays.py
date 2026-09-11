"""Structural/future-placement corridors under the current combat ray policy.

Axes retain their existing lattice policy. Other directions include every
intersected square so a proposed operator stand cannot silently graze a ray.
This is a placement estimate, not a claim that a planned move has succeeded.
"""
from .navigation import axis_ray
from .rays import clear_centre_ray, segment_cells


def corridor(start, target, blocked, robots=(), *, stop_at_robot=False):
    if stop_at_robot:
        dx, dy = target[0]-start[0], target[1]-start[1]
        length = dx*dx+dy*dy
        hits = [p for p in robots if dx*(p[1]-start[1]) == dy*(p[0]-start[0])
                and 0 < (p[0]-start[0])*dx+(p[1]-start[1])*dy <= length]
        if hits:
            target = min(hits, key=lambda p:(p[0]-start[0])**2+(p[1]-start[1])**2)
    ray = axis_ray(start, target)
    if ray is not None:
        return tuple(ray) if not blocked.intersection(ray) else None
    ray = clear_centre_ray(start, target, blocked | set(robots))
    if ray is None or blocked.intersection(ray):
        return None
    return segment_cells(start, target)
