"""Bounded eight-neighbour routing. Current occupied cells remain hard obstacles."""
from collections import deque
import time

from .protocol import distance


class DistanceField(dict):
    """Missing cells are unreachable only when the flood fill completed."""
    complete = True


def neighbours(pos):
    x, y = pos
    return [(x+dx, y+dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if dx or dy]


def interaction_cells(world, targets, actor_pos=None, extra_blocked=()):
    blocked = world.occupied | set(extra_blocked) | world.navigation_avoided.get(actor_pos, set())
    if actor_pos is not None:
        blocked = blocked - {actor_pos}
    return {p for target in targets for p in neighbours(target) + [target]
            if world.inside(p) and p not in blocked}


def distance_field(world, goals, actor_pos, deadline=float("inf"), extra_blocked=()):
    blocked = (world.occupied | set(extra_blocked) | world.navigation_avoided.get(actor_pos, set())) - {actor_pos}
    distances = DistanceField({p: 0 for p in sorted(goals) if world.inside(p) and p not in blocked})
    queue = deque(distances)
    count = 0
    while queue:
        count += 1
        if count % 64 == 0 and time.monotonic() >= deadline:
            distances.complete=False
            break
        current = queue.popleft()
        for p in neighbours(current):
            if world.inside(p) and p not in blocked and p not in distances:
                distances[p] = distances[current] + 1
                queue.append(p)
    return distances


def route(world, actor, targets, deadline=float("inf"), extra_blocked=()):
    goals = interaction_cells(world, targets, actor.pos, extra_blocked)
    field = distance_field(world, goals, actor.pos, deadline, extra_blocked)
    length = field.get(actor.pos)
    if length is None or length == 0:
        return length, []
    # Include alternatives so arbitration can resolve competing destinations.
    steps = [p for p in neighbours(actor.pos) if p in field and field[p] < length]
    return length, sorted(steps, key=lambda p: (field[p], p))


def axis_ray(start, target):
    """Only straight axes/45-degree diagonals have unambiguous cell centres.

    This does not assert the official arbitrary-angle rasterizer or blockers.
    Callers exclude lines containing non-robot objects before predicting damage.
    """
    dx, dy = target[0] - start[0], target[1] - start[1]
    if (dx == 0 and dy == 0) or (dx and dy and abs(dx) != abs(dy)):
        return None
    sx, sy = (dx > 0) - (dx < 0), (dy > 0) - (dy < 0)
    return [(start[0]+i*sx, start[1]+i*sy) for i in range(1, distance(start, target)+1)]
