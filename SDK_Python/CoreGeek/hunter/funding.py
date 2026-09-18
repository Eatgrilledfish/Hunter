"""Current-cash grants shared by purchases and the final bundle validator."""
from collections import Counter
import time
from .navigation import distance_field, interaction_cells
from . import defence_duties
from .protocol import fingerprint


def publish(world, clock, rules, policy, intelligence, deadline):
    world.funding_plan = []
    world.treasure_reserved_gold = 0
    roster = world.night_roster
    worker = world.ours.get(roster.w)
    if worker and worker.backpack is not None and len(world.weapons) == rules.weapon_limit:
        capacity=max(0,(worker.capacity or 0)-len(worker.backpack))
        stock=[('WallFixer',policy.caretaker_repair_target),('Medicine',1)]
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
    plan=getattr(world,'wall_rebuild_plan',{})
    if (worker and worker.backpack is not None and plan.get('actor')==worker.id
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
            demand_id=fingerprint([row['owner'],row['purpose'],row['items']])[:16],
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
    for row in rows:
        if emergency:
            continue
        credit=sum(min(c.command.get('num',1),row['items'][c.command['name']])*world.shop[c.command['name']]
                   for c in candidates if fulfilled(row,c,world))
        reserve+=max(0,row['granted']-credit)
    return world.gold is not None and spent+reserve<=world.gold


def item_granted(world, actor, name, quantity=1):
    return any(r['owner']==actor and r['items'].get(name,0)>=quantity
               and r['granted']>=world.shop.get(name,0)*quantity for r in getattr(world,'funding_plan',()))
