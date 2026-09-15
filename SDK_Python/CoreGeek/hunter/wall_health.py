"""Session-local full-health evidence; never rewrite the formal rule table.

Only consecutive, paid, successful restorations in a robot-free daylight
frame establish a limit. A damaged snapshot is not a maximum-HP observation.
"""
from dataclasses import dataclass, field
from .protocol import obj, position


@dataclass
class WallHealth:
    levels: dict = field(default_factory=dict)
    previous: dict = field(default_factory=dict)

    def observe(self, world, clock, session):
        clean = clock.phases == {'day'} and obj(world.raw.get('robot')).get('roles') == []
        before = self.previous
        if (clean and before.get('clean') and before.get('round') == world.round-1
                and session.last_round == world.round-1):
            receipts = obj(world.raw.get('lastRoundRoleActionResults'))
            for identity, command in session.last_response.get('roleCommandMap',{}).items():
                item = command.get('name')
                building = command.get('action')=='build' and item=='wall'
                restoring = command.get('action')=='use' and item in ('WallFixer','WallUpgradeVoucher1','WallUpgradeVoucher2')
                if receipts.get(identity) is not True or not (building or restoring):
                    continue
                actor = world.ours.get(identity)
                stock = before['stock'].get(identity)
                consumed = 'stone' if building else item
                if (not actor or not actor.alive or actor.backpack is None or stock is None
                        or stock[consumed] != actor.inventory[consumed]+1):
                    continue
                points = command.get('targetPos',[])
                point = position(points[0]) if len(points)==1 else None
                wall = next((u for u in world.ours.values() if u.alive and u.kind=='wall' and u.pos==point),None)
                old = before['walls'].get(wall.id) if wall else None
                if not wall or wall.health is None or wall.health<=0:
                    continue
                if building:
                    if (actor.kind!='worker' or wall.level!=1 or old
                            or any(w[0]==point for w in before['walls'].values())):
                        continue
                else:
                    if not old or old[0]!=point:continue
                    expected = old[1] if item=='WallFixer' else {'WallUpgradeVoucher1':2,'WallUpgradeVoucher2':3}[item]
                    if (wall.level!=expected or item!='WallFixer' and old[1]!=expected-1
                            or item=='WallFixer' and (old[2] is None or wall.health<=old[2])):
                        continue
                record = self.levels.get(wall.level)
                conflict = bool(record and (record.get('conflict') or record['hp']!=wall.health))
                self.levels[wall.level] = dict(hp=wall.health,round=world.round,wall=wall.id,
                    source='confirmed_clean_build' if building else 'confirmed_clean_restoration',conflict=conflict)
        self.previous = dict(round=world.round,clean=clean,
            stock={u.id:u.inventory.copy() for u in world.movers if u.backpack is not None},
            walls={u.id:(u.pos,u.level,u.health) for u in world.ours.values() if u.alive and u.kind=='wall'})
        world.wall_health_levels = self.levels
