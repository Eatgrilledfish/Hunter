"""Bounded, explicit construction heuristic; not a robot spawn prediction.

Compare four-round sustained centre-hit potential on a near-base grid, capped
at one small robot's 40 HP per cell. Optional saturation repair restores
diminishing marginal utility only when existing guns cover the whole grid at
that ceiling. No splash, future upgrades, task rewards,
or unconfirmed grazing hits are invented. Actual attacks still use combat.py.
"""
import time
from math import log1p

from .planning_rays import corridor
from .protocol import distance


class WeaponPortfolio:
    def __init__(self, world, rules, deadline, *, repair_saturation=False):
        self.world, self.rules, self.deadline = world, rules, deadline
        if world.width * world.height > 1312:
            raise TimeoutError("unsupported construction search size")
        base = world.stations[0].pos
        footprint = (base, (base[0]+1, base[1]), (base[0], base[1]-1), (base[0]+1, base[1]-1))
        self.grid = []
        for x in range(world.width):
            for y in range(world.height):
                d = min(distance((x,y), p) for p in footprint)
                if 1 < d <= 10 and (x,y) not in world.occupied:
                    self.grid.append(((x,y), 1/(d+1)))
        # Mobile roles can choose future operating stands; fixed buildings and
        # neutral objects remain structural obstacles. This is not actual LOS.
        self.blocked = world.occupied - {u.pos for u in world.movers} - {r.pos for r in world.robots.values()}
        self.existing = [(u.kind,u.level,u.pos) for u in world.weapons[:3]]
        self.cache = {}
        self.repair_saturation = repair_saturation
        self._saturated = None

    def profile(self, gun, extra):
        name,level,pos = gun
        key = (gun, frozenset(extra))
        if key in self.cache:
            return self.cache[key]
        if time.monotonic() >= self.deadline:
            raise TimeoutError("construction portfolio budget")
        stats = self.rules.building_stats.get(name, {}).get(level)
        values = [0.0]*len(self.grid)
        if stats is None or stats.attack_range is None or stats.listed_power is None:
            return values
        radius = max(self.world.width,self.world.height) if stats.attack_range == 'map' else stats.attack_range
        if name == 'rocket':
            if stats.cooldown is None or stats.projectiles is None:
                return values
            power = stats.listed_power * stats.projectiles * 4/(stats.cooldown+1)
        elif name == 'railgun':
            # The reference table is not sufficient to invent API total energy.
            power = self.rules.rail_energy.get(level,0)*4
        elif name == 'gatling':
            power = stats.listed_power*(stats.projectiles or 0)*4
        else:
            return values
        blocked = self.blocked | set(extra)
        for i,(target,_) in enumerate(self.grid):
            if distance(pos,target)>radius or target in blocked:
                continue
            if name != 'rocket':
                if corridor(pos,target,blocked) is None:
                    continue
            values[i] = power
        self.cache[key] = values
        return values

    def existing_saturated(self):
        # Select one scoring scale for the entire construction plan. Planned
        # guns must not change modes midway through a marginal comparison.
        if self._saturated is None:
            totals = [0.0]*len(self.grid)
            for gun in self.existing:
                for i, value in enumerate(self.profile(gun, set())):
                    totals[i] += value
            self._saturated = bool(totals) and all(value >= 40 for value in totals)
        return self._saturated

    def utility(self, guns):
        extra = {pos for _,_,pos in guns}
        totals = [0.0]*len(self.grid)
        for gun in self.existing + guns:
            for i,value in enumerate(self.profile(gun,extra)):
                totals[i] += value
        if self.repair_saturation and self.existing_saturated():
            return sum(40*log1p(value/40)*weight for value,(_,weight) in zip(totals,self.grid))
        return sum(min(40,value)*weight for value,(_,weight) in zip(totals,self.grid))

    def marginal(self, name, target, planned):
        # Include loss of existing straight lanes caused by the new building.
        return self.utility(planned+[(name,1,target)]) - self.utility(planned)
