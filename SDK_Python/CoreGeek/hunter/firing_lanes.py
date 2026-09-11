"""Bounded future stand evaluation under the existing conservative ray policy.

This does not change attack legality or treat a planned move as already applied.
Clear centre corridors are modeled; ambiguous grazing remains blocked. Robot positions
are a static scenario for placement, never an asserted next-turn prediction.
"""
import time

from .planning_rays import corridor
from .protocol import distance


class FiringLanes:
    def __init__(self, world, clock, weapons, operators, deadline):
        self.guns = [0] * len(weapons)
        self.cells = {}
        if clock.phases != {"night"}:
            return
        robot_cells = {r.pos for r in world.robots.values() if r.alive}
        static = world.occupied - robot_cells - {u.pos for u in operators}
        paths = []
        for index, gun in enumerate(weapons):
            if gun.kind not in {"gatling", "railgun"} or gun.cooldown not in (0, None) or gun.attack_range is None:
                continue
            count = 0
            for robot in sorted(world.robots.values(), key=lambda r: (distance(gun.pos, r.pos), r.id)):
                if time.monotonic() >= deadline:
                    return  # No partly accumulated lane profile is published.
                if not robot.alive or distance(gun.pos, robot.pos) > gun.attack_range:
                    continue
                ray = corridor(gun.pos, robot.pos, static, robot_cells,
                               stop_at_robot=gun.kind == 'gatling')
                if ray is None:
                    continue
                paths.append((index, ray))
                count += 1
                if count == 8:
                    break
        for number, (index, ray) in enumerate(paths):
            bit = 1 << number
            self.guns[index] |= bit
            for point in ray:
                self.cells[point] = self.cells.get(point, 0) | bit

    def mask(self, point):
        return self.cells.get(point, 0)

    def available(self, points):
        blocked = 0
        for point in points:
            blocked |= self.mask(point)
        return sum(1 << index for index, lanes in enumerate(self.guns) if lanes & ~blocked)
