"""Choose an observed daylight passage without changing permanent wall goals."""
from copy import copy
import time
from .navigation import distance_field, interaction_cells, neighbours
from .rules import station_rings


def gate(world):
    plan=getattr(world,'task_side_plan',None)
    return getattr(world,'active_access_gap',None) or (plan['gate'] if plan else None)


def prepare(world, clock, deadline, state=None):
    world.active_access_gap=None
    world.access_diagnostic={}
    plan=getattr(world,'task_side_plan',None)
    if not plan or clock.phases!={'day'} or clock.day<=1:return
    if state is not None and state.get('day')!=clock.day:
        state.clear();state['day']=clock.day
    blue,yellow=station_rings(plan['anchor'])
    walls={u.pos:u for u in world.ours.values() if u.alive and u.kind=='wall'}
    gaps=yellow-walls.keys()
    if not gaps:return
    fixed=plan['gate']
    actors=[world.ours.get(i) for i in (world.night_roster.w,world.night_roster.p)]
    if any(not u or not u.alive for u in actors):return
    blocked=set(world.occupied)-{u.pos for u in world.movers}
    for robot in world.robots.values():
        if not robot.alive:continue
        if robot.attack_range is None or robot.attack_power is None:
            world.access_diagnostic={'reason':'unknown threat prevents alternate passage selection'}
            return
        if robot.attack_power>0:
            radius=robot.attack_range
            blocked.update((x,y) for x in range(max(0,robot.pos[0]-radius),min(world.width,robot.pos[0]+radius+1))
                           for y in range(max(0,robot.pos[1]-radius),min(world.height,robot.pos[1]+radius+1)))
    outside={p for q in yellow for p in neighbours(q) if world.inside(p) and p not in blue|yellow}
    options=[]
    candidates=set(gaps)
    if fixed in walls and walls[fixed].level==1:candidates.add(fixed)
    incumbent=state.get('point') if state else None
    for point in sorted(candidates,key=lambda p:(p!=incumbent,p)):
        view=copy(world)
        # Prove this passage remains useful after the other holes are closed.
        view.occupied=(blocked | (gaps-{point}))-{point}
        cost=2 if point in walls else 0  # removal and replacement actions
        valid=True
        for actor in actors:
            reach=distance_field(view,{actor.pos},actor.pos,deadline)
            exterior=outside & reach.keys()
            if not exterior or plan['w'] not in reach:
                valid=False;break
            destinations = ('stone','vendor') if actor.kind=='worker' else ('weaponShop','vendor')
            goals=set().union(*(interaction_cells(view,world.zones.get(k,()),actor.pos) for k in destinations))
            distances=[reach[p] for p in goals if p in reach]
            if goals and not distances:
                valid=False;break
            cost+=min(distances) if distances else min(reach[p] for p in exterior)
        if time.monotonic()>=deadline:
            world.access_diagnostic={'reason':'passage comparison incomplete'}
            return
        if valid:
            options.append((cost,point in walls,point))
            # The policy already prefers today's valid selection over every
            # alternative. Once both current routes prove it valid, stop;
            # unrelated searches must not make a funded passage disappear.
            if point==incumbent:break
    if options:
        chosen=next((o for o in options if state and o[2]==state.get('point')),None)
        _,needs_remove,point=chosen or min(options)
        if state is not None:state['point']=point
        if not needs_remove:world.active_access_gap=point
        world.access_diagnostic=dict(gap=point,reused=not needs_remove,
            candidates=[dict(cost=c,remove=r,pos=p) for c,r,p in sorted(options)])
