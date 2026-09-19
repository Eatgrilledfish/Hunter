"""Current-cash grants shared by purchases and the final bundle validator."""
from collections import Counter
import time
from .navigation import distance_field, interaction_cells
from . import defence_duties
from .protocol import fingerprint
from .day_schedule import weighted_field


def publish(world, clock, rules, policy, intelligence, deadline):
    world.funding_plan = []
    world.treasure_reserved_gold = 0
    roster = world.night_roster
    worker = world.ours.get(roster.w)
    world.work_rejections=[]
    if worker and worker.alive and worker.backpack is not None and len(world.weapons) == rules.weapon_limit:
        capacity=max(0,(worker.capacity or 0)-len(worker.backpack))
        from .guard_stock import requirements
        stock=requirements(world,worker,policy)
        due=world.round+clock.until_night
        if clock.phases=={'night'} and clock.day is not None and clock.day<10:
            due=min(o+clock.day*130+70 for o in clock.offsets)
        reach=distance_field(world,{worker.pos},worker.pos,deadline)
        home=distance_field(world,defence_duties.stands(world,worker.id),worker.pos,deadline) if getattr(world,'task_side_plan',None) else {}
        shops=interaction_cells(world,world.zones.get('weaponShop',()),worker.pos)
        walks=[reach[p]+home[p] for p in shops&reach.keys()&home.keys()]
        travel=min(walks) if walks and time.monotonic()<deadline else None
        action_count=sum(target>worker.inventory[name] and world.shop.get(name,0)>0 for name,target in stock)
        required=(travel+action_count+policy.return_buffer) if travel is not None else None
        if required is None or required>due-world.round:
            world.work_rejections.append(dict(owner=worker.id,purpose='night_stock',reason='shop_return_deadline',required=required))
            capacity=0
        for name, target in stock:
            price = world.shop.get(name,0)
            count = min(max(0,target-worker.inventory[name]),capacity)
            if price > 0 and count:
                capacity-=count
                from .repair_decision import purchase_floor
                for index in range(count):
                    owned=worker.inventory[name]+index
                    purpose=('night_attack' if name in {'DizzyWeapon','Bomb'} else
                             'night_buffer' if name=='WallFixer' and owned>=purchase_floor(world,policy)
                             else 'night_essential')
                    world.funding_plan.append(dict(owner=worker.id,purpose=purpose,
                        items={name:1},cost=price,stock_index=owned,
                        deadline=due,required_rounds=required,latest_departure=due-required,
                        deadline_source='next_night',expires=world.round))
    actor = world.ours.get(roster.p)
    plans = [] if getattr(intelligence,'plans_suspended',False) else intelligence.treasures or intelligence.preparations
    lists = {tuple(p['items']) for p in plans}
    if (actor and actor.backpack is not None and actor.capacity is not None
            and intelligence.treasure_complete and not intelligence.terminal and len(lists)==1
            and len(intelligence.attempts)<policy.treasure_attempt_limit):
        items = list(next(iter(lists)))
        needed = Counter(items)-actor.inventory
        valid = (not any(a.get('result')==3 and a['items']==sorted(items) for a in intelligence.attempts)
                 and all(world.shop.get(k,0)>0 for k in needed)
                 and len(actor.backpack)+sum(needed.values())<=actor.capacity)
        cost = sum(world.shop.get(k,0)*n for k,n in needed.items())
        if valid and cost and intelligence.treasure_spent+cost<=policy.treasure_gold_limit:
            start=distance_field(world,{actor.pos},actor.pos,deadline)
            from .task_schedule import home_cells
            home=distance_field(world,home_cells(world,actor),actor.pos,deadline)
            shops=interaction_cells(world,world.zones.get('weaponShop',()),actor.pos)
            trips=[start[p]+len(needed)+home[p]+policy.return_buffer for p in shops&start.keys()&home.keys()]
            # An unresolved location can still have a bounded supply tour.
            if trips and time.monotonic()<deadline and min(trips)<=70:
                limit=min((p.get('closing_round',1300) for p in plans),default=1300)
                if limit>=world.round+min(trips):
                    world.funding_plan.append(dict(owner=actor.id,purpose='treasure',items=dict(needed),cost=cost,
                        deadline=min(limit,world.round+max(clock.until_night,min(trips))),
                        deadline_source='safe_supply_window_strategy',expires=world.round,
                        required_rounds=min(trips)))
    from .wall_policy import purchase_units, upgrade_rank
    from .defence_duties import stands
    due=sorted(purchase_units(world),key=lambda u:(u.id not in getattr(world,'critical_base_ids',()),
        u.kind!='wall',upgrade_rank(world,u),u.level,u.id))
    if due and clock.phases=={'day'}:
        actors=[u for u in (worker,actor) if u and u.alive and u.backpack is not None and u.capacity is not None
                and (u.id==roster.w and not getattr(world,'wall_rebuild_plan',{})
                     or u.id==roster.p and not world.phase_task and not getattr(world,'six_task_priority',False)
                     and not intelligence.return_plan)]
        held=sum((u.inventory for u in world.movers if u.backpack is not None),Counter())
        routes={}
        for target in due:
            if time.monotonic()>=deadline:
                world.work_rejections.append(dict(reason='funding_planning_budget',target_id=target.id))
                break
            if not target.alive or target.level not in (1,2):continue
            from .procurement import upgrade_allowed
            if not upgrade_allowed(world,target,policy,rules):continue
            prefix='Wall' if target.kind=='wall' else 'Station' if target.kind=='station' else 'Weapon'
            name=f'{prefix}UpgradeVoucher{target.level}'
            if held[name]:held[name]-=1;continue
            price=world.shop.get(name,0)
            if price<=0:continue
            from .day_maintenance import owner
            reserved=owner(world,target)
            preferred=roster.w if target.kind=='wall' else roster.p
            for buyer in sorted(actors,key=lambda u:(u.id!=preferred,not world.near_zone(u.pos,'weaponShop'),u.id)):
                if reserved not in (None,buyer.id):continue
                allocated=sum(sum(r['items'].values()) for r in world.funding_plan if r['owner']==buyer.id)
                if len(buyer.backpack)+allocated>=buyer.capacity:continue
                if buyer.id not in routes:
                    start=distance_field(world,{buyer.pos},buyer.pos,deadline)
                    home=distance_field(world,stands(world,buyer.id),buyer.pos,deadline)
                    routes[buyer.id]=(start,home)
                start,home=routes[buyer.id]
                service=weighted_field(world,{p:home[p]+1 for p in interaction_cells(world,[target.pos],buyer.pos)
                                             if p in home},buyer,deadline) or {}
                shops=interaction_cells(world,world.zones.get('weaponShop',()),buyer.pos)
                trips=[start[p]+service[p]+1+policy.return_buffer+8 for p in shops&start.keys()&service.keys()]
                if time.monotonic()>=deadline:
                    world.work_rejections.append(dict(owner=buyer.id,target_id=target.id,reason='funding_planning_budget'))
                    break
                if not trips or min(trips)>clock.until_night:
                    world.work_rejections.append(dict(owner=buyer.id,target_id=target.id,reason='shop_service_return_deadline',required=min(trips) if trips else None))
                    continue
                world.funding_plan.append(dict(owner=buyer.id,purpose='wall_upgrade' if prefix=='Wall' else 'base_upgrade' if prefix=='Station' else 'weapon_upgrade',items={name:1},cost=price,
                    position=target.pos,target_id=target.id,level=target.level,
                    generation=getattr(world,'wall_service',{}).get(target.pos,{}).get('generation'),
                    deadline=world.round+clock.until_night,expires=world.round,required_rounds=min(trips),
                    latest_departure=world.round+clock.until_night-min(trips)))
                break
    plan=getattr(world,'wall_rebuild_plan',{})
    if (worker and worker.backpack is not None and plan.get('actor')==worker.id
            and plan.get('target_level',1)>1
            and plan.get('stage') in {'SUPPLY','UPGRADE'} and not worker.inventory['WallUpgradeVoucher1']):
        allocated=sum(sum(r['items'].values()) for r in world.funding_plan if r['owner']==worker.id)
        rule=rules.build_rule(world,'wall')
        stone_missing=max(0,(rule.items.get('stone',0) if rule else 0)-worker.inventory['stone']) if plan['stage']=='SUPPLY' else 0
        price=world.shop.get('WallUpgradeVoucher1',0)
        if price>0 and len(worker.backpack)+allocated+stone_missing<(worker.capacity or 0):
            world.funding_plan.append(dict(owner=worker.id,purpose='wall_rebuild',items={'WallUpgradeVoucher1':1},cost=price,
                deadline=world.round+clock.until_night,expires=world.round,
                last_progress_round=plan.get('last_progress_round'),deadline_source='reprice_each_frame'))
    allocate(world)
    link_works(world)


def link_works(world):
    """Tag each grant with the active procurement work consuming it (design §4.4).

    Matching is by owner plus item-name overlap with the work's quote, issued
    command or confirmed receipts. Unmatched rows remain provisional quotes.
    """
    works = getattr(world, 'procurement_works', ())
    if not works:
        return
    # Only committed trips hold their grants; PROPOSED quotes and SUSPENDED
    # temporary reservations stay provisional and may lapse (§4.4).
    active = [w for w in works if w.status in ('ACTIVE', 'WAIT_RECEIPT')]
    if not active:
        return
    for row in getattr(world, 'funding_plan', ()):
        if row.get('work_id') or not row.get('demand_id'):
            continue
        for work in active:
            if work.actor == row['owner'] and set(row['items']) & work.item_names():
                row['work_id'] = work.work_id
                work.grants.update({name: min(row.get('granted', 0), row['cost']) for name in row['items']})
                work.demands = tuple(dict.fromkeys(work.demands + (row['demand_id'],)))
                break


def completion_candidates(world, released, selected):
    """One bounded completion for demands the first ledger priced out (§4.4).

    funding.publish already proved each retained row's shop/return trip before
    creating it; the rows keep that feasibility evidence. Once the initial
    selection lapses provisional grants, a row whose owner is standing at the
    counter can issue its buy under the released ledger. Travelling re-quotes
    stay with the next frame's executors; no unverified movement is invented.
    """
    rows = getattr(world, 'funding_plan', ())
    if not rows:
        return []
    spent = sum(world.shop.get(c.command.get('name'), 0)*c.command.get('num', 1)
                for c in selected if c.command.get('action') == 'buy')
    out = []
    for row in rows:
        deficit = row.get('deficit', row['cost']-row.get('granted', 0))
        if deficit <= 0:
            continue
        actor = world.ours.get(row['owner'])
        if (not actor or not actor.alive or actor.backpack is None or actor.capacity is None
                or not world.near_zone(actor.pos, 'weaponShop')):
            continue
        name = next(iter(row['items']))
        price = world.shop.get(name, 0)
        num = min(row['items'][name], actor.capacity-len(actor.backpack))
        if price <= 0 or num <= 0:
            continue
        from .arbitration import Candidate
        candidate = Candidate(actor.id, {'action': 'buy', 'name': name, 'num': num}, 60,
                              'bounded funding completion for a cash-blocked retained demand')
        if permits_bundle(world, list(selected)+[candidate], spent+price*num, released=released):
            market = getattr(world, 'procurement_market', None)
            work = market.linkable_work(actor.id, name) if market else None
            if work is not None:
                # Register the offer with the ledger: if the command ships,
                # record_selected commits the same work like any other purchase.
                market.propose_step(world, work, [candidate], 'checkout')
            out.append(candidate)
            spent += price*num
    return out


def allocate(world):
    # Rescue precedes reserves; unaffordable optional purchases reserve zero,
    # so a 100-gold consumable never blocks a feasible 20-gold wall step.
    critical=getattr(world,'critical_base_ids',())
    # Keep two executable wall steps ahead of bulk stores on every checkout.
    # This is a spending priority, not a daily upgrade cap. Spare cash can
    # still finish all other walls after personal night supplies are funded.
    walls=[r for r in world.funding_plan if r['purpose'] in {'wall_upgrade','wall_rebuild'}][:2]
    def priority(row):
        if row.get('target_id') in critical:return 0
        if row['purpose']=='night_essential':return 1
        if row['purpose']=='treasure':return 2
        if any(row is r for r in walls):return 3
        if row['purpose']=='night_buffer':return 4
        if row['purpose']=='night_attack':return 5 if row.get('stock_index',0)==0 else 7
        return 6
    world.funding_plan.sort(key=lambda r:(priority(r),r.get('stock_index',0)))
    cash=max(0,world.gold or 0)
    for row in world.funding_plan:
        unit_cost=row['cost']//max(1,sum(row['items'].values()))
        if row['purpose']=='night_attack':
            grant=min(row['cost'],cash//unit_cost*unit_cost)
        elif row['purpose'].endswith('_upgrade'):
            grant=row['cost'] if cash>=row['cost'] else 0
        else:grant=min(cash,row['cost'])
        cash-=grant
        row.update(granted=grant,deficit=row['cost']-grant,
            demand_id=fingerprint([row['owner'],row['purpose'],row['items'],row.get('target_id'),row.get('generation'),row.get('stock_index')])[:16],
            stage='funded_await_purchase' if grant==row['cost'] else 'cash_deficit')
        if row['purpose']=='treasure':world.treasure_reserved_gold=grant
    worker=world.ours.get(world.night_roster.w)
    if worker and worker.backpack is not None:
        targets=dict(getattr(world,'guard_attack_targets',{}),Medicine=1,
            WallFixer=getattr(world,'caretaker_repair_target',3)+getattr(world,'day_repair_demand',0))
        world.guard_funding_report={name:dict(owned=worker.inventory[name],target=targets.get(name,1),
            missing=max(0,targets.get(name,1)-worker.inventory[name]),
            planned=sum(r['items'].get(name,0) for r in world.funding_plan if r['owner']==worker.id),
            funded=sum(min(r['items'].get(name,0),r['granted']//world.shop[name])
                       for r in world.funding_plan if r['owner']==worker.id) if world.shop.get(name,0)>0 else 0)
            for name in ('WallFixer','Medicine','DizzyWeapon','Bomb')}


def release_busy(world, excluded):
    """New task/treasure ownership supersedes unpaid building work only."""
    world.funding_plan=[r for r in world.funding_plan
        if not (r['purpose'].endswith('_upgrade') and r['owner'] in excluded)]
    allocate(world)
    link_works(world)


def reserve_for(world, purpose):
    return sum(r['granted'] for r in getattr(world,'funding_plan',()) if r['purpose']!=purpose)


def fulfilled(row, candidate, world):
    command=candidate.command
    return (candidate.actor==row['owner'] and command.get('action')=='buy'
            and command.get('name') in row['items'])


def permits_bundle(world, candidates, spent, released=()):
    rows=getattr(world,'funding_plan',())
    if not rows:
        return True
    # Only an observed critical-base rescue or personal emergency treatment
    # may explicitly borrow lower-priority task/stock grants.
    emergency=any(c.command.get('action')=='use' and c.command.get('name','').startswith('StationUpgradeVoucher')
                  for c in candidates) and bool(getattr(world,'critical_base_ids',()))
    emergency=emergency or any(c.command.get('action')=='buy' and c.command.get('name')=='Medicine'
        and c.actor in world.ours and world.ours[c.actor].health<=110 for c in candidates)
    reserve=0
    credits=Counter()
    for c in candidates:
        if c.command.get('action')=='buy':credits[c.actor,c.command['name']]+=c.command.get('num',1)
    for row in rows:
        if emergency:
            continue
        if released and row.get('demand_id') in released:
            continue  # Lapsed provisional grant (one-shot top-up pass only).
        credit=0
        for name,count in row['items'].items():
            used=min(count,credits[row['owner'],name])
            credit+=used*world.shop[name]
            credits[row['owner'],name]-=used
        reserve+=max(0,row['granted']-credit)
    return world.gold is not None and spent+reserve<=world.gold


def lapsable(world, selected):
    """Provisional grants the chosen bundle neither consumes nor a committed work holds.

    A grant linked to an ACTIVE/WAIT_RECEIPT/SUSPENDED procurement work is a
    continuing reservation and never lapses merely because this frame's legal
    step was a wait. Strategy floors (treasure) are protected separately via
    candidate reserves and stay out of this release set.
    """
    bought=Counter()
    for c in selected:
        if c.command.get('action')=='buy':
            bought[c.actor,c.command['name']]+=c.command.get('num',1)
    committed=getattr(world,'committed_work_ids',set())
    released=set()
    for row in getattr(world,'funding_plan',()):
        if row.get('granted',0)<=0 or row.get('purpose')=='treasure':
            continue
        if row.get('work_id') and row['work_id'] in committed:
            continue
        unpaid=sum(max(0,count-bought[row['owner'],name]) for name,count in row['items'].items())
        if unpaid and row.get('demand_id'):
            released.add(row['demand_id'])
    return released


def item_granted(world, actor, name, quantity=1):
    price=world.shop.get(name,0)
    return price>0 and sum(min(r['items'].get(name,0),r['granted']//price)
        for r in getattr(world,'funding_plan',()) if r['owner']==actor)>=quantity
