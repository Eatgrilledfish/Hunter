"""Observed potential base pressure, used only as an optional local heuristic.

Proximity does not reveal the robot's chosen target. The stationary remaining-
night exposure and fractional removal below are scores, not simulated damage.
"""
from .protocol import distance


class BasePressure:
    def __init__(self, world, clock):
        self.targets = {}
        self.scale = 0.0
        bases = [b for b in world.stations if b.alive]
        if clock.phases != {"night"} or len(bases) != 1:
            return
        base = bases[0]
        cells = [(base.pos[0]+dx, base.pos[1]+dy) for dx in (0, 1) for dy in (-1, 0)]
        for r in world.robots.values():
            if (r.alive and r.target_team == world.side and r.abnormal != "dizzy"
                    and r.attack_power is not None and r.attack_power > 0
                    and r.attack_range is not None
                    and min(distance(r.pos, p) for p in cells) <= r.attack_range):
                self.targets[r.id] = (r.attack_power, r.health)
        power = sum(p for p, _ in self.targets.values())
        if power:
            remaining = min(130-(world.round-o) % 130 for o in clock.offsets)
            # 5000 is the local base-loss preference already used by tactical
            # planning, not an official score or a robot attack cadence claim.
            self.scale = 5000 * min(1.0, power*remaining/base.health) / power

    def value(self, damage):
        # Combined damage is capped once per robot across the whole action set.
        return self.scale * sum(power*min(1.0, max(0, damage.get(i, 0))/hp)
                                for i, (power, hp) in self.targets.items())
