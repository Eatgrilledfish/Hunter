"""Daytime upgrade checkout shared by the maintenance worker and free pioneer.

Each commitment proves buy -> personal use -> actual duty stand. Construction
does not reserve a worker's entire day ahead of an affordable upgrade. A free
pioneer can take that trip while the worker continues its stone/wall project.
"""
from copy import copy
import time

from . import procurement, defence_duties
from .day_schedule import weighted_field, DaySchedule
from .navigation import distance_field, interaction_cells
from .rules import station_rings


def batch_size(world, actor, name, targets, home, gold, margin, left, deadline):
    """Up to three current upgrades in one shop visit, with a complete use tour.

    Targets have already been matched against every living carrier's stock.
    No higher-tier future building, resource sale or inventory transfer is used.
    """
    price=world.shop.get(name,0)
    if not price or actor.capacity is None:return 1,None
    count=min(3,actor.capacity-len(actor.backpack),gold//price,
              sum(t['name']==name for t in targets.values()))
    if count<2:return 1,None
    remaining={i:t for i,t in targets.items() if t['name']==name}
    point=actor.pos;total=1;fitted=1;required=None
    for amount in range(1,count+1):
        reach=distance_field(world,{point},actor.pos,deadline)
        options=[(reach[q],i,q) for i,t in remaining.items()
                 for q in interaction_cells(world,[t['unit'].pos],actor.pos) if q in reach and q in home]
        if not options or time.monotonic()>=deadline:break
        length,identity,point=min(options)
        total+=length+1
        remaining.pop(identity)
        if total+home[point]+margin<=left:fitted=amount;required=total+home[point]+margin
    return fitted,required


def prepare(market, world, clock, rules, policy, guidance, jobs, excluded, deadline):
    """Return None only after the leading weapon/front-wall demand is finished."""
    targets, tier, restricted, _ = procurement.upgrade_demand(world,policy,rules=rules)
    leading = {i:t for i,t in targets.items() if t['unit'].kind != 'station'
               and (not restricted or t['rank']==tier)}
    world.upgrade_priority_pending=bool(leading)
    world.upgrade_priority_items={t['name'] for t in leading.values()}
    if not leading and not market.upgrade_travellers:
        return None
    market.diagnostic={'stage':'upgrade_checkout','blocked':'funds_or_available_carrier',
                       'demand':len(leading)}
    roster=world.night_roster
    eligible={i for i in (roster.w,roster.p) if i not in excluded and i in world.ours
              and world.ours[i].alive and world.ours[i].backpack is not None
              and i not in guidance.recovery_actions
              and i not in getattr(world,'return_recovery_actions',{}) and i not in market.pending}
    market.upgrade_travellers.intersection_update(i for i in (roster.w,roster.p)
                                                 if i in world.ours and world.ours[i].alive)
    if not eligible or time.monotonic()>=deadline:
        return []
    view=copy(world)
    view.upgrade_dispatch_ids=eligible
    view.upgrade_stock_ids={i for i in (roster.w,roster.p) if i in world.ours}
    view.upgrade_busy_ids={i for i in eligible if jobs.get(i) and not jobs[i].get('gate')}
    view.upgrade_buyer_limit=1
    view.upgrade_preferred_buyer=market.upgrade_owner if market.upgrade_owner in eligible else None
    view.pioneer_trade_ids=eligible & {roster.p}
    view.pioneer_trade_stands=dict(guidance.operator_stands)
    view.pioneer_trade_stands.setdefault(roster.p,world.task_side_plan['w'])
    view.gold=max(0,(world.gold or 0)-getattr(world,'treasure_reserved_gold',0))
    plans={}
    if leading:procurement.propose(view,policy,deadline,plans=plans,rules=rules)
    _,yellow=station_rings(world.task_side_plan['anchor'])
    margin=policy.return_buffer+(2 if set(world.wall_targets or ())==yellow else 0)
    result=[]
    for identity,plan in plans.items():
        if plan['target'] not in leading and plan['stage']!='deliver':
            continue
        actor=world.ours[identity]
        target=world.ours[plan['target']]
        # P is expected to occupy A/B on return. W must not budget a shortcut
        # through that stand, even while P is still shopping or answering.
        route_world=copy(world)
        if identity==roster.w and defence_duties.rotator(world) in world.night_defenders:
            route_world.occupied=world.occupied | {world.task_side_plan['w']}
        goals=({guidance.operator_stands[identity]} if identity in guidance.operator_stands
               else defence_duties.stands(world,identity))
        home=distance_field(route_world,goals,actor.pos,deadline)
        ends=interaction_cells(route_world,[target.pos],actor.pos)
        delivery=weighted_field(route_world,{p:home[p]+1 for p in ends if p in home},actor,deadline)
        if delivery is None:
            continue
        route=delivery
        if plan['stage']=='procure':
            shops=interaction_cells(route_world,world.zones.get('weaponShop',()),actor.pos)
            route=weighted_field(route_world,{p:delivery[p]+1 for p in shops if p in delivery},actor,deadline)
        if route is None or route.get(actor.pos,float('inf'))+margin>clock.until_night:
            market.diagnostic['blocked']='upgrade_use_return_deadline'
            continue
        direct=[c for c in plan['candidates'] if c.command['action'] in ('buy','use')]
        # A direct purchase still needs a valid delivery leg from this counter.
        if direct and plan['stage']=='procure' and actor.pos not in delivery:
            direct=[]
        choices=direct or DaySchedule.moves(actor,route,'priority upgrade checkout, personal use and return')
        preview=copy(guidance)
        preview.return_routes={i:r for i,r in guidance.return_routes.items() if i!=identity}
        choices=[c for c in choices if preview.permit(c)]
        if not choices or time.monotonic()>=deadline:
            continue
        required=route[actor.pos]+margin
        if choices[0].command.get('action')=='buy':
            amount,batch_required=batch_size(route_world,actor,plan['name'],getattr(view,'upgrade_unfilled_targets',{}),
                              home,view.gold,margin,clock.until_night,deadline)
            if time.monotonic()>=deadline:continue
            for c in choices:c.command=dict(c.command,num=amount)
            if batch_required is not None:required=batch_required
        for c in choices:
            c.utility=240
            c.gold_reserve=max(c.gold_reserve,getattr(world,'treasure_reserved_gold',0))
        world.sunset_actions[identity]=[c.command for c in choices]
        guidance.day_actions[identity]=list(world.sunset_actions[identity])
        guidance.funded_actions[identity]=list(world.sunset_actions[identity])
        world.sunset_buyer=identity
        market.diagnostic.update(buyer=identity,item=plan['name'],target=target.id,
            stage='upgrade_'+plan['stage'],required=required,num=choices[0].command.get('num',1),
            blocked=None,worker_busy=roster.w in view.upgrade_busy_ids)
        result.extend(choices)
    # The quoted circuit includes the return leg. When another purchase no
    # longer fits, complete that leg instead of releasing the carrier to a new
    # wall tour. Positions are re-observed and routing is rebuilt each frame.
    for identity in sorted(market.upgrade_travellers & eligible - world.sunset_actions.keys()):
        actor=world.ours[identity]
        goals=defence_duties.stands(world,identity)
        home=distance_field(world,goals,actor.pos,deadline,
            extra_blocked={world.task_side_plan['w']} if identity==roster.w else ())
        if home.get(actor.pos)==0:
            if clock.until_night<=policy.return_buffer+8:
                world.sunset_actions[identity]=[]
                guidance.day_actions[identity]=[]
                guidance.funded_actions[identity]=[]
                market.diagnostic.update(stage='upgrade_return_hold',buyer=identity,required=0,blocked=None)
            else:
                market.upgrade_travellers.discard(identity)
            continue
        choices=DaySchedule.moves(actor,home,'complete the upgrade checkout return before new construction')
        if not choices or time.monotonic()>=deadline:continue
        preview=copy(guidance)
        preview.return_routes={i:r for i,r in guidance.return_routes.items() if i!=identity}
        choices=[c for c in choices if preview.permit(c)]
        if not choices:continue
        for c in choices:c.utility=240
        world.sunset_actions[identity]=[c.command for c in choices]
        guidance.day_actions[identity]=list(world.sunset_actions[identity])
        guidance.funded_actions[identity]=list(world.sunset_actions[identity])
        result.extend(choices)
        if world.sunset_buyer is None:
            world.sunset_buyer=identity
            market.diagnostic.update(stage='upgrade_return',buyer=identity,required=home[actor.pos],blocked=None)
    return result
