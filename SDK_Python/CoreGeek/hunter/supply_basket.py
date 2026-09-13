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


def requirements(world, rules, policy):
    result = []
    for unit in world.ours.values():
        if not unit.alive or unit.level not in (1, 2):
            continue
        if not procurement.upgrade_allowed(world, unit, policy, rules):
            continue
        prefix = ('Weapon' if unit.kind in WEAPONS else 'Wall' if unit.kind == 'wall'
                  else 'Station' if unit.kind == 'station' else None)
        if not prefix:
            continue
        rank = {'Weapon':1, 'Wall':2, 'Station':3}[prefix]
        for level in range(unit.level, 3):
            result.append(dict(unit=unit, level=level, name=f'{prefix}UpgradeVoucher{level}', rank=rank))
    return sorted(result, key=lambda r:(r['rank'], r['level'], r['unit'].id))


def basket(world, actor, rules, policy, deadline, *, cash=None):
    """Match every owned tier once, then fund as much useful stock as possible."""
    roster = world.night_roster
    carriers = {i:world.ours[i] for i in (roster.w, roster.p) if i in world.ours
                and world.ours[i].alive and world.ours[i].backpack is not None}
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
        else:
            unfilled.append(req)
    costs = [r.gold for k in WEAPONS if (r:=rules.build_rule(world,k)) is not None]
    reserve = (min(costs,default=0)*max(0,rules.weapon_limit-len(world.weapons))
               + getattr(world,'treasure_reserved_gold',0))
    primary = getattr(world,'upgrade_checkout_actor',actor.id)==actor.id
    worker = carriers.get(roster.w)
    if actor.id == roster.p and worker and world.shop.get('WallFixer',0)>0:
        # P cannot carry W's maintenance supplies. Preserve the small amount
        # W still needs for its own two repair packs, not an arbitrary gold floor.
        reserve += max(0,2-worker.inventory['WallFixer'])*world.shop['WallFixer']
    available = max(0, (world.gold or 0)-reserve) if cash is None else max(0,cash-reserve)
    space = actor.capacity-len(actor.backpack)
    prepaid = set(covered)
    planned = []
    for req in unfilled:
        if not primary:break
        if space <= 0 or time.monotonic() >= deadline:
            break
        uid = req['unit'].id
        price = world.shop.get(req['name'],0)
        predecessor = req['level'] == req['unit'].level or (uid,req['level']-1) in covered
        if not predecessor or reachable(actor.id,req) is None or not 0 < price <= available:
            continue
        # Keep an actor's successive tiers together whenever it can carry them.
        # Vouchers already in another backpack are reserved, never transferred.
        planned.append(req); covered.add((uid,req['level']))
        available -= price; space -= 1
    walls = [u for u in world.ours.values() if u.alive and u.kind=='wall']
    stock = []
    if actor.id == roster.w and walls:
        damaged = sum(u.health*10 < rules.max_health.get('wall',{}).get(u.level,u.health)*3 for u in walls)
        stock.append(('WallFixer', max(2, damaged+(len(walls)+3)//4)))
    if primary or actor.id==roster.w:stock.append(('Medicine', 2))
    # Night actions and bag space bound useful explosive reserves. The budget
    # may buy several in one action rather than stopping at the old one-item cap.
    if primary:stock.append(('Bomb', 60))
    for name, target in stock:
        price = world.shop.get(name,0)
        if price <= 0: continue
        count = min(space, available//price, max(0,target-actor.inventory[name]))
        if count:
            planned.extend(dict(name=name,rank=4,level=0,unit=None) for _ in range(count))
            available -= count*price;space -= count
    return dict(held=held, planned=planned, reserve=reserve, unspent=available,
                prepaid=prepaid, stock_targets=stock)


def use_tour(world, actor, entries, start, home, deadline, fields=None):
    """Cost of actually applying selected tiers in order, ending at duty."""
    view = copy(world)
    view.occupied = world.occupied-{actor.pos}
    remaining = [e for e in entries if e['unit'] is not None]
    fields = {} if fields is None else fields
    point, total = start, 0
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
        total += length+1
        remaining.pop(index)
    if point not in home:return None
    return total+home[point]


def quote(world, actor, clock, rules, policy, deadline, *, home, tail=None, end=None,
          cash=None, sale_stock=None, margin=None):
    """Fit useful purchases, skipping detours that would suppress the whole trip."""
    if actor.backpack is None or actor.capacity is None or not home:return None
    data = basket(world,actor,rules,policy,deadline,cash=cash)
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
        # Do not fill the last available checkout turns with upgrades and
        # then leave hundreds unused because even one Bomb purchase no longer
        # fits. Reserve one action for each affordable missing stock type.
        pending_stock = 0
        if trial and actor.capacity-len(actor.backpack)>len(selected)+1:
            left_cash=budget-sum(world.shop[e['name']] for e in selected+[entry])
            pending_stock=sum(name not in trial['orders'] and actor.inventory[name]<target
                              and 0<world.shop.get(name,0)<=left_cash
                              for name,target in data['stock_targets'])
        if trial and trial['fits'] and trial['required']+margin+pending_stock<=clock.until_night:
            selected.append(entry);best=trial
            if unit:covered.add((unit.id,entry['level']))
    # Reallocate money released by skipped upgrade detours to useful personal
    # stock. Consumables add checkout actions, not another delivery circuit.
    available = data['unspent']+sum(world.shop[e['name']] for e in planned)-sum(world.shop[e['name']] for e in selected)
    counts = Counter(e['name'] for e in selected)
    for name,target in data['stock_targets']:
        if time.monotonic() >= deadline:break
        price=world.shop.get(name,0)
        if price<=0:continue
        count=min(actor.capacity-len(actor.backpack)-len(selected),available//price,
                  max(0,target-actor.inventory[name]-counts[name]))
        if count<=0:continue
        entries=[dict(name=name,rank=4,level=0,unit=None) for _ in range(count)]
        trial=attempt(selected+entries)
        if trial and trial['fits']:
            selected+=entries;best=trial;available-=count*price;counts[name]+=count
    return best
