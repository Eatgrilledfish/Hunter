"""Bounded exterior survival choice, shared by movement and personal treatment.

Damage values are two-opportunity scenarios, not calibrated robot DPS. Recent
motion is extrapolated only as a labelled scenario, never a game speed rule.
"""
import time
import heapq
from functools import lru_cache
from .arbitration import Candidate
from .navigation import neighbours
from .protocol import distance, pos_json
from .rules import station_rings
from .robot_threats import active


def propose(world, clock, deadline, trapped=False, state=None):
    previous=dict(state) if state is not None else {}
    if state is not None:state.clear()
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
    from .rear_open import enabled as rear_enabled
    from .robot_threats import resolved
    waking = ([resolved(r) for r in world.robots.values() if r.alive and r.abnormal=='dizzy']
              if rear_enabled(world) else [])
    waking = [r for r in waking if r.attack_range is not None and r.attack_power is not None]
    wake_near = any(distance(actor.pos,r.pos)<=r.attack_range+1 for r in waking)
    motion = getattr(world, 'observed_robot_motion', {})

    def projected(r, depth):
        dx, dy = motion.get(r.id, (0, 0))
        return r.pos[0]+dx*depth, r.pos[1]+dy*depth

    @lru_cache(maxsize=None)
    def uncertain(q):
        return any(r.attack_range is not None and distance(q,r.pos)<=r.attack_range for r in unknown)

    @lru_cache(maxsize=None)
    def damage(q, depth=0):
        # Search revisits the same cell/horizon across many routes. The
        # observation is immutable for this call; cache only within this turn
        # so the real deadline is spent exploring, not recomputing exposure.
        # Keep the stationary scenario too; moving enemies may change target.
        # A measured displacement need not continue straight. Evaluate the
        # same bounded turning scenario used below across the short route,
        # not merely after a straight-line route has already won. Depth zero
        # remains observed range; this is a scenario, not a speed/cadence rule.
        return 2*sum(r.attack_power for r in known if distance(q,r.pos)
                     <= r.attack_range+max(map(abs,motion.get(r.id,(0,0))))*depth)

    forecast = damage
    if waking:
        @lru_cache(maxsize=None)
        def forecast(q, depth=0):
            # Stunned robots cannot attack now. Their observed firing area is
            # still a future route hazard; no remaining stun timer or future
            # displacement is invented. Use the temporary respite to escape.
            return damage(q,depth)+2*sum(r.attack_power for r in waking
                                         if distance(q,r.pos)<=r.attack_range)

    current = damage(actor.pos)
    closing = [r for r in known if motion.get(r.id, (0,0)) != (0,0)
               and distance(actor.pos,projected(r,1)) < distance(actor.pos,r.pos)
               and distance(actor.pos,projected(r,1)) <= r.attack_range]
    report = dict(known_damage_before=current, unknown_sources=len(unknown),
                  nearest=min((distance(actor.pos,r.pos) for r in threats),default=None),
                  in_range=sum(distance(actor.pos,r.pos)<=r.attack_range for r in known),
                  motion_scenario=bool(closing))
    if waking:
        report['stun_wake_scenario']=[r.id for r in waking]
    continuation=None
    def resume_escape(route,bound,pursuers):
        state.update(round=world.round,actor=actor.id,path=route,pursuers=sorted(pursuers),committed=True)
        report.update(status='continue_observed_escape',actor=actor.id,to=route[0],
            observed_route=route,route_damage_bound=bound,decision='move',
            basis='observed prior step; revalidated route prevents support-direction reversal')
        return [Candidate(actor.id,dict(action='move',targetPos=[pos_json(route[0])]),1200,
            'exterior survival: finish revalidated escape from observed pursuit')],report
    if rear_enabled(world) and state is not None and previous:
        path=previous.get('path',())
        pursuers=set(previous.get('pursuers',())) & {r.id for r in known+waking}
        route=tuple(path[1:])
        # Replanning a safe first step forever can orbit a pursuer at the map
        # edge. Continue a complete escape only after its prior move is really
        # observed, and revalidate every cell and the whole exposure scenario.
        valid=(previous.get('round')==world.round-1 and previous.get('actor')==actor.id
            and path and actor.pos==path[0] and route and pursuers
            and not forecast(route[-1]) and not uncertain(route[-1]))
        origin=actor.pos;bound=0
        if valid:
            for depth,q in enumerate(route,1):
                if (time.monotonic()>=deadline or not world.inside(q)
                        or q in world.occupied or q in interior or distance(origin,q)!=1
                        or q in world.navigation_avoided.get(origin,()) or uncertain(q)):
                    valid=False;break
                bound+=forecast(q,min(depth,3))
                origin=q
        if valid and time.monotonic()<deadline and bound<actor.health and forecast(route[0],1)/2<actor.health:
            continuation=(route,bound,pursuers)
            if previous.get('committed'):return resume_escape(*continuation)
    # Recent confirmed injury makes the immediate range edge a poor place
    # to stop, even when a pursuing robot pauses for one observation. The
    # short-lived record never changes the current damage observation.
    hit = getattr(world,'observed_mover_losses',{}).get(actor.id,0)
    injury = getattr(world,'recent_mover_injuries',{}).get(actor.id)
    edge = [r for r in known if r.attack_power > 0
            and distance(actor.pos,r.pos) == r.attack_range+1]
    if ((hit > 0 or injury) and not actor.inventory['Medicine']
            and not current and not unknown and edge and not wake_near):
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
    if not current and not uncertain(actor.pos) and not closing and not trapped and not wake_near:
        return [], dict(report,status='no observed need to retreat')
    if time.monotonic() >= deadline:
        return [], dict(report,status='budget exhausted')
    blocked = world.occupied | interior
    avoided = world.navigation_avoided
    safe_exterior = None
    if rear_enabled(world):
        # Compare complete escape routes, not a short safe prefix ending in
        # a wall/boundary pocket. The base exterior is an existing staffed
        # support destination. Only the next step is committed, and ordinary
        # economic scheduling resumes once the observed pursuit is cleared.
        safe_exterior = {q for p in yellow for q in neighbours(p)
                         if world.inside(q) and q not in blocked
                         and not forecast(q) and not uncertain(q)}
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
                total = loss+forecast(point,depth+1)
                exits = sum(world.inside(p) and p not in blocked and not uncertain(p)
                            and forecast(p,depth+2)==0 for p in neighbours(point))
                options.append((total,forecast(point,depth+1),-exits,route))
                expanded.append((total,route,point))
            if time.monotonic() >= deadline:break
        beam = sorted(expanded,key=lambda r:(r[0],r[1]))[:32]
        if not beam or time.monotonic() >= deadline:break
    # Compare equal-horizon exposure, so a longer route is not penalised merely
    # for containing additional steps. Once at an endpoint, retain its cost.
    stay = sum(forecast(actor.pos,t) for t in (1,2,3))
    ranked = []
    for total,end,exits,path in options:
        score = total+sum(forecast(path[-1],t) for t in range(len(path)+1,4))
        if score < stay or (not current and trapped and end == 0):
            ranked.append((score,damage(path[0],1),exits,len(path),path,total))
    if safe_exterior or not ranked or trapped or all(forecast(row[4][-1])>0 for row in ranked):
        # A locally improving prefix can still end in the observed firing
        # strip. Search the existing complete-exit fallback before accepting
        # that prefix; retain it if the bounded search finds no better exit.
        plan = getattr(world,'task_side_plan',None)
        goals = ({q for q in neighbours(plan['gate']) if world.inside(q) and q not in blocked
                  and not forecast(q) and not uncertain(q)} if trapped and plan else safe_exterior)
        queue = [(0,0,0,actor.pos,())]
        best = {actor.pos:(0,0,0)}
        escapes = []
        escape_loss = None
        while queue and time.monotonic() < deadline:
            loss,first_risk,steps,point,path = heapq.heappop(queue)
            if best.get(point) != (loss,first_risk,steps):continue
            cost_key=(loss,first_risk,steps) if safe_exterior else (loss,0,0)
            if escape_loss is not None and cost_key > escape_loss:break
            if path and forecast(point)==0 and (goals is None or point in goals):
                # Equal-loss exits must reach the common support/tie-break
                # ranking. Heap coordinate order is not a survival policy.
                escape_loss = cost_key
                escapes.append((loss,damage(path[0],1),0,len(path),path,loss))
                continue
            for q in neighbours(point):
                if not world.inside(q) or q in blocked or uncertain(q) or q in avoided.get(point,()):continue
                if not path and damage(q,1)/2 >= actor.health:continue
                # Do not discard a safe first step when two complete routes
                # merge at the same cell. Stationary total exposure alone can
                # prefer stepping into the already observed pursuit motion.
                risk=(first_risk if path else damage(q,1)) if safe_exterior else 0
                cost = (loss+forecast(q),risk,steps+1)
                if cost < best.get(q,(float('inf'),0,0)):
                    best[q]=cost
                    heapq.heappush(queue,(*cost,q,path+(q,)))
        if escapes:ranked = escapes
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
        route=row[4][:3] if safe_exterior else row[4]
        return sum(r.attack_power for depth,q in enumerate(route,1) for r in known
            if distance(q,r.pos) <= r.attack_range + max(map(abs,motion.get(r.id,(0,0))))*depth)
    chosen = min(
        ranked,key=lambda r:(r[0],r[1],-support(r),
                             min((max(0,distance(g.pos,r[4][-1])-g.attack_range) for g in staffed),default=0),
                             turning(r),
                             min((distance(g.pos,r[4][-1]) for g in staffed),default=0),
                             r[2],r[3],r[4]))
    score,first,_,_,route,total = chosen
    if continuation and time.monotonic()<deadline:
        old_route=continuation[0]
        goal=old_route[-1]
        if (distance(route[0],goal)>distance(actor.pos,goal)
                and distance(old_route[0],goal)<distance(actor.pos,goal)):
            return resume_escape(*continuation)
    # A strictly safer legal step wins over an independent high-scoring heal.
    # If even the first step exceeds current HP but a carried dose improves the
    # stationary scenario, permit healing as the one survival action instead.
    if first >= actor.health and actor.inventory['Medicine'] and actor.health < 220 and damage(actor.pos,1) < 220:
        command = dict(action='use',name='Medicine')
        report.update(status='treat_before_escape',actor=actor.id,decision='Medicine',
                      rejected_move=list(route[0]),first_step_opportunity_bound=first)
        return [Candidate(actor.id,command,1200,'exterior survival: treatment before otherwise lethal first step')],report
    point = route[0]
    if (state is not None and safe_exterior and route[-1] in safe_exterior):
        state.update(round=world.round,actor=actor.id,path=route,
            committed=False,
            pursuers=sorted(r.id for r in known+waking
                if distance(actor.pos,r.pos)<=r.attack_range+1 or r in closing))
    report.update(status='retreat',actor=actor.id,**{'from':actor.pos,'to':point},
                  known_damage_after=damage(point),observed_route=route,
                  route_end_damage=damage(route[-1]),route_damage_bound=total,
                  damage_budget=actor.health*.25,no_observed_exposure=not unknown and damage(point)==0,
                  decision='move',hold_scenario=stay,move_scenario=score,
                  turning_exposure_scenario=turning(chosen),
                  staffed_distance=min((distance(g.pos,route[-1]) for g in staffed),default=None),
                  turning_basis='observed displacement short-route scenario; not a movement rule')
    return [Candidate(actor.id,dict(action='move',targetPos=[pos_json(point)]),1200,
                      'exterior survival: improving escape before personal treatment')],report
