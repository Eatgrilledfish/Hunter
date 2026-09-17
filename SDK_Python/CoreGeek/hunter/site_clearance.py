"""Coordinate imminent wall construction with the role occupying its cell."""
import time

from .arbitration import Candidate
from .navigation import neighbours, distance_field, interaction_cells
from .protocol import distance, pos_json


def propose(world, clock, rules, guidance, deadline, task_actor=None):
    if clock.phases != {'day'} or not world.build_interior:
        return []
    reserved = getattr(world,'operator_excluded_cells',set())
    if not reserved:
        return []
    rule = rules.build_rule(world,'wall')
    if not rule or world.gold is None or world.gold < rule.gold:
        return []
    candidates=[];destinations=set()
    for actor in sorted(world.movers,key=lambda a:a.id):
        if actor.id == task_actor or actor.pos not in reserved:
            continue
        builders=[u for u in world.movers if u.id != actor.id and u.kind=='worker'
                  and u.backpack is not None and distance(u.pos,actor.pos)<=1
                  and all(u.inventory[k]>=n for k,n in rule.items.items())]
        if not builders:
            continue
        # Do not clear a wall by occupying the builder's only interior cell
        # that can both finish this wall and operate a gun. That displaced the
        # builder's return route in a continuous R67 construction counterexample.
        work_cells={p for p in world.build_interior if distance(p,actor.pos)<=1
                    and p not in world.occupied-{u.pos for u in builders}
                    and any(distance(p,g.pos)<=1 for g in world.weapons)}
        if getattr(world,'night_roster',None) and getattr(world,'task_side_plan',None):
            from .defence_duties import stands
            # Exterior M is not a turret operator. Its wall work must not
            # evict P from P's own valid interior arrival cell merely because
            # that cell could operate a gun. Preserve actual builders' duties.
            work_cells &= {p for builder in builders if builder.id in world.night_defenders
                           for p in stands(world,builder.id)}
        occupied_stands={p for i,p in guidance.operator_stands.items() if i != actor.id}
        options=[p for p in neighbours(actor.pos) if world.inside(p) and p not in
                 world.occupied|set(reserved)|destinations|occupied_stands
                 and p not in world.navigation_avoided.get(actor.pos,set())
                 and p not in guidance.blocked_moves.get(actor.id,set())]
        if not options:
            continue
        viable=[]
        for point in options:
            goals=interaction_cells(world,[g.pos for g in world.weapons],point,extra_blocked=reserved)
            field=distance_field(world,goals,point,deadline,extra_blocked=reserved)
            if point in field and 1+field[point]<=clock.until_night:
                viable.append(point)
        if not viable:
            continue
        point=min(viable,key=lambda p:(p in work_cells,p not in world.build_interior,
                      distance(p,guidance.operator_stands.get(actor.id,actor.pos)),
                      -sum(distance(p,g.pos)<=1 for g in world.weapons),p))
        destinations.add(point)
        candidates.append(Candidate(actor.id,{'action':'move','targetPos':[pos_json(point)]},70,
                                    'clear reserved wall cell for adjacent funded builder'))
    return [] if time.monotonic()>=deadline else candidates
