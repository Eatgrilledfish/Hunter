"""Static eight-turn staffing opportunities, not predicted damage or robot AI.

A role walks to a proposed stand and then stays. Walking consumes its turns;
guns share controllers and observed cooldowns. Replanning may choose a new
stand next turn. Targets/lanes are a frozen placement scenario, not guaranteed
future targets, and no hypothetical move changes actual attack legality.
"""
from functools import lru_cache
import time

from .firing_lanes import FiringLanes
from .protocol import distance
from .rules import Clock


class OperatorCycles:
    def __init__(self, world, clock, weapons, operators, deadline):
        self.deadline = deadline
        self.weapons = weapons
        self.operators = operators
        self.cache = {}
        self.horizon = 0
        if clock.phases != {'night'} or any(w.cooldown is None or w.attack_range is None for w in weapons):
            return
        for step in range(8):
            if Clock(world.round+step, clock.origin).phases != {'night'}:
                break
            self.horizon += 1
        self.lanes = FiringLanes(world, clock, weapons, operators, deadline)
        if time.monotonic() >= deadline:
            raise TimeoutError('incomplete staffing lane profile')
        self.rockets = sum(1 << i for i, gun in enumerate(weapons) if gun.kind == 'rocket'
                           and any(r.alive and distance(gun.pos, r.pos) <= gun.attack_range
                                   for r in world.robots.values()))

    def value(self, stands, fields):
        if not self.horizon:
            return None
        points = tuple(p if p is not None else u.pos for u, p in zip(self.operators, stands))
        available = self.rockets | self.lanes.available(points)
        masks = tuple(sum(1 << i for i, gun in enumerate(self.weapons)
                          if available & (1 << i) and distance(point, gun.pos) <= 1)
                      for point in points)
        arrivals = tuple(min(self.horizon, fields[u.id].get(p, self.horizon))
                         for u, p in zip(self.operators, points))
        key = masks, arrivals
        if key in self.cache:
            return self.cache[key]

        @lru_cache(None)
        def choices(access):
            result = {0}
            for mask in access:
                result |= {used | (1 << i) for used in tuple(result) for i in range(len(self.weapons))
                           if mask & (1 << i) and not used & (1 << i)}
            return tuple(sorted(result))

        @lru_cache(None)
        def search(step, cooldowns):
            if time.monotonic() >= self.deadline:
                raise TimeoutError('incomplete staffing cycle comparison')
            if step == self.horizon:
                return 0
            ready = sum(1 << i for i, n in enumerate(cooldowns) if n == 0)
            access = tuple(mask & ready if arrival <= step else 0 for mask, arrival in zip(masks, arrivals))
            best = 0
            for fired in choices(access):
                after = tuple(3 if fired & (1 << i) and gun.kind == 'rocket' else max(0, n-1)
                              for i, (gun, n) in enumerate(zip(self.weapons, cooldowns)))
                best = max(best, fired.bit_count()+search(step+1, after))
            return best

        result = search(0, tuple(min(self.horizon, w.cooldown) for w in self.weapons)) / self.horizon
        self.cache[key] = result
        return result
