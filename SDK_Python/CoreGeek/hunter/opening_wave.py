"""Previous opening-wave exposure is a dusk precaution, not a spawn rule."""
from dataclasses import dataclass, field

from .navigation import distance_field, neighbours
from .protocol import pos_json


@dataclass
class OpeningWave:
    day: int | None = None
    round: int | None = None
    cells: frozenset = field(default_factory=frozenset)

    def observe(self, world, clock):
        # Both possible origins must place this at the beginning of night.
        # Mid-night attachments must not label travelling robots as spawns.
        if (clock.phases == {'night'} and clock.day != self.day
                and all(70 <= (clock.round-o)%130 <= 71 for o in clock.offsets)):
            cells=set()
            for robot in world.robots.values():
                if (not robot.alive or robot.target_team != world.side
                        or robot.attack_range is None or robot.attack_power is None
                        or robot.attack_power <= 0):
                    continue
                radius=robot.attack_range
                cells.update((x,y)
                    for x in range(max(0,robot.pos[0]-radius),min(world.width,robot.pos[0]+radius+1))
                    for y in range(max(0,robot.pos[1]-radius),min(world.height,robot.pos[1]+radius+1)))
            self.day,self.round,self.cells=clock.day,clock.round,frozenset(cells)
        world.previous_opening_exposure=(self.cells if clock.day == (self.day or -2)+1 else frozenset())
        world.previous_opening_round=self.round


def dusk_exit(world, clock, actor, interior, deadline):
    cells=getattr(world,'previous_opening_exposure',frozenset())
    if (not cells or 'day' not in clock.phases or clock.until_night>18
            or actor.pos in interior):
        return None, {}
    report=dict(actor=actor.id,basis='previous opening-wave observed range; recurrence uncertain',
                observed_round=world.previous_opening_round)
    # The last daylight step must not walk back into yesterday's exposure.
    if actor.pos not in cells:
        if clock.until_night<=1 and any(q in cells for q in neighbours(actor.pos)):
            return None,dict(report,hold=True)
        return None,{}
    goals={(x,y) for x in range(world.width) for y in range(world.height)
           if (x,y) not in cells|interior|world.occupied}
    route=distance_field(world,goals,actor.pos,deadline,extra_blocked=interior)
    # Another visible mover can contest an adjacent destination even when that
    # cell is empty now. For this deadline-bound retreat, prefer a complete
    # route outside those first-step destinations when one exists. This is a
    # collision precaution, not a prediction of the opponent's command.
    contested={q for unit in world.enemies.values()
               if unit.alive and unit.kind in {'worker','pioneer'}
               for q in neighbours(unit.pos)}
    if contested:
        alternate=distance_field(world,goals,actor.pos,deadline,
                                 extra_blocked=interior|contested)
        if actor.pos in alternate:
            route=alternate
            report['collision_precaution']='visible opposing mover adjacent destinations'
    length=route.get(actor.pos)
    margin=max(3,getattr(getattr(world,'strategy_policy',None),'return_buffer',3))
    if length is None or clock.until_night>length+margin:
        return None,{}
    choices=[q for q in neighbours(actor.pos) if route.get(q,float('inf'))<length
             and q not in world.navigation_avoided.get(actor.pos,set())]
    if not choices:return None,{}
    point=min(choices,key=lambda q:(route[q],q))
    return dict(action='move',targetPos=[pos_json(point)]),dict(report,steps=length)
