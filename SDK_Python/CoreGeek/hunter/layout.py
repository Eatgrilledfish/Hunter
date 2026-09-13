"""Bounded structural connectivity checks for a whole proposed build bundle.

This is a layout policy, not an additional official action legality rule. Mobile
units are ignored only in this persistent-topology analysis; actual moves still
use the validator's current occupied-cell checks. No removal is assumed to open
a route within the same turn.
"""
from collections import deque
import time

from .protocol import MOBILE, MINERALS, WEAPONS, position
from .navigation import neighbours
from . import battery


class LayoutGuard:
    def __init__(self, world, deadline, max_checks=256):
        self.world, self.deadline, self.max_checks = world, deadline, max_checks
        self.cache, self.before, self.static = {}, None, None
        self.sources = {u.id: u.pos for u in world.movers}
        self.facilities = []
        self.internal_facilities = []

    def components(self, blocked):
        world = self.world
        labels, count, component = {}, 0, 0
        for x in range(world.width):
            for y in range(world.height):
                start = x, y
                if start in labels or start in blocked:
                    continue
                component += 1
                labels[start] = component
                queue = deque([start])
                while queue:
                    count += 1
                    if count % 64 == 0 and time.monotonic() >= self.deadline:
                        return None
                    point = queue.popleft()
                    for p in neighbours(point):
                        if world.inside(p) and p not in labels and p not in blocked:
                            labels[p] = component
                            queue.append(p)
        return labels

    def prepare(self):
        world = self.world
        entities = [*world.ours.values(), *world.enemies.values()]
        dynamic = {p for u in entities if u.kind in MOBILE for p in u.cells}
        dynamic.update(p for u in world.robots.values() for p in u.cells)
        self.static = world.occupied-dynamic
        # Preserve a known static object even in an overlapping/partial snapshot.
        self.static.update(p for cells in world.zones.values() for p in cells)
        # The adapter releases known destroyed buildings, including removed
        # walls whose health=0 records remain in the next snapshot. Do not
        # resurrect those obstacles while restoring overlapping static objects.
        # Unknown/missing health still blocks through the shared Unit predicate.
        self.static.update(p for u in entities if u.kind not in MOBILE and u.blocks for p in u.cells)
        for kind, cells in world.zones.items():
            if kind.startswith(world.side+"TaskPoint"):
                # A two-cell task point remains usable from either cell.
                self.facilities.append({p for cell in cells for p in neighbours(cell)})
            elif kind in MINERALS | {"vendor", "weaponShop"}:
                for p in sorted(cells):
                    self.facilities.append(set(neighbours(p)))
        for unit in world.ours.values():
            if unit.alive and unit.kind in WEAPONS | {"station"}:
                goals = set(neighbours(unit.pos))
                self.facilities.append(goals)
                self.internal_facilities.append(goals)
        self.before = self.components(self.static)
        return self.before is not None

    def check(self, candidates):
        builds = tuple(sorted((c.actor, c.command["name"], position(c.command["targetPos"][0]))
                              for c in candidates if c.command["action"] == "build"))
        if not builds:
            return True, "no new structural blockage"
        if any(p in getattr(self.world,'return_recovery_cells',()) for _,_,p in builds):
            return False, 'build would close an active defender recovery route'
        if self.world.battery_plan is not None:
            for _,kind,point in builds:
                if point not in battery.construction_cells(self.world,kind,{point}):
                    return False, "build conflicts with reserved battery site or firing port"
        if builds in self.cache:
            return self.cache[builds]
        if time.monotonic() >= self.deadline or len(self.cache) >= self.max_checks:
            return False, "layout verification budget exhausted"
        if self.before is None and not self.prepare():
            return False, "layout baseline incomplete within budget"
        added = {p for _, _, p in builds}
        after = self.components(self.static | added)
        if after is None:
            return False, "layout result incomplete within budget"

        def finish(allowed, reason):
            self.cache[builds] = allowed, reason
            return allowed, reason

        # Access to *a* gun neighbour is insufficient for the two-gun worker:
        # its exact shared stand must remain reachable after the whole bundle.
        plan=getattr(self.world,'task_side_plan',None)
        roster=getattr(self.world,'night_roster',None)
        clock=getattr(self.world,'strategy_clock',None)
        if plan and roster and any(kind=='wall' for _,kind,_ in builds):
            from .defence_duties import stands
            for identity,goals in ((i,stands(self.world,i)) for i in (roster.w,roster.p)):
                actor=self.world.ours.get(identity)
                if not actor or not actor.alive:continue
                reachable={q for q in goals if q in after and after.get(actor.pos)==after[q]}
                if not reachable:
                    return finish(False,'wall would block defender return to assigned gun stand')
                if clock and clock.phases=={'day'} and actor.pos not in goals:
                    # Reserve this build turn; do not rely on a simultaneous
                    # move or an unobserved arrival to justify closing a route.
                    limit=max(0,clock.until_night-1)
                    queue=deque([(actor.pos,0)]);seen={actor.pos};arrives=False
                    while queue:
                        if time.monotonic()>=self.deadline:
                            return finish(False,'defender return verification budget exhausted')
                        point,length=queue.popleft()
                        if point in reachable:arrives=True;break
                        if length>=limit:continue
                        for q in neighbours(point):
                            if q in after and q not in seen:
                                seen.add(q);queue.append((q,length+1))
                    if not arrives:
                        return finish(False,'wall leaves insufficient daylight for assigned gun return')

        walls = {u.pos for u in self.world.ours.values() if u.alive and u.kind == "wall"}
        seal = (bool(self.world.seal_cells) and all(kind == "wall" for _, kind, _ in builds)
                and added <= self.world.seal_cells and self.world.seal_cells <= walls | added)
        from .external_gate import valid_seal
        external = seal and valid_seal(self.world, builds)
        outside_id = self.world.external_gate_permit['m'] if external else None
        facilities = self.internal_facilities if seal else self.facilities
        for identity, source in self.sources.items():
            old, new = self.before.get(source), after.get(source)
            if old is None or new is None:
                return finish(False, "layout cannot establish mobile access")
            for other_id, other in self.sources.items():
                if self.before.get(other) == old and after.get(other) != new:
                    if external and ((identity==outside_id)!=(other_id==outside_id)):
                        continue
                    return finish(False, "build bundle separates previously connected teammates")
            required = ([g for g in self.facilities if g not in self.internal_facilities]
                        if external and identity==outside_id else facilities)
            for goals in required:
                if any(self.before.get(p) == old for p in goals) and not any(after.get(p) == new for p in goals):
                    return finish(False, "build bundle cuts access to a current facility")
            if any(self.before.get(p) == old for p in neighbours(source)) and not any(after.get(p) == new for p in neighbours(source)):
                return finish(False, "build bundle traps a mobile unit")
        for identity, kind, point in builds:
            if kind in WEAPONS:
                component = after.get(self.sources.get(identity))
                stands = [p for p in neighbours(point) if p in after and after[p] == component]
                if self.world.build_interior:
                    stands = [p for p in stands if p in self.world.build_interior]
                if len(stands) < 2:
                    return finish(False, "new weapon lacks two connected operator stands")
        if self.world.build_interior:
            guns = [u.pos for u in self.world.weapons] + [p for _,k,p in builds if k in WEAPONS]
            floor = self.world.build_interior - self.static - added
            if not floor:
                return finish(False, "no interior circulation remains")
            seen = {next(iter(floor))}
            queue = list(seen)
            for point in queue:
                for p in neighbours(point):
                    if p in floor and p not in seen:
                        seen.add(p)
                        queue.append(p)
            if seen != floor and any(k in WEAPONS for _,k,_ in builds):
                return finish(False, "completed walls would disconnect interior circulation")
            states = {0}
            for p in floor:
                mask = sum(1 << i for i,g in enumerate(guns) if max(abs(p[0]-g[0]),abs(p[1]-g[1])) <= 1)
                states |= {state | (1 << i) for state in list(states) for i in range(len(guns))
                           if mask & (1 << i) and not state & (1 << i)}
            if not any(s.bit_count() == len(guns) for s in states):
                return finish(False, "completed walls would leave guns without distinct interior operators")
        return finish(True, "night seal preserves team and gun access; reopen at dawn" if seal else "build bundle preserves current structural access")
