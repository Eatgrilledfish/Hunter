"""Clear a gate outward only when observed traffic and return time permit it."""
import time
from copy import copy

from .navigation import distance_field, neighbours
from .rules import station_rings


def outward_clearance(world, clock, blocker, path, policy, deadline):
    if (clock.phases != {'day'} or not world.stations or not world.defence_cells
            or blocker.pos not in path or world.seal_cells
            or any(r.alive for r in world.robots.values())
            or any('UpgradeVoucher' in name and count > 0 for name, count in blocker.inventory.items())
            or time.monotonic() >= deadline):
        return None
    blue, yellow = station_rings(world.stations[0].pos)
    outside = {p for q in yellow for p in neighbours(q)
               if world.inside(p) and p not in blue | yellow and p not in path and p not in world.occupied}
    real = distance_field(world, [blocker.pos], blocker.pos, deadline)
    home = distance_field(world, world.defence_cells, blocker.pos, deadline)
    # The relaxed incoming path gives an earliest arrival, not permission to
    # move through a currently occupied role or to assume a successful swap.
    arrival = path.index(blocker.pos) + 1
    relaxed = copy(world)
    relaxed.occupied = world.occupied-{u.pos for u in world.movers}
    arrivals = distance_field(relaxed, [blocker.pos], blocker.pos, deadline)
    # A later iteration for a distant role must not override a closer role's
    # right of way through the same gate.
    arrival = min(arrival, min((arrivals[u.pos] for u in world.movers
                  if u.id != blocker.id and u.pos not in world.defence_cells and u.pos in arrivals),
                  default=arrival))
    choices = []
    for point in outside & real.keys() & home.keys():
        length = real[point]
        if (0 < length and length + 1 < arrival
                and length + home[point] + policy.return_buffer + 2 <= clock.until_night):
            choices.append((length, home[point], point))
    if time.monotonic() >= deadline:
        return None
    return min(choices)[2] if choices else None
