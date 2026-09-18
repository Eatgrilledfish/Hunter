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
    if worker and worker.backpack is not None and len(world.weapons) == rules.weapon_limit:
        capacity=max(0,(worker.capacity or 0)-len(worker.backpack))
        from .repair_decision import stock_target
        stock=[('WallFixer',stock_target(world,policy)),('Medicine',1)]
        if worker.health<220:stock.reverse()
        due=world.round+clock.until_night
        if clock.phases=={'night'} and clock.day is not None and clock.day<10:
            due=min(o+clock.day*130+70 for o in clock.offsets)
        for name, target in stock:
            price = world.shop.get(name,0)
            count = min(max(0,target-worker.inventory[name]),capacity)
            if price > 0 and count:
                capacity-=count
                world.funding_plan.append(dict(owner=worker.id,purpose='night_essential',
                    items={name:count},cost=price*count,deadline=due,
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
    from .wall_policy import daily_upgrade_targets
    from .defence_duties import stands
    due=daily_upgrade_targets(world)
    if due and clock.phases=={'day'}:
        actors=[u for u in (worker,actor) if u and u.alive and u.backpack is not None and u.capacity is not None
                and (u.id==roster.w or not world.phase_task and not getattr(world,'news_task_hold',False)
                     and not intelligence.treasures)]
        held=sum((u.inventory for u in world.movers if u.backpack is not None),Counter())
        routes={}
        for target in due:
            name=f'WallUpgradeVoucher{target.level}'
            if held[name]:held[name]-=1;continue
            price=world.shop.get(name,0)
            if price<=0:continue
            for buyer in sorted(actors,key=lambda u:(not world.near_zone(u.pos,'weaponShop'),u.id!=roster.w,u.id)):
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
                if time.monotonic()>=deadline or not trips or min(trips)>clock.until_night:continue
                world.funding_plan.append(dict(owner=buyer.id,purpose='wall_upgrade',items={name:1},cost=price,
                    position=target.pos,target_id=target.id,level=target.level,
                    generation=getattr(world,'wall_service',{}).get(target.pos,{}).get('generation'),
                    deadline=world.round+clock.until_night,expires=world.round,required_rounds=min(trips)))
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
    cash=max(0,world.gold or 0)
    for row in world.funding_plan:
        grant=min(cash,row['cost']);cash-=grant
        row.update(granted=grant,deficit=row['cost']-grant,
            demand_id=fingerprint([row['owner'],row['purpose'],row['items'],row.get('target_id'),row.get('generation')])[:16],
            stage='funded_await_purchase' if grant==row['cost'] else 'cash_deficit')
        if row['purpose']=='treasure':world.treasure_reserved_gold=grant


def reserve_for(world, purpose):
    return sum(r['granted'] for r in getattr(world,'funding_plan',()) if r['purpose']!=purpose)


def fulfilled(row, candidate, world):
    command=candidate.command
    return (candidate.actor==row['owner'] and command.get('action')=='buy'
            and command.get('name') in row['items'])


def permits_bundle(world, candidates, spent):
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
        credit=0
        for name,count in row['items'].items():
            used=min(count,credits[row['owner'],name])
            credit+=used*world.shop[name]
            credits[row['owner'],name]-=used
        reserve+=max(0,row['granted']-credit)
    return world.gold is not None and spent+reserve<=world.gold


def item_granted(world, actor, name, quantity=1):
    return sum(r['items'].get(name,0) for r in getattr(world,'funding_plan',())
               if r['owner']==actor and r['granted']>=r['cost'])>=quantity
