"""A free guard completes a funded shopping basket before returning to upgrade."""
from copy import copy
import time

from . import defence_duties, supply_basket
from .arbitration import Candidate
from .day_schedule import weighted_field, DaySchedule
from .navigation import distance_field, interaction_cells
from .protocol import pos_json


def prepare(market, world, clock, rules, policy, guidance, jobs, excluded, deadline):
    roster = world.night_roster
    # Basket ordering enforces priorities while permitting prepaid future tiers
    # and useful night stock after the affordable upgrades have been covered.
    world.upgrade_priority_pending = False
    world.upgrade_priority_items = set()
    market.diagnostic = {'stage':'upgrade_checkout','blocked':'no_feasible_basket'}
    eligible = [world.ours[i] for i in (roster.w,roster.p) if i in world.ours
                and i not in excluded and world.ours[i].alive and world.ours[i].backpack is not None
                and i not in guidance.recovery_actions and i not in getattr(world,'return_recovery_actions',{})]
    market.diagnostic['quotes'] = {}
    market.diagnostic['excluded'] = sorted(set(excluded)&{roster.w,roster.p})
    if not eligible:market.diagnostic['blocked']='no_eligible_guard'
    proposals = []
    for actor in eligible:
        if time.monotonic() >= deadline:break
        view = copy(world)
        if actor.id == roster.w:
            view.occupied = world.occupied | {world.task_side_plan['w']}
        home = distance_field(view,defence_duties.stands(world,actor.id),actor.pos,deadline)
        if actor.id in market.pending:
            market.diagnostic['blocked']='purchase_receipt_pending'
            continue
        stock = {k:actor.inventory[k] for k in ('stone','iron','copper')
                 if actor.kind=='pioneer' and actor.inventory[k] and world.vendor.get(k,0)>0}
        emergency = [u for u in world.stations if u.id in getattr(world,'critical_base_ids',()) and u.level in (1,2)]
        if any(actor.inventory[f'StationUpgradeVoucher{u.level}'] or
               0 < world.shop.get(f'StationUpgradeVoucher{u.level}',0) <= (world.gold or 0) for u in emergency):
            stock = {}  # An already funded rescue must not detour to sell ore.
        trip = supply_basket.quote(view,actor,clock,rules,policy,deadline,home=home,sale_stock=stock)
        if time.monotonic() >= deadline:
            market.diagnostic['blocked']='planning_budget_exhausted'
            break  # An unfinished quote is not evidence that shopping cannot fit.
        if trip is None:
            # Paid stock remains useful when a whole shopping/use tour cannot
            # be quoted (including an unknown/full purchasing capacity). Prove
            # one observed-level delivery below, independent of new purchases.
            market.diagnostic['blocked']='no_complete_quote_check_paid_delivery'
            trip=dict(held=[r for r in supply_basket.requirements(world,rules,policy)
                            if not r.get('pending') and r['level']==r['unit'].level
                            and actor.inventory[r['name']]],orders={},required=None,fits=False)
        market.diagnostic['quotes'][actor.id]=dict(cash=world.gold,orders=dict(trip['orders']),
            required=trip['required'],left=clock.until_night,fits=trip['fits'],
            reserve=trip.get('reserve'),reason='quoted' if trip['fits'] else 'route_or_deadline')
        choices,stage,item,num = [],'upgrade_return',None,0
        if stock and trip['fits'] and not trip.get('delivery_only'):
            item=max(stock,key=lambda k:stock[k]*world.vendor[k]);num=stock[item]
            choices=([Candidate(actor.id,dict(action='sell',name=item,num=num),240,'sell only personally held pioneer ore')]
                     if world.near_zone(actor.pos,'vendor') else DaySchedule.moves(actor,trip['sale'],'sell actual personal ore before checkout'))
            stage='personal_sale'
        elif trip['orders'] and trip['fits'] and world.sunset_buyer in (None,actor.id):
            item,num=next(iter(trip['orders'].items()))
            choices=([Candidate(actor.id,dict(action='buy',name=item,num=num),240,
                        'buy complete upgrade chain and night stock with observed cash',gold_reserve=trip['reserve'])]
                     if world.near_zone(actor.pos,'weaponShop') else DaySchedule.moves(actor,trip['checkout'],'free guard goes directly to weapon shop'))
            stage='upgrade_procure'
        else:
            ready=[r for r in trip['held'] if not r.get('pending') and r['level']==r['unit'].level]
            targets=[]
            from .wall_pressure import priority
            for req in ready:
                use=interaction_cells(view,[req['unit'].pos],actor.pos)
                route=weighted_field(view,{p:home[p]+1 for p in use if p in home},actor,deadline)
                if route and route.get(actor.pos,float('inf'))+policy.return_buffer <= clock.until_night:
                    targets.append((req['rank'],priority(world,req['unit']),actor.pos not in use,route[actor.pos],req['unit'].id,req,route))
            if targets:
                # The quoted tour applies nearby coupons on the way home.
                # Do not pass an equally ranked, already adjacent target just
                # because a different target is closer to the final duty stand.
                _,_,_,_,_,req,route=min(targets,key=lambda t:t[:5]);item=req['name'];num=1
                choices=([Candidate(actor.id,dict(action='use',name=item,targetPos=[pos_json(req['unit'].pos)]),240,
                                    'apply held coupon at its observed current level')]
                         if actor.pos in interaction_cells(view,[req['unit'].pos],actor.pos)
                         else DaySchedule.moves(actor,route,'deliver personal upgrade chain before night'))
                stage='upgrade_deliver'
            elif (getattr(world,'summon_use_remaining',0) and not getattr(world,'critical_base_ids',())
                  and any(n and k.endswith('SummonOrder') for k,n in actor.inventory.items())
                  and home.get(actor.pos,float('inf'))+policy.return_buffer+1 < clock.until_night):
                item=next(k for k,n in actor.inventory.items() if n and k.endswith('SummonOrder'))
                choices=[Candidate(actor.id,dict(action='use',name=item),240,
                                   'use owned next-wave order within observed daily quota before returning')]
                stage='pressure_use';num=1
            elif actor.id in market.upgrade_travellers or market.upgrade_owner==actor.id:
                choices=DaySchedule.moves(actor,home,'complete shopping return before night')
        preview=copy(guidance)
        preview.return_routes={i:r for i,r in guidance.return_routes.items() if i!=actor.id}
        proposed=bool(choices)
        choices=[c for c in choices if preview.permit(c)]
        if not choices:
            reason='guidance_denied' if proposed else 'no_executable_order_or_delivery'
            market.diagnostic['quotes'][actor.id]['reason']=reason
            market.diagnostic['blocked']=reason
            continue
        for c in choices:c.utility=240
        priority=(actor.id!=market.upgrade_owner if market.upgrade_owner else False,
                  not world.near_zone(actor.pos,'weaponShop'), bool(jobs.get(actor.id)),
                  actor.id!=roster.p, trip['required'] if trip['required'] is not None else float('inf'),actor.id)
        proposals.append((priority,actor,choices,trip,stage,item,num))
    if not proposals:return []
    _,actor,choices,trip,stage,item,num=min(proposals,key=lambda p:p[0])
    world.sunset_actions[actor.id]=[c.command for c in choices]
    guidance.day_actions[actor.id]=list(world.sunset_actions[actor.id])
    guidance.funded_actions[actor.id]=list(world.sunset_actions[actor.id])
    # A worker daily itinerary may also exist. Serialise purchases only, not
    # the other guard's independent building, repairing or return movement.
    if world.sunset_buyer is None or choices[0].command['action']=='buy':
        world.sunset_buyer=actor.id
    if stage == 'upgrade_procure':
        # Only the selected, executable, return-feasible basket reserves cash.
        # Ore-sale forecasts and stale/unconfirmed orders are not commitments.
        cost = sum(world.shop[name]*count for name,count in trip['orders'].items())
        if cost + trip['reserve'] <= (world.gold or 0):
            world.checkout_cash_reserve = cost + trip['reserve']
    market.diagnostic.update(stage=stage,buyer=actor.id,item=item,num=num,
        basket=dict(trip['orders']),required=trip['required'],blocked=None,
        worker_busy=bool(jobs.get(roster.w)))
    return choices
