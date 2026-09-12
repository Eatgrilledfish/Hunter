"""Observed exterior escape routes; execute one step and replan after feedback."""
from collections import deque
import time
from .arbitration import Candidate
from .navigation import neighbours
from .protocol import distance,pos_json
from .rules import station_rings


def propose(world, clock, deadline):
    if clock.phases != {'night'} or len(world.stations)!=1:
        return [],{'status':'inactive'}
    roster=world.night_roster
    actor=world.ours.get(roster.m)
    if not actor or not actor.alive or actor.id in world.night_defenders:
        return [],{'status':'not an exterior economist'}
    blue,yellow=station_rings(world.stations[0].pos)
    interior=blue|yellow|world.stations[0].cells
    if actor.pos in interior:
        return [],{'status':'interior transit handled by roster'}
    threats=[r for r in world.robots.values() if r.alive and r.abnormal!='dizzy']
    known=[r for r in threats if r.attack_power is not None and r.attack_range is not None]
    unknown=[r for r in threats if r not in known]
    def damage(point):
        return 2*sum(r.attack_power for r in known if distance(r.pos,point)<=r.attack_range)
    current=damage(actor.pos)
    if not current and not unknown:
        return [],{'status':'no observed need to retreat'}
    blocked=world.occupied|interior
    queue=deque([(actor.pos,())]);seen={actor.pos};options=[];found_depth=None
    before=min((distance(actor.pos,r.pos) for r in unknown),default=0)
    # A damage plateau is traversable: at an edge a worker may need lateral
    # steps before leaving the current attack area. Never increase observed
    # exposure along that route, and never walk closer to an unknown source.
    # Search complete breadth layers; on expiry publish no partial route.
    while queue:
        origin,path=queue.popleft()
        if found_depth is not None and len(path)>=found_depth:
            break
        for point in neighbours(origin):
            if time.monotonic()>=deadline:
                return [],{'status':'budget exhausted'}
            if (not world.inside(point) or point in blocked or point in seen or
                    point in world.navigation_avoided.get(origin,set())):
                continue
            value=damage(point)
            if value>damage(origin):
                continue
            if unknown and any(distance(point,r.pos)<distance(origin,r.pos) for r in unknown):
                continue
            seen.add(point)
            route=path+(point,)
            separation=min((distance(point,r.pos) for r in unknown),default=0)
            if value<current or separation>before:
                clearance=min((distance(point,r.pos)-r.attack_range for r in known),default=0)
                options.append((value,-separation,-clearance,route))
                found_depth=len(route)
            else:
                queue.append((point,route))
    if not options:
        return [],{'status':'no improving legal exterior route','known_damage_bound':current,
                   'unknown_sources':len(unknown)}
    _,_,_,route=min(options)
    point=route[0];value=damage(point)
    report={'status':'retreat','actor':actor.id,'from':actor.pos,'to':point,
            'known_damage_before':current,'known_damage_after':value,'unknown_sources':len(unknown),
            'observed_route':route,'route_end_damage':damage(route[-1]),
            'no_observed_exposure':not unknown and value==0}
    return [Candidate(actor.id,{'action':'move','targetPos':[pos_json(point)]},1200,
                      'observed exterior economic-worker retreat')],report
