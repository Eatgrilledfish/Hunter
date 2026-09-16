"""Bounded exterior survival choice, shared by movement and personal treatment.

Damage values are two-opportunity scenarios, not calibrated robot DPS. Recent
motion is extrapolated only as a labelled scenario, never a game speed rule.
"""
import time
import heapq
from .arbitration import Candidate
from .navigation import neighbours
from .protocol import distance, pos_json
from .rules import station_rings
from .robot_threats import active


def propose(world, clock, deadline, trapped=False):
    if clock.phases != {'night'} or len(world.stations) != 1:
        return [], {'status': 'inactive'}
    actor = world.ours.get(world.night_roster.m)
    if not actor or not actor.alive or actor.id in world.night_defenders:
        return [], {'status': 'not an exterior economist'}
    blue, yellow = station_rings(world.stations[0].pos)
    interior = blue | yellow | world.stations[0].cells
    if actor.pos in interior:
        return [], {'status': 'interior transit handled by roster'}
    threats = active(world)
    known = [r for r in threats if r.attack_range is not None and r.attack_power is not None]
    unknown = [r for r in threats if r not in known]
    motion = getattr(world, 'observed_robot_motion', {})

    def projected(r, depth):
        dx, dy = motion.get(r.id, (0, 0))
        return r.pos[0]+dx*depth, r.pos[1]+dy*depth

    def uncertain(q):
        return any(r.attack_range is not None and distance(q,r.pos)<=r.attack_range for r in unknown)

    def damage(q, depth=0):
        # Keep the stationary scenario too; moving enemies may change target.
        return 2*sum(r.attack_power for r in known if min(distance(q,r.pos),
                     distance(q,projected(r,depth))) <= r.attack_range)

    current = damage(actor.pos)
    closing = [r for r in known if motion.get(r.id, (0,0)) != (0,0)
               and distance(actor.pos,projected(r,1)) < distance(actor.pos,r.pos)
               and distance(actor.pos,projected(r,1)) <= r.attack_range]
    report = dict(known_damage_before=current, unknown_sources=len(unknown),
                  nearest=min((distance(actor.pos,r.pos) for r in threats),default=None),
                  in_range=sum(distance(actor.pos,r.pos)<=r.attack_range for r in known),
                  motion_scenario=bool(closing))
    # Recent confirmed injury makes the immediate range edge a poor place
    # to stop, even when a pursuing robot pauses for one observation. The
    # short-lived record never changes the current damage observation.
    hit = getattr(world,'observed_mover_losses',{}).get(actor.id,0)
    injury = getattr(world,'recent_mover_injuries',{}).get(actor.id)
    edge = [r for r in known if r.attack_power > 0
            and distance(actor.pos,r.pos) == r.attack_range+1]
    if ((hit > 0 or injury) and actor.health <= 110 and not actor.inventory['Medicine']
            and not current and not unknown and edge):
        choices=[]
        for q in neighbours(actor.pos):
            if time.monotonic() >= deadline:break
            if (not world.inside(q) or q in world.occupied or q in interior
                    or q in world.navigation_avoided.get(actor.pos,())):continue
            if all(min(distance(q,r.pos),distance(q,projected(r,1))) > r.attack_range+1
                   for r in known if r.attack_power > 0):
                choices.append(q)
        if choices:
            point=min(choices)
            return [Candidate(actor.id,dict(action='move',targetPos=[pos_json(point)]),1200,
                'exterior survival: clear pursuit edge after observed injury')],dict(
                report,status='post_hit_clearance',actor=actor.id,observed_loss=hit,
                injury_age=world.round-injury['round'] if injury else 0,
                decision='move',to=point,precaution=True)
    if not current and not uncertain(actor.pos) and not closing and not trapped:
        return [], dict(report,status='no observed need to retreat')
    if time.monotonic() >= deadline:
        return [], dict(report,status='budget exhausted')
    blocked = world.occupied | interior
    avoided = world.navigation_avoided
    # Three real legal steps are sufficient for local comparison; never insist
    # on a complete zero-damage global route before offering an improving step.
    beam = [(0, (), actor.pos)]
    options = []
    for depth in range(3):
        expanded = []
        for loss,path,origin in beam:
            for point in neighbours(origin):
                if time.monotonic() >= deadline:
                    break
                if (not world.inside(point) or point in blocked or uncertain(point)
                        or point in path or point in avoided.get(origin, ())):
                    continue
                # A lower three-step total is useless if its first step is
                # already lethal under one observed attack opportunity.
                if not path and damage(point,1)/2 >= actor.health:
                    continue
                route = path+(point,)
                total = loss+damage(point,depth+1)
                exits = sum(world.inside(p) and p not in blocked and not uncertain(p)
                            and damage(p,depth+2)==0 for p in neighbours(point))
                options.append((total,damage(point,depth+1),-exits,route))
                expanded.append((total,route,point))
            if time.monotonic() >= deadline:break
        beam = sorted(expanded,key=lambda r:(r[0],r[1]))[:32]
        if not beam or time.monotonic() >= deadline:break
    # Compare equal-horizon exposure, so a longer route is not penalised merely
    # for containing additional steps. Once at an endpoint, retain its cost.
    stay = sum(damage(actor.pos,t) for t in (1,2,3))
    ranked = []
    for total,end,exits,path in options:
        score = total+sum(damage(path[-1],t) for t in range(len(path)+1,4))
        if score < stay or (not current and trapped and end == 0):
            ranked.append((score,damage(path[0],1),exits,len(path),path,total))
    if not ranked or trapped:
        plan = getattr(world,'task_side_plan',None)
        goals = ({q for q in neighbours(plan['gate']) if world.inside(q) and q not in blocked
                  and not damage(q) and not uncertain(q)} if trapped and plan else None)
        queue = [(0,0,actor.pos,())]
        best = {actor.pos:(0,0)}
        while queue and time.monotonic() < deadline:
            loss,steps,point,path = heapq.heappop(queue)
            if best.get(point) != (loss,steps):continue
            if path and damage(point)==0 and (goals is None or point in goals):
                ranked = [(loss,damage(path[0],1),0,len(path),path,loss)]
                break
            for q in neighbours(point):
                if not world.inside(q) or q in blocked or uncertain(q) or q in avoided.get(point,()):continue
                if not path and damage(q,1)/2 >= actor.health:continue
                cost = (loss+damage(q),steps+1)
                if cost < best.get(q,(float('inf'),0)):
                    best[q]=cost
                    heapq.heappush(queue,(*cost,q,path+(q,)))
        if not ranked:
            if actor.inventory['Medicine'] and actor.health < 220 and damage(actor.pos,1) < 220:
                return [Candidate(actor.id,dict(action='use',name='Medicine'),1200,
                    'exterior survival: restore HP when no survivable first step exists')],dict(
                    report,status='treat_before_escape',actor=actor.id,decision='Medicine')
            return [], dict(report,status='no improving legal exterior route')
    # Preserve projected exposure and immediate risk. Among equally safe
    # routes prefer staffed fire support, including distance to its edge
    # when every local endpoint is outside range. Exit count breaks ties.
    staffed = [g for g in world.weapons if g.attack_range is not None and
        any(i in world.ours and world.ours[i].alive and distance(world.ours[i].pos,g.pos)<=1
            for i in world.night_defenders)]
    def support(row):
        return min(sum(distance(g.pos,q) <= g.attack_range for g in staffed) for q in row[4])
    def turning(row):
        # An observed displacement can turn instead of continuing straight.
        # Compare this uncertainty only after established exposure and support;
        # it is not an asserted speed, attack cadence, or expanded hard range.
        return sum(r.attack_power for depth,q in enumerate(row[4],1) for r in known
            if distance(q,r.pos) <= r.attack_range + max(map(abs,motion.get(r.id,(0,0))))*depth)
    chosen = min(
        ranked,key=lambda r:(r[0],r[1],-support(r),
                             min((max(0,distance(g.pos,r[4][-1])-g.attack_range) for g in staffed),default=0),
                             turning(r),
                             min((distance(g.pos,r[4][-1]) for g in staffed),default=0),
                             r[2],r[3],r[4]))
    score,first,_,_,route,total = chosen
    # A strictly safer legal step wins over an independent high-scoring heal.
    # If even the first step exceeds current HP but a carried dose improves the
    # stationary scenario, permit healing as the one survival action instead.
    if first >= actor.health and actor.inventory['Medicine'] and actor.health < 220 and damage(actor.pos,1) < 220:
        command = dict(action='use',name='Medicine')
        report.update(status='treat_before_escape',actor=actor.id,decision='Medicine',
                      rejected_move=list(route[0]),first_step_opportunity_bound=first)
        return [Candidate(actor.id,command,1200,'exterior survival: treatment before otherwise lethal first step')],report
    point = route[0]
    report.update(status='retreat',actor=actor.id,**{'from':actor.pos,'to':point},
                  known_damage_after=damage(point),observed_route=route,
                  route_end_damage=damage(route[-1]),route_damage_bound=total,
                  damage_budget=actor.health*.25,no_observed_exposure=not unknown and damage(point)==0,
                  decision='move',hold_scenario=stay,move_scenario=score,
                  turning_exposure_scenario=turning(chosen),
                  staffed_distance=min((distance(g.pos,route[-1]) for g in staffed),default=None),
                  turning_basis='observed displacement; tie-break only, not a movement rule')
    return [Candidate(actor.id,dict(action='move',targetPos=[pos_json(point)]),1200,
                      'exterior survival: improving escape before personal treatment')],report
