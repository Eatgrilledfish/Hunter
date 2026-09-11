"""Centre-line geometry, not an assumed official cell rasterizer.

For new, non-axis directions, accept only corridors where every occupied
intersected square has its centre exactly on the segment. Thus off-centre
grazing/corner rules cannot change the predicted sequence of occupied cells.
Existing axis/45-degree behaviour remains in the calling adapter.
"""
from functools import lru_cache
from math import gcd


def primitive_step(start, target):
    dx, dy = target[0] - start[0], target[1] - start[1]
    steps = gcd(abs(dx), abs(dy))
    return (dx // steps, dy // steps) if steps else None


@lru_cache(maxsize=2048)
def segment_cells(start, target):
    """Exact closed unit-square intersections, excluding the origin square.

    Integer separating-axis tests: the segment's bounding box intersects a
    cell, and the cell straddles the segment's supporting line. Including
    corner-only contact is deliberately conservative, not a hit assertion.
    Runtime callers supply map-bounded coordinates, never attackRange extents.
    """
    dx, dy = target[0] - start[0], target[1] - start[1]
    if not (dx or dy):
        return ()
    tolerance = abs(dx) + abs(dy)
    return tuple((x, y)
                 for x in range(min(start[0], target[0]), max(start[0], target[0]) + 1)
                 for y in range(min(start[1], target[1]), max(start[1], target[1]) + 1)
                 if (x, y) != start
                 and 2 * abs(dx * (y - start[1]) - dy * (x - start[0])) <= tolerance)


def clear_centre_ray(start, target, occupied):
    """Return collinear lattice centres, or None for an ambiguous corridor."""
    step = primitive_step(start, target)
    if step is None:
        return None
    dx, dy = target[0] - start[0], target[1] - start[1]
    for x, y in segment_cells(start, target):
        if (x, y) in occupied and dx * (y - start[1]) != dy * (x - start[0]):
            return None
    count = gcd(abs(dx), abs(dy))
    return [(start[0] + i * step[0], start[1] + i * step[1])
            for i in range(1, count + 1)]
