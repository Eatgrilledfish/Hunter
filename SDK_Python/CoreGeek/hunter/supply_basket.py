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


def basket(world, actor, rules, policy, deadline, *, cash=None, order_limits=None):
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
    for req in requirements(world, rules, policy):
        if time.monotonic() >= deadline:
            return None
        uid = req['unit'].id
        possible = [(owners.get(uid) != i, reachable(i,req), i) for i in carriers
                    if supply[i][req['name']] and reachable(i,req) is not None]
        if possible:
            _, _, owner = min(possible)
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
    # The maintenance worker needs real personal repair stock, not just money
    # reserved in P's basket. Fund a small working stock alongside the wall
    # stage, before its remaining cash is exhausted by upgrade chains.
    if (not emergency and actor.id==roster.w and len(world.weapons)==rules.weapon_limit
            and not any(g.id in stage_ids for g in world.weapons)):
        price=world.shop.get('WallFixer',0)
        if price>0:
            count=min(space,available//price,max(0,2-actor.inventory['WallFixer']))
            if limits is not None:count=min(count,limits['WallFixer'])
            if count:
                planned.extend(dict(name='WallFixer',rank=1.9,level=0,unit=None) for _ in range(count))
                available-=count*price;space-=count
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
    if not emergency and actor.id == roster.w and walls:
        from .repair_decision import eligible
        damaged = sum(eligible(world,u,rules,policy) for u in walls)
        stock.append(('WallFixer', max(2, damaged+(len(walls)+3)//4)))
    # Night actions and bag space bound useful explosive reserves. The budget
    # may buy several in one action rather than stopping at the old one-item cap.
    defence_reserve = sum(world.shop.get(r['name'], available) for r in unfilled
                          if r['unit'].id in stage_ids and (r['unit'].id,r['level']) not in covered)
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
    for name, target in stock:
        price = world.shop.get(name,0)
        if price <= 0: continue
        spendable = max(0,available-defence_reserve)
        count = min(space, spendable//price, max(0,target-actor.inventory[name]-sum(r['name']==name for r in planned)))
        if limits is not None:count=min(count,limits[name])
        if count:
            planned.extend(dict(name=name,rank=4,level=0,unit=None) for _ in range(count))
            available -= count*price;space -= count
    return dict(held=held, planned=planned, reserve=reserve, unspent=available,
                prepaid=prepaid, stock_targets=stock, unfunded_defence_reserve=defence_reserve)


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
        length,_,_,_,point,index = min(choices)
        entry = remaining[index]
        if entry.get('pending'):
            total=max(total+length,max(0,entry['ready_after']-world.round-arrival_offset))+1
        else:
            total += length+1
        remaining.pop(index)
    if point not in home:return None
    return total+home[point]


def quote(world, actor, clock, rules, policy, deadline, *, home, tail=None, end=None,
          cash=None, sale_stock=None, margin=None):
    """Fit useful purchases, skipping detours that would suppress the whole trip."""
    if actor.backpack is None or actor.capacity is None or not home:return None
    order_limits=getattr(world,'checkout_order_limits',{}).get(actor.id)
    data = basket(world,actor,rules,policy,deadline,cash=cash,order_limits=order_limits)
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
            sale=(weighted_field(world,{p:checkout[p]+len(sale_stock) for p in
                  interaction_cells(world,world.zones.get('vendor',()),actor.pos) if p in checkout},actor,deadline)
                  if sale_stock else checkout) or {}
            required=sale.get(actor.pos)
            return dict(data,planned=entries,orders=orders,use_steps=min(costs.values())-len(orders),
                        checkout=checkout,sale=sale,required=required,
                        fits=required is not None and required+margin<=clock.until_night)
        use_steps = use_tour(world,actor,data['held']+entries,end,home,deadline,fields)
        if use_steps is None:return None
        delivery = {p:n+use_steps for p,n in tail.items()}
        # Every extra use/purchase adds a constant to the same travel field.
        # Reuse navigation while considering several feasible subsets.
        key = bool(orders)
        if key not in routes:
            checkout_base = (weighted_field(world,{p:tail[p] for p in shops if p in tail},actor,deadline)
                             if orders else tail)
            sale_base = (weighted_field(world,{p:checkout_base[p]+len(sale_stock) for p in
                         interaction_cells(world,world.zones.get('vendor',()),actor.pos) if p in checkout_base},actor,deadline)
                         if sale_stock and checkout_base else checkout_base)
            routes[key] = checkout_base or {},sale_base or {}
        checkout_base,sale_base = routes[key]
        extra = use_steps+len(orders)
        checkout = {p:n+extra for p,n in checkout_base.items()}
        sale = {p:n+extra for p,n in sale_base.items()}
        required = sale.get(actor.pos) if sale else None
        return dict(data, planned=entries, orders=orders, use_steps=use_steps,
                      checkout=checkout, sale=sale, required=required,
                      fits=required is not None and required+margin <= clock.until_night)
    full = attempt(planned)
    if full and full['fits']:return full
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
        spendable=max(0,available-defence_reserve)
        count=min(actor.capacity-len(actor.backpack)-len(selected),spendable//price,
                  max(0,target-actor.inventory[name]-counts[name]))
        if order_limits is not None:count=min(count,max(0,order_limits.get(name,0)-counts[name]))
        if count<=0:continue
        entries=[dict(name=name,rank=4,level=0,unit=None) for _ in range(count)]
        trial=attempt(selected+entries)
        if trial and trial['fits']:
            selected+=entries;best=trial;available-=count*price;counts[name]+=count
    return best
