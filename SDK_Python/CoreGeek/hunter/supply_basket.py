"""Observed-cash shopping for complete upgrade chains and personal night stock.

A future tier is a shopping requirement, never an observed building level.
Only the normal current-level validator can authorize using a voucher.
"""
from collections import Counter
from copy import copy
import time

from . import defence_duties, procurement
from .navigation import distance_field, interaction_cells
from .protocol import WEAPONS, distance
from .day_schedule import weighted_field

OPTIONAL_STOCK = {'DizzyWeapon', 'Bomb', 'SmallRobotSummonOrder'}


def stock_quantity(world, actor, name, target, available, reserve, space, planned, minimum_reserve):
    """Bounded personal maintenance precedes optional reconstruction spending."""
    from .wall_policy import minimum_stock
    price=world.shop[name]
    held=actor.inventory[name]+sum(e['name']==name for e in planned)
    paid=sum(world.shop[e['name']] for e in planned if e['unit'] is not None)
    ordinary=max(0,available-reserve)//price
    essential=min(max(0,minimum_stock(world,actor,name)-held),
                  max(0,available-max(0,minimum_reserve-paid))//price)
    return min(space,max(0,target-held),max(ordinary,essential))


def requirements(world, rules, policy):
    from .wall_policy import upgrade_rank
    emergency = {u.id for u in world.stations if u.id in getattr(world, 'critical_base_ids', ()) and u.level in (1, 2)}
    result = []
    for unit in world.ours.values():
        if emergency and unit.id not in emergency:
            continue
        if not unit.alive or unit.level not in (1, 2):
            continue
        if not procurement.upgrade_allowed(world, unit, policy, rules):
            continue
        prefix = ('Weapon' if unit.kind in WEAPONS else 'Wall' if unit.kind == 'wall'
                  else 'Station' if unit.kind == 'station' else None)
        if not prefix:
            continue
        rank = upgrade_rank(world, unit)
        for level in range(unit.level, unit.level+1 if emergency else 3):
            result.append(dict(unit=unit, level=level, name=f'{prefix}UpgradeVoucher{level}', rank=rank))
    from .wall_service import pending_targets, service_key
    if not emergency:
        for unit in pending_targets(world,scheduled=True):
            for level in (1,2):
                result.append(dict(unit=unit,level=level,name=f'WallUpgradeVoucher{level}',rank=2,
                    pending=True,ready_after=world.wall_service[unit.pos]['build_after']))
    return sorted(result, key=lambda r:(int(r['rank']), r['level'],
        not r.get('pending',False),service_key(world,r['unit'])))


def basket(world, actor, rules, policy, deadline, *, cash=None, order_limits=None, future_repair_sites=()):
    """Match every owned tier once, then fund as much useful stock as possible."""
    roster = world.night_roster
    carriers = {u.id:u for u in world.movers if u.backpack is not None}
    carriers.setdefault(actor.id, actor)
    supply = {i:u.inventory.copy() for i,u in carriers.items()}
    fields = {i:distance_field(world, {u.pos}, u.pos, deadline) for i,u in carriers.items()}
    lengths = {}
    def reachable(identity, req):
        key = identity, req['unit'].id
        if key not in lengths:
            u = carriers[identity]
            lengths[key] = min((fields[identity][p] for p in interaction_cells(world,[req['unit'].pos],u.pos)
                               if p in fields[identity]), default=None)
        return lengths[key]
    from .wall_policy import purchase_units
    stage_ids = {u.id for u in purchase_units(world)}
    held, unfilled, covered, owners = [], [], set(), {}
    commitments={i:set(targets) for i,targets in getattr(world,'checkout_targets',{}).items()}
    def committed(req,identity):
        return (req['unit'].id,req['level']) in commitments.get(identity,set())
    requests=requirements(world,rules,policy)
    # A receipt must retain the quoted feasible targets. Reassigning a partial
    # batch by unit ID can add an excluded exterior corner and invalidate the
    # remaining checkout/use circuit immediately after the first purchase.
    from .wall_pressure import priority
    requests=sorted(enumerate(requests),key=lambda pair:(
        not any(committed(pair[1],i) for i in carriers),pair[1]['rank'],pair[1]['level'],
        priority(world,pair[1]['unit']),pair[0]))
    for _,req in requests:
        if time.monotonic() >= deadline:
            return None
        uid = req['unit'].id
        possible = [(not committed(req,i),owners.get(uid) != i, reachable(i,req), i) for i in carriers
                    if supply[i][req['name']] and reachable(i,req) is not None]
        if possible:
            _, _, _, owner = min(possible)
            supply[owner][req['name']] -= 1
            owners.setdefault(uid, owner)
            covered.add((uid, req['level']))
            if owner == actor.id: held.append(req)
            if req['name']=='WallUpgradeVoucher1' and req.get('pending'):
                world.wall_service[req['unit'].pos]['reserved_owner']=owner
        else:
            unfilled.append(req)
    costs = [r.gold for k in WEAPONS if (r:=rules.build_rule(world,k)) is not None]
    reserve = (min(costs,default=0)*max(0,rules.weapon_limit-len(world.weapons))
               + getattr(world,'treasure_reserved_gold',0))
    emergency = any(u.id in getattr(world, 'critical_base_ids', ()) and u.level in (1, 2) for u in world.stations)
    if emergency:
        reserve = 0  # Restoring the endangered base takes precedence over optional funds.
    primary = getattr(world,'upgrade_checkout_actor',actor.id)==actor.id
    worker = carriers.get(roster.w)
    if not emergency and not any(g.id in stage_ids for g in world.weapons) and actor.id == roster.p and worker and world.shop.get('WallFixer',0)>0:
        # P cannot carry W's maintenance supplies. Preserve the small amount
        # W still needs for its own two repair packs, not an arbitrary gold floor.
        reserve += max(0,2-worker.inventory['WallFixer'])*world.shop['WallFixer']
    available = max(0, (world.gold or 0)-reserve) if cash is None else max(0,cash-reserve)
    space = actor.capacity-len(actor.backpack)
    prepaid = set(covered)
    planned = []
    limits = Counter(order_limits) if order_limits is not None and not emergency else None
    from .rear_open import enabled as rear_enabled
    if (rear_enabled(world) and actor.id==roster.w and not emergency
            and not getattr(world,'critical_base_ids',())
            and len(world.weapons)==rules.weapon_limit):
        front=[u for u in world.ours.values() if u.alive and u.kind=='wall'
               and u.pos in world.monster_front_walls]
        price=world.shop.get('WallFixer',0)
        count=min(space,available//price,max(0,2-actor.inventory['WallFixer'])) if price>0 and (front or future_repair_sites) else 0
        if limits is not None:count=min(count,limits['WallFixer'])
        if count:
            world.essential_repair_stock=getattr(world,'essential_repair_stock',{})
            world.essential_repair_stock[actor.id]=dict(count=count,targets=[u.id for u in front],
                future_targets=sorted(future_repair_sites),
                round=world.round,basis='personally serviced night front stock')
            planned.extend(dict(name='WallFixer',rank=1.8,level=0,unit=None) for _ in range(count))
            available-=count*price;space-=count
            if limits is not None:limits['WallFixer']-=count
    # The maintenance worker needs real personal repair stock, not just money
    # reserved in P's basket. Fund a small working stock alongside the wall
    # stage, before its remaining cash is exhausted by upgrade chains.
    if (not emergency and not getattr(world,'critical_base_ids',())
            and actor.id in (roster.w,roster.p)
            and not (actor.id==roster.p and getattr(world,'base_restore_ids',()))
            and len(world.weapons)==rules.weapon_limit
            and not any(g.id in stage_ids for g in world.weapons)):
        price=world.shop.get('WallFixer',0)
        if price>0:
            from .wall_policy import investment_fund
            investment_reserve,_=investment_fund(world,preserve_reconstruction=False)
            # The minimum working stock is still subject to the same next
            # investment reserve as the final purchase permit. An impossible
            # top-up must not keep prepaid delivery waiting at the shop.
            stock_cash=max(0,available-max(0,investment_reserve-reserve))
            count=min(space,stock_cash//price,max(0,(2 if actor.id==roster.w else 1)-actor.inventory['WallFixer']
                -sum(e['name']=='WallFixer' for e in planned)))
            if limits is not None:count=min(count,limits['WallFixer'])
            if count:
                planned.extend(dict(name='WallFixer',rank=1.9,level=0,unit=None) for _ in range(count))
                available-=count*price;space-=count
                if limits is not None:limits['WallFixer']-=count
    # Current repair work is different from predictive spare stock. Only the
    # actual maintenance carrier already at the shop can fund this exception.
    # Its own route to an eligible wall and back to duty must fit today's clock.
    clock=getattr(world,'strategy_clock',None)
    if (not emergency and actor.id==roster.w and clock and clock.phases=={'day'}
            and world.near_zone(actor.pos,'weaponShop') and world.shop.get('WallFixer',0)>0):
        from .repair_decision import eligible
        from .wall_policy import minimum_stock
        home=defence_duties.stands(world,actor.id) if getattr(world,'task_side_plan',None) else set()
        back=distance_field(world,home,actor.pos,deadline) if home else {}
        service=[u for u in world.ours.values() if eligible(world,u,rules,policy)
            and any(fields[actor.id][p]+2+back[p]+policy.return_buffer<=clock.until_night
                for p in interaction_cells(world,[u.pos],actor.pos)
                if p in fields[actor.id] and p in back)]
        if service and time.monotonic()<deadline:
            count=max(0,min(len(service),minimum_stock(world,actor,'WallFixer'))
                -actor.inventory['WallFixer']-sum(e['name']=='WallFixer' for e in planned))
            count=min(count,space,available//world.shop['WallFixer'])
            if limits is not None:count=min(count,limits['WallFixer'])
            if count:
                world.essential_repair_stock=getattr(world,'essential_repair_stock',{})
                world.essential_repair_stock[actor.id]=dict(count=count,targets=[u.id for u in service],round=world.round)
                planned.extend(dict(name='WallFixer',rank=1.8,level=0,unit=None) for _ in range(count))
                available-=count*world.shop['WallFixer'];space-=count
                if limits is not None:limits['WallFixer']-=count
    for req in unfilled:
        if not primary:break
        if space <= 0 or time.monotonic() >= deadline:
            break
        uid = req['unit'].id
        if uid not in stage_ids:
            continue
        price = world.shop.get(req['name'],0)
        if limits is not None and limits[req['name']] <= 0:
            continue
        predecessor = req['level'] == req['unit'].level or (uid,req['level']-1) in covered
        if not predecessor or reachable(actor.id,req) is None or not 0 < price <= available:
            continue
        # Keep an actor's successive tiers together whenever it can carry them.
        # Vouchers already in another backpack are reserved, never transferred.
        planned.append(req); covered.add((uid,req['level']))
        if limits is not None:limits[req['name']] -= 1
        available -= price; space -= 1
    walls = [u for u in world.ours.values() if u.alive and u.kind=='wall']
    stock = []
    if not emergency and (primary or actor.id==roster.w):
        stock.append(('Medicine', 1 if policy.medical_stock_enabled else 2))
    if (not emergency and actor.id in (roster.w,roster.p) and walls
            and not (actor.id==roster.p and getattr(world,'base_restore_ids',()))):
        from .repair_decision import eligible
        damaged = sum(eligible(world,u,rules,policy) for u in walls)
        target=(max(2,damaged+(len(walls)+3)//4) if actor.id==roster.w else 1)
        if rear_enabled(world) and actor.id==roster.w:
            target=getattr(world,'caretaker_repair_target',max(2,policy.caretaker_repair_target))
        stock.append(('WallFixer',target))
    # Night actions and bag space bound useful explosive reserves. The budget
    # may buy several in one action rather than stopping at the old one-item cap.
    defence_reserve = sum(world.shop.get(r['name'], available) for r in unfilled
                          if r['unit'].id in stage_ids and (r['unit'].id,r['level']) not in covered)
    from .wall_policy import reconstruction_pending
    if (getattr(world,'critical_base_ids',()) or reconstruction_pending(world)) and not emergency:
        from .wall_policy import investment_fund
        paid_now=sum(world.shop[e['name']] for e in planned if e['unit'] is not None)
        defence_reserve=max(defence_reserve,max(0,investment_fund(world)[0]-paid_now))
    # Upgrade ownership serializes shared building investment, not personal
    # guard ammunition. W must budget its sale/checkout before sealing even
    # when the free pioneer owns the team's upgrade purchases.
    if (primary or actor.id == roster.w) and not emergency:
        stock.extend((('DizzyWeapon', 2), ('Bomb', 60)))
    if primary and not emergency:
        # Spend smaller residuals on next-wave pressure only after personal
        # defence stock. Held orders across all bags already occupy the quota.
        slots=getattr(world,'summon_purchase_slots',0)
        from .wall_policy import pressure_ready
        if slots and policy.summon_pressure_enabled and pressure_ready(world):
            stock.append(('SmallRobotSummonOrder', actor.inventory['SmallRobotSummonOrder']+slots))
    # Forecast proceeds may fund necessary investment, but must not make an
    # already affordable upgrade depend on an extra sale just to add optional
    # ammunition. Keep the whole basket payable from observed cash in that case.
    primary_cost = sum(world.shop[e['name']] for e in planned)
    cash_funded_investment = bool(any(e['unit'] is not None for e in planned)
        and primary_cost <= max(0,(world.gold or 0)-reserve))
    if cash_funded_investment:
        available = min(available,max(0,(world.gold or 0)-reserve-primary_cost))
    from .wall_policy import investment_fund
    minimum_reserve,_=investment_fund(world,preserve_reconstruction=False)
    for name, target in stock:
        price = world.shop.get(name,0)
        if price <= 0: continue
        count=stock_quantity(world,actor,name,target,available,defence_reserve,space,planned,minimum_reserve)
        if limits is not None:count=min(count,limits[name])
        if count:
            planned.extend(dict(name=name,rank=4,level=0,unit=None) for _ in range(count))
            available -= count*price;space -= count
    return dict(held=held, planned=planned, reserve=reserve, unspent=available,
                prepaid=prepaid, stock_targets=stock, unfunded_defence_reserve=defence_reserve,
                minimum_stock_reserve=minimum_reserve,
                cash_funded_investment=cash_funded_investment)


def use_tour(world, actor, entries, start, home, deadline, fields=None, *, arrival_offset=0):
    """Cost of actually applying selected tiers in order, ending at duty."""
    view = copy(world)
    view.occupied = world.occupied-{actor.pos}
    remaining = [e for e in entries if e['unit'] is not None]
    fields = {} if fields is None else fields
    point = start
    total = sum(e['unit'] is None and e['name'].endswith('SummonOrder') for e in entries)
    total += min(getattr(world,'summon_use_remaining',0),sum(n for k,n in actor.inventory.items() if k.endswith('SummonOrder')))
    while remaining:
        if time.monotonic() >= deadline:return None
        if point not in fields:fields[point] = distance_field(view,{point},point,deadline)
        reach = fields[point]
        # Each level-2 application follows that building's level-1 application.
        choices = [(reach[p],r['rank'],r['level'],r['unit'].id,p,index) for index,r in enumerate(remaining)
                   if not any(t['unit'].id==r['unit'].id and t['level']<r['level'] for t in remaining)
                   for p in interaction_cells(view,[r['unit'].pos],point) if p in reach]
        if not choices:return None
        # The delivery planner prioritizes last night's damaged wall within
        # a tier. Quote that same order rather than a shorter unrelated tour.
        from .wall_pressure import priority
        chosen=min(choices,key=lambda c:(c[1],priority(world,remaining[c[-1]]['unit']),c[0],c[2],c[3]))
        length,_,_,_,point,index = chosen
        entry = remaining[index]
        if entry.get('pending'):
            total=max(total+length,max(0,entry['ready_after']-world.round-arrival_offset))+1
        else:
            total += length+1
        remaining.pop(index)
    if point not in home:return None
    return total+home[point]


def quote(world, actor, clock, rules, policy, deadline, *, home, tail=None, end=None,
          cash=None, sale_stock=None, margin=None, future_repair_sites=()):
    """Fit useful purchases, skipping detours that would suppress the whole trip."""
    if actor.backpack is None or actor.capacity is None or not home:return None
    order_limits=getattr(world,'checkout_order_limits',{}).get(actor.id)
    data = basket(world,actor,rules,policy,deadline,cash=cash,order_limits=order_limits,
                  future_repair_sites=future_repair_sites)
    if data is None:return None
    tail = home if tail is None else tail
    end = min((p for p in home if home[p]==0), default=None) if end is None else end
    if end is None:return None
    margin = policy.return_buffer+8 if margin is None else margin
    shops = interaction_cells(world,world.zones.get('weaponShop',()),actor.pos)
    sale_stock = sale_stock or {}
    planned = data['planned']
    fields, routes = {}, {}
    def attempt(entries):
        orders = Counter(e['name'] for e in entries)
        # A cash-funded building investment does not need an ore-sale detour.
        # Apply this before fitting subsets, or that unnecessary detour can
        # discard a required voucher even when checkout and delivery fit.
        from .rear_open import enabled as rear_enabled
        personal_repair = (rear_enabled(world) and actor.id==world.night_roster.w
            and actor.inventory['WallFixer']<2 and orders['WallFixer']>0)
        funded = ((any(e['unit'] is not None for e in entries) or personal_repair)
            and sum(world.shop[name]*n for name,n in orders.items())
                <= max(0,(world.gold or 0)-data['reserve']))
        sales = {} if funded else sale_stock
        if actor.kind == 'pioneer' and not orders and (data['held'] or not sale_stock):
            # Checkout has finished. Start delivery at the observed carrier,
            # not at home followed by a second outward tour of the same walls.
            use_steps=use_tour(world,actor,data['held'],actor.pos,home,deadline,fields)
            if use_steps is None:return None
            # Ore liquidation is a separate optional itinerary once the
            # already-paid vouchers have been delivered.
            return dict(data,planned=entries,orders=orders,use_steps=use_steps,
                        checkout=home,sale={},required=use_steps,delivery_only=True,
                        fits=use_steps+margin<=clock.until_night)
        if actor.kind == 'pioneer' and orders:
            # P has no wall-closing obligation. Apply its coupons on the way
            # back from checkout, including walls serviced from outside. The
            # old home->tour->home estimate rejected affordable short trips.
            costs = {}
            for point in shops & home.keys():
                steps = use_tour(world,actor,data['held']+entries,point,home,deadline,fields,
                    arrival_offset=distance(actor.pos,point)+len(orders))
                if steps is not None:costs[point]=steps+len(orders)
                if time.monotonic()>=deadline:return None
            if not costs:return None
            checkout=weighted_field(world,costs,actor,deadline) or {}
            sale=(weighted_field(world,{p:checkout[p]+len(sales) for p in
                  interaction_cells(world,world.zones.get('vendor',()),actor.pos) if p in checkout},actor,deadline)
                  if sales else checkout) or {}
            required=sale.get(actor.pos)
            return dict(data,planned=entries,orders=orders,use_steps=min(costs.values())-len(orders),
                        checkout=checkout,sale=sale,required=required,
                        fits=required is not None and required+margin<=clock.until_night)
        use_steps = use_tour(world,actor,data['held']+entries,end,home,deadline,fields)
        if use_steps is None:return None
        delivery = {p:n+use_steps for p,n in tail.items()}
        # Every extra use/purchase adds a constant to the same travel field.
        # Reuse navigation while considering several feasible subsets.
        key = bool(orders), bool(sales)
        if key not in routes:
            checkout_base = (weighted_field(world,{p:tail[p] for p in shops if p in tail},actor,deadline)
                             if orders else tail)
            sale_base = (weighted_field(world,{p:checkout_base[p]+len(sales) for p in
                         interaction_cells(world,world.zones.get('vendor',()),actor.pos) if p in checkout_base},actor,deadline)
                         if sales and checkout_base else checkout_base)
            routes[key] = checkout_base or {},sale_base or {}
        checkout_base,sale_base = routes[key]
        extra = use_steps+len(orders)
        checkout = {p:n+extra for p,n in checkout_base.items()}
        sale = {p:n+extra for p,n in sale_base.items()}
        required = sale.get(actor.pos) if sale else None
        return dict(data, planned=entries, orders=orders, use_steps=use_steps,
                      checkout=checkout, sale=sale, required=required,
                      fits=required is not None and required+margin <= clock.until_night)
    def remember_targets(trip):
        if trip is not None and hasattr(world,'quoted_checkout_targets'):
            world.quoted_checkout_targets[actor.id]=list(dict.fromkeys(
                (e['unit'].id,e['level']) for e in trip['held']+trip['planned']
                if e['unit'] is not None and not e.get('pending')))
        return trip
    full = attempt(planned)
    if full and full['fits']:return remember_targets(full)
    selected, best = [], attempt([])
    covered = set(data['prepaid'])
    budget = data['unspent']+sum(world.shop[e['name']] for e in planned)
    for entry in planned:
        if time.monotonic() >= deadline:break
        unit = entry['unit']
        if unit and entry['level']>unit.level and (unit.id,entry['level']-1) not in covered:
            continue
        trial = attempt(selected+[entry])
        if trial and trial['fits']:
            selected.append(entry);best=trial
            if unit:covered.add((unit.id,entry['level']))
    # Reallocate money released by skipped upgrade detours to useful personal
    # stock. Consumables add checkout actions, not another delivery circuit.
    available = data['unspent']+sum(world.shop[e['name']] for e in planned)-sum(world.shop[e['name']] for e in selected)
    defence_reserve = data['unfunded_defence_reserve']+sum(world.shop[e['name']] for e in planned
        if e not in selected and e['unit'] is not None and e['unit'].kind != 'station')
    counts = Counter(e['name'] for e in selected)
    for name,target in data['stock_targets']:
        if time.monotonic() >= deadline:break
        price=world.shop.get(name,0)
        if price<=0:continue
        count=stock_quantity(world,actor,name,target,available,defence_reserve,
            actor.capacity-len(actor.backpack)-len(selected),selected,data['minimum_stock_reserve'])
        if order_limits is not None:count=min(count,max(0,order_limits.get(name,0)-counts[name]))
        if count<=0:continue
        entries=[dict(name=name,rank=4,level=0,unit=None) for _ in range(count)]
        trial=attempt(selected+entries)
        if trial and trial['fits']:
            selected+=entries;best=trial;available-=count*price;counts[name]+=count
    return remember_targets(best)
