"""A free pioneer buys and applies its own repair pack within daylight."""
import time
from .arbitration import Candidate
from .day_schedule import weighted_field, DaySchedule
from .navigation import interaction_cells
from .protocol import pos_json
from .repair_decision import eligible


def owner(world, unit):
    """Paid delivery and active maintenance share one building exclusion."""
    for identity,targets in getattr(world,'checkout_targets',{}).items():
        if any(uid==unit.id for uid,_ in targets):return identity
    plan=getattr(world,'wall_rebuild_plan',{})
    if plan.get('position')==unit.pos:return plan.get('actor')
    return getattr(world,'maintenance_targets',{}).get(unit.id)


def propose(world, actor, clock, rules, policy, deadline, home):
    if actor.id!=world.night_roster.p or clock.phases!={'day'}:return [],{}
    from .rear_open import enabled
    if not enabled(world):return [],{}
    report=dict(reason='no_unassigned_damaged_wall')
    options=[]
    held=actor.inventory['WallFixer']>0
    if not held:
        return [], dict(reason='repair_stock_owned_by_worker')
    price=world.shop.get('WallFixer',0)
    reserve=sum(r['granted'] for r in getattr(world,'funding_plan',()))
    if not held and (price<=0 or actor.capacity is None or len(actor.backpack)>=actor.capacity
                     or (world.gold or 0)<price+reserve):
        return [],dict(reason='personal_pack_capacity_or_unreserved_cash',price=price,reserve=reserve)
    for wall in world.ours.values():
        if not eligible(world,wall,rules,policy) or owner(world,wall) not in (None,actor.id):continue
        # A coupon already assigned to this wall must be applied first.
        if wall.level in (1,2) and actor.inventory[f'WallUpgradeVoucher{wall.level}']:continue
        if any(r.get('target_id')==wall.id and r['granted']>=r['cost'] for r in getattr(world,'funding_plan',())):continue
        goals=interaction_cells(world,[wall.pos],actor.pos)
        service=weighted_field(world,{p:home[p]+1 for p in goals if p in home},actor,deadline) or {}
        route=service if held else weighted_field(world,{p:service[p]+1 for p in
            interaction_cells(world,world.zones.get('weaponShop',()),actor.pos) if p in service},actor,deadline) or {}
        required=route.get(actor.pos,float('inf'))+policy.return_buffer+8
        if required<=clock.until_night:options.append((wall.health,required,wall.id,wall,goals,route))
        if time.monotonic()>=deadline:return [],dict(reason='repair_planning_budget')
    if not options:return [],dict(report,reason='no_unassigned_wall_with_complete_return_route')
    _,required,_,wall,goals,route=min(options,key=lambda r:r[:3])
    if held and actor.pos in goals:
        choices=[Candidate(actor.id,dict(action='use',name='WallFixer',targetPos=[pos_json(wall.pos)]),240,'pioneer daytime wall maintenance')]
    elif not held and world.near_zone(actor.pos,'weaponShop'):
        choices=[Candidate(actor.id,dict(action='buy',name='WallFixer',num=1),240,'personal pack for assigned daylight repair',gold_reserve=reserve)]
        world.essential_repair_stock=dict(getattr(world,'essential_repair_stock',{}))
        world.essential_repair_stock[actor.id]=dict(count=1,targets=[wall.id],round=world.round)
    else:choices=DaySchedule.moves(actor,route,'pioneer completes personal repair and returns before night')
    return choices,dict(reason='assigned_day_repair',target=wall.id,position=wall.pos,required=required,
        latest_departure=world.round+clock.until_night-required,held=held,cost=0 if held else price)
