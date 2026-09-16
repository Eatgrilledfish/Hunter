"""Recent night losses by wall position, separate from instantaneous damage."""
from dataclasses import dataclass, field
from .rules import station_rings
from .protocol import distance


@dataclass
class WallPressure:
    previous: dict = field(default_factory=dict)
    sites: dict = field(default_factory=dict)
    night: int | None = None

    def observe(self, world, clock):
        night = clock.day if clock.phases == {'night'} else clock.day-1 if clock.day else None
        before = self.previous
        continuous = before.get('round') == world.round-1
        if not continuous or night != self.night:
            self.sites = {}
        self.night = night
        walls = {u.pos: u for u in world.ours.values() if u.kind == 'wall'}
        if continuous and before.get('night') == night and night is not None:
            for pos, unit in walls.items():
                old = before['walls'].get(pos)
                if not old or old[0] != unit.id or old[1] != unit.level:
                    continue  # Rebuilding/upgrading is not a damage observation.
                if old[2] is None or unit.health is None:
                    continue
                loss = max(0, old[2]-unit.health)
                if loss:
                    record = self.sites.setdefault(pos, dict(damage=0, night=night))
                    record.update(damage=record['damage']+loss, last_round=world.round)
        self.previous = dict(round=world.round, night=clock.day if clock.phases == {'night'} else None,
                             walls={p:(u.id,u.level,u.health) for p,u in walls.items()})
        world.wall_pressure = {p:dict(r) for p,r in self.sites.items()}


def priority(world, unit):
    """Tie-break within an existing investment tier; never invent future DPS."""
    record = getattr(world, 'wall_pressure', {}).get(unit.pos, {}) if unit.kind == 'wall' else {}
    damage = record.get('damage', 0)
    if not damage or unit.health is None:
        return (1, 0, 0)
    blue = set().union(*(station_rings(b.pos)[0] for b in world.stations))
    buildings = {u.pos for u in world.ours.values() if u.alive and u.kind not in {'worker','pioneer'}}
    inaccessible = not any(distance(p, unit.pos) == 1 and p not in buildings for p in blue)
    return (0, -damage/max(1,damage+unit.health), -int(inaccessible))
