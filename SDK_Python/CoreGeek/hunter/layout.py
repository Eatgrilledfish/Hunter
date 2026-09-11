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


class LayoutGuard:
    def __init__(self, world, deadline, max_checks=256):
        self.world, self.deadline, self.max_checks = world, deadline, max_checks
        self.cache, self.before, self.static = {}, None, None
        self.sources = {u.id: u.pos for u in world.movers}
        self.facilities = []

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
        self.static.update(p for u in entities if u.kind not in MOBILE for p in u.cells)
        for kind, cells in world.zones.items():
            if kind.startswith(world.side+"TaskPoint"):
                # A two-cell task point remains usable from either cell.
                self.facilities.append({p for cell in cells for p in neighbours(cell)})
            elif kind in MINERALS | {"vendor", "weaponShop"}:
                for p in sorted(cells):
                    self.facilities.append(set(neighbours(p)))
        for unit in world.ours.values():
            if unit.alive and unit.kind in WEAPONS | {"station"}:
                self.facilities.append(set(neighbours(unit.pos)))
        self.before = self.components(self.static)
        return self.before is not None

    def check(self, candidates):
        builds = tuple(sorted((c.actor, c.command["name"], position(c.command["targetPos"][0]))
                              for c in candidates if c.command["action"] == "build"))
        if not builds:
            return True, "no new structural blockage"
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

        for identity, source in self.sources.items():
            old, new = self.before.get(source), after.get(source)
            if old is None or new is None:
                return finish(False, "layout cannot establish mobile access")
            for other in self.sources.values():
                if self.before.get(other) == old and after.get(other) != new:
                    return finish(False, "build bundle separates previously connected teammates")
            for goals in self.facilities:
                if any(self.before.get(p) == old for p in goals) and not any(after.get(p) == new for p in goals):
                    return finish(False, "build bundle cuts access to a current facility")
            if any(self.before.get(p) == old for p in neighbours(source)) and not any(after.get(p) == new for p in neighbours(source)):
                return finish(False, "build bundle traps a mobile unit")
        for identity, kind, point in builds:
            if kind in WEAPONS:
                component = after.get(self.sources.get(identity))
                stands = [p for p in neighbours(point) if p in after and after[p] == component]
                if len(stands) < 2:
                    return finish(False, "new weapon lacks two connected operator stands")
        return finish(True, "build bundle preserves current structural access")
