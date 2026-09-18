"""A real ore sale may fund W's personal maintenance before its deadline."""
import time
from copy import copy
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import MINERALS, distance, pos_json
from .robot_threats import active
from .repair_decision import purchase_floor
from . import defence_duties, rear_open


def propose(world, clock, rules, policy, miner, deadline, *, stock=None):
    if (not rear_open.enabled(world) or world.gold is None or not miner
            or not miner.alive or miner.backpack is None
            or miner.id!=world.night_roster.m or miner.id in world.night_defenders
            or getattr(world,'critical_base_ids',()) or clock.day is None):
        return None,{}
    demands=[r for r in getattr(world,'funding_plan',()) if r.get('deficit',0)>0]
    buyer=min(demands,key=lambda r:(r.get('latest_departure',r['deadline']),r['purpose']!='night_essential')) if demands else None
    worker=world.ours.get(buyer['owner'] if buyer else world.night_roster.w)
    from .medical import needs_treatment
    if needs_treatment(world,miner,clock):return None,{}
    price=world.shop.get('WallFixer',0)
    if (not worker or not worker.alive or worker.backpack is None or (price<=0 and not buyer)
            or worker.capacity is None or len(worker.backpack)>=worker.capacity):return None,{}
    missing=max(0,purchase_floor(world,policy)-worker.inventory['WallFixer'])
    cost=missing*price
    available=max(0,world.gold-getattr(world,'treasure_reserved_gold',0))
    if buyer:
        cost=sum(r['cost'] for r in world.funding_plan if r['owner']==worker.id and r['deadline']<=buyer['deadline'])
        missing=sum(r['deficit'] for r in world.funding_plan if r['owner']==worker.id and r['deadline']<=buyer['deadline'])
        available=cost-missing
    if not missing or available>=cost:return None,{}
    if clock.phases=={'day'}:horizon=clock.until_night
    elif clock.phases=={'night'} and clock.day<10:
        explicit=isinstance(world.raw.get('robot'),dict) and isinstance(world.raw['robot'].get('roles'),list)
        if not explicit or any(r.alive and r.target_team in (None,world.side) for r in world.robots.values()):return None,{}
        horizon=min(130-(clock.round-o)%130 for o in clock.offsets)+70
    else:return None,{}
    if buyer:
        horizon=min(horizon,max(0,buyer['deadline']-world.round))
    if stock is None:
        stock={k:miner.inventory[k] for k in MINERALS if miner.inventory[k] and world.vendor.get(k,0)>0}
        walls={u.pos for u in world.ours.values() if u.alive and u.kind=='wall'}
        rule=rules.build_rule(world,'wall')
        reserve=max(0,len(rear_open.required(world)-walls)*(rule.items.get('stone',0) if rule else 0)
                    -world.ours[world.night_roster.w].inventory['stone'])
        if 'stone' in stock:stock['stone']=max(0,stock['stone']-reserve)
        stock={k:n for k,n in stock.items() if n>0}
    value=sum(n*world.vendor.get(k,0) for k,n in stock.items())
    if value<=0:return None,{}
    blocked=set()
    for r in active(world):
        if r.attack_range is None or r.attack_power is None:return None,{}
        if r.attack_power<=0:continue
        if distance(miner.pos,r.pos)<=r.attack_range:return None,{}
        radius=r.attack_range+1
        blocked.update((x,y) for x in range(max(0,r.pos[0]-radius),min(world.width,r.pos[0]+radius+1))
                       for y in range(max(0,r.pos[1]-radius),min(world.height,r.pos[1]+radius+1)))
    vendors=interaction_cells(world,world.zones.get('vendor',()),miner.pos,blocked)
    sell=distance_field(world,vendors,miner.pos,deadline,blocked)
    sale_walk=sell.get(miner.pos)
    if sale_walk is None:return None,{}
    shops=interaction_cells(world,world.zones.get('weaponShop',()),worker.pos,blocked)
    reach=distance_field(world,{worker.pos},worker.pos,deadline,blocked)
    home=distance_field(world,defence_duties.stands(world,worker.id),worker.pos,deadline,blocked)
    buy_rounds=len({name for row in world.funding_plan if row['owner']==worker.id and row['deadline']<=buyer['deadline'] for name in row['items']}) if buyer else 1
    options=[(max(sale_walk+len(stock)+1,reach[p])+buy_rounds+1+home[p]+policy.return_buffer,p)
             for p in shops&reach.keys()&home.keys()]
    if buyer and buyer.get('target_id') in world.ours:
        # Price the same shop -> assigned building -> duty tour as the grant.
        # M's sale and the buyer's outward walk can progress concurrently.
        from .day_schedule import weighted_field
        target=world.ours[buyer['target_id']]
        view=copy(world);view.occupied=world.occupied|blocked
        service=weighted_field(view,{p:home[p]+1 for p in
            interaction_cells(world,[target.pos],worker.pos,blocked) if p in home},worker,deadline) or {}
        options=[(max(sale_walk+len(stock)+1,reach[p])+buy_rounds+service[p]+policy.return_buffer+8,p)
                 for p in shops&reach.keys()&service.keys()]
    if not options or time.monotonic()>=deadline:return None,{}
    required,shop=min(options)
    if required>horizon:return None,{}
    name=max(stock,key=lambda k:(stock[k]*world.vendor[k],k))
    if sale_walk==0:command=dict(action='sell',name=name,num=stock[name])
    else:
        steps=sorted(q for q in neighbours(miner.pos) if sell.get(q,float('inf'))<sale_walk)
        if not steps:return None,{}
        command=dict(action='move',targetPos=[pos_json(steps[0])])
    report=dict(actor=miner.id,buyer=worker.id,stage='MAINTENANCE_FUNDING',
        purposes=[r['purpose'] for r in demands],
        required_gold=cost,cash_gap=cost-available,actual_stock_value=value,
        required_rounds=required,deadline_round=world.round+horizon,shop=shop,
        latest_sale_departure=world.round+horizon-required,
        basis='current personal stock and observed quotes; proceeds not yet spendable')
    world.maintenance_funding=report
    return command,report
