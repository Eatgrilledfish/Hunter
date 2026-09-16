"""Exterior worker spends observed gold on reachable defence during the night.

No door is removed and no guard is released. A closed ring can still be
upgraded from outside; a weapon requires a real, currently open safe path.
"""
from copy import copy
import time

from .arbitration import Candidate
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import pos_json
from . import supply_basket
from .rules import station_rings
from .medical import needs_treatment


def propose(world, clock, rules, policy, actor, blocked, deadline):
    if (clock.phases != {'night'} or not (policy.upgrade_commitment_enabled or policy.medical_supply_enabled)
            or actor.backpack is None or actor.capacity is None or world.gold is None):
        return None, {}
    treatment_needed=policy.medical_supply_enabled and needs_treatment(world,actor,clock)
    if treatment_needed and actor.inventory['Medicine']:
        return Candidate(actor.id,dict(action='use',name='Medicine'),1050,'treat persistent exterior injury'),dict(
            actor=actor.id,stage='NIGHT_TREATMENT',item='Medicine',independent=True)
    view = copy(world)
    view.occupied = set(blocked)
    view.upgrade_checkout_actor = actor.id
    reach = distance_field(view, {actor.pos}, actor.pos, deadline)
    shops = interaction_cells(view, world.zones.get('weaponShop',()), actor.pos) & reach.keys()
    remaining = min(130 - (world.round-o) % 130 for o in clock.offsets)
    blue,yellow=station_rings(world.task_side_plan['anchor'])
    interior=blue|yellow|world.stations[0].cells
    outside={(x,y) for x in range(world.width) for y in range(world.height)
             if (x,y) not in interior and (x,y) not in blocked}
    home=distance_field(view,outside,actor.pos,deadline)
    data = supply_basket.basket(view, actor, rules, policy, deadline)
    if data is None or time.monotonic() >= deadline:
        return None, {'stage':'NIGHT_SUPPLY_BUDGET'}
    fields = {}
    def field(unit):
        if unit.id not in fields:
            fields[unit.id] = distance_field(view, interaction_cells(view, [unit.pos], actor.pos), actor.pos, deadline)
        return fields[unit.id]
    def offer(command, stage, **details):
        return Candidate(actor.id, command, 1050, 'night personal supply: '+stage), dict(
            actor=actor.id, stage=stage, independent=True, **details)
    def move(route):
        length = route.get(actor.pos)
        steps = sorted(p for p in neighbours(actor.pos) if route.get(p,float('inf')) < (length or 0))
        return dict(action='move',targetPos=[pos_json(steps[0])]) if steps else None
    def personal_dose():
        if (actor.inventory['Medicine'] or len(actor.backpack)>=actor.capacity
                or not 0<world.shop.get('Medicine',0)<=world.gold-data['reserve']):
            return None
        medical_view = view
        medical_reach = reach
        medical_shops = shops
        if treatment_needed and not world.near_zone(actor.pos,'weaponShop'):
            # A treatment route must not repeatedly skim the attack boundary
            # while the patient cannot afford pursuit. One tile is a planning
            # precaution, not an assumed robot movement rule.
            from .robot_threats import active
            medical_view = copy(view)
            medical_view.occupied = set(view.occupied)
            for robot in active(world):
                if robot.attack_range is None or robot.attack_power is None:return None
                if robot.attack_power <= 0:continue
                radius = robot.attack_range + 1
                medical_view.occupied.update((x,y)
                    for x in range(max(0,robot.pos[0]-radius),min(world.width,robot.pos[0]+radius+1))
                    for y in range(max(0,robot.pos[1]-radius),min(world.height,robot.pos[1]+radius+1)))
            medical_view.occupied.discard(actor.pos)
            medical_reach = distance_field(medical_view,{actor.pos},actor.pos,deadline)
            medical_shops = interaction_cells(medical_view,world.zones.get('weaponShop',()),actor.pos) & medical_reach.keys()
        choices=sorted((medical_reach[p],p) for p in medical_shops if medical_reach[p]+1+int(treatment_needed)<=remaining)
        if not choices:return None
        _,shop=choices[0]
        command=(dict(action='buy',name='Medicine',num=1) if actor.pos==shop else
                 move(distance_field(medical_view,{shop},actor.pos,deadline)))
        if command and time.monotonic()<deadline:
            return offer(command,'NIGHT_UPGRADE_BUY',item='Medicine',quantity=1,shop=shop)
        return None
    if treatment_needed:
        treatment=personal_dose()
        if treatment:return treatment
    if not policy.upgrade_commitment_enabled:return None,{}
    held = [r for r in data['held'] if not r.get('pending') and r['unit'] is not None and r['level']==r['unit'].level
            and actor.pos in field(r['unit'])]
    held.sort(key=lambda r:(field(r['unit'])[actor.pos] != 0, r['rank'], field(r['unit'])[actor.pos],r['unit'].id))
    # Actually usable stock wins immediately; elsewhere finish checkout before
    # delivery so consecutive tiers do not require separate long trips.
    if held and (field(held[0]['unit'])[actor.pos] == 0 or not world.near_zone(actor.pos,'weaponShop')):
        entry = held[0]; route = field(entry['unit'])
        command = (dict(action='use',name=entry['name'],targetPos=[pos_json(entry['unit'].pos)])
                   if route[actor.pos]==0 else move(route))
        if command and time.monotonic()<deadline:
            return offer(command,'NIGHT_UPGRADE_DELIVER',target=entry['unit'].id,item=entry['name'])
    planned = [r for r in data['planned'] if r['unit'] is not None]
    # Reserve one dose personally, without turning an exterior trip into a
    # purchase of the guards' repair packs or an unlimited explosive stockpile.
    from .wall_policy import investment_fund
    wants_medicine = policy.medical_stock_enabled and not actor.inventory['Medicine'] and world.near_zone(actor.pos,'weaponShop')
    medicine = wants_medicine and 0 < world.shop.get('Medicine',0) <= world.gold-max(data['reserve'],investment_fund(world)[0])
    options=[]
    if actor.capacity > len(actor.backpack):
        for entry in planned:
            route=field(entry['unit'])
            for shop in shops & route.keys():
                # At least this purchase and actual application fit tonight.
                if reach[shop]+route[shop]+2 <= remaining:
                    options.append((1+entry['rank'],reach[shop]+route[shop],shop,entry['name'],1))
    for _,_,shop,name,_ in sorted(set(options)):
        if time.monotonic()>=deadline:break
        route=distance_field(view,{shop},actor.pos,deadline)
        count=1
        if name!='Medicine':
            entries=[r for r in planned if r['name']==name and shop in field(r['unit'])]
            # Reserve the complete carried-and-new use tour plus an actual
            # exterior exit. One nearby target alone cannot justify a bulk buy.
            while entries:
                use_steps=supply_basket.use_tour(view,actor,data['held']+entries,shop,home,deadline)
                if use_steps is not None and reach[shop]+1+use_steps+policy.return_buffer<=remaining:
                    break
                entries.pop()
            count=len(entries)
        count=min(count,actor.capacity-len(actor.backpack),(world.gold-data['reserve'])//world.shop[name])
        command=(dict(action='buy',name=name,num=count) if actor.pos==shop else move(route))
        if count>0 and command and time.monotonic()<deadline:
            return offer(command,'NIGHT_UPGRADE_BUY',item=name,quantity=count,shop=shop,
                         delivery_steps=reach[shop]+1+use_steps+policy.return_buffer,
                         night_remaining=remaining)
    if held and time.monotonic()<deadline:
        entry=held[0];command=move(field(entry['unit']))
        if command:return offer(command,'NIGHT_UPGRADE_DELIVER',target=entry['unit'].id,item=entry['name'])
    if time.monotonic()<deadline and clock.day is not None and clock.day<10:
        # Closed walls make immediate gun delivery impossible. The existing
        # planner can prepay a concrete current-tier target for the next dawn,
        # while all movements still use the actual closed topology.
        from .night_procurement import prepare
        gate=world.task_side_plan['gate']
        gate_ends={p for p in neighbours(gate) if p not in interior and p in reach}
        gate_home=distance_field(view,gate_ends,actor.pos,deadline)
        future=prepare(world,clock,rules,policy,actor,reach,gate_home,blocked,remaining,deadline)
        if future.candidate is not None:
            future.candidate.utility=1050
            return future.candidate,dict(actor=actor.id,stage='NIGHT_DAWN_PREBUY',
                                          independent=True,delivery=future.diagnostic)
        if any(r['actor']==actor.id for r in future.assignments):
            # Prepaid stock does not occupy the rest of the night. Continue
            # exterior work until the observed gate return consumes the slack.
            if remaining <= gate_home.get(actor.pos, remaining)+policy.return_buffer+2:
                command=move(gate_home)
                if command:return offer(command,'NIGHT_WAIT_DAWN_DELIVERY')
                if actor.pos in gate_ends:
                    return None,dict(actor=actor.id,stage='NIGHT_WAIT_DAWN_DELIVERY',hold=True,
                                     reason='personal prepaid vouchers await observed daytime gate opening')
    # A healthy reserve dose must not consume the last ten coins needed for
    # a verified next-dawn gun upgrade. Immediate low-HP treatment is owned
    # by triage; stock this dose once both upgrade routes have been considered.
    if medicine and len(actor.backpack)<actor.capacity and time.monotonic()<deadline:
        dose=personal_dose()
        if dose:return dose
    # Sell owned surplus as soon as a useful upgrade/dose lacks funds. The
    # next frame must show actual gold before any resulting purchase.
    from .wall_policy import priority_units
    priority_ids={u.id for u in priority_units(world)}
    prices = [world.shop[r['name']] for r in supply_basket.requirements(view,rules,policy)
              if r['unit'].id in priority_ids and r['level']==r['unit'].level and world.shop.get(r['name'],0)>0]
    if wants_medicine and world.shop.get('Medicine',0)>0:prices.append(world.shop['Medicine'])
    stock={k:actor.inventory[k] for k in ('iron','copper','stone') if world.vendor.get(k,0)>0 and actor.inventory[k]}
    if 'stone' in stock:
        # Night material work already prepares the next full ring. Selling
        # against day one's front10 target would liquidate that same stock.
        targets=yellow if getattr(world,'staged_walls',False) else set(getattr(world,'wall_targets',()))
        missing=len(targets-{u.pos for u in world.ours.values() if u.alive and u.kind=='wall'})
        worker=world.ours.get(world.night_roster.w)
        worker_stock=worker.inventory['stone'] if worker and worker.backpack is not None else 0
        rule=rules.build_rule(world,'wall')
        stone_cost=rule.items.get('stone',0) if rule else 0
        reserve=max(0,missing*stone_cost-worker_stock,stone_cost-worker_stock if clock.day<10 else 0)
        stock['stone']=max(0,stock['stone']-reserve)
        if not stock['stone']:stock.pop('stone')
    vendors=interaction_cells(view,world.zones.get('vendor',()),actor.pos)&reach.keys()
    if clock.day>=10:
        vendors={p for p in vendors if reach[p]+1<=remaining}
    value=sum(n*world.vendor[k] for k,n in stock.items())
    useful_sale = bool(prices and (any(world.gold < p <= world.gold+value for p in prices)
                                  or value>=min(prices)))
    if (useful_sale or len(actor.backpack)>=actor.capacity) and stock and vendors and time.monotonic()<deadline:
        shop=min(vendors,key=lambda p:(reach[p],p))
        name=max(stock,key=lambda k:(stock[k]*world.vendor[k],k))
        command=(dict(action='sell',name=name,num=stock[name]) if actor.pos==shop else
                 move(distance_field(view,{shop},actor.pos,deadline)))
        if command and time.monotonic()<deadline:
            return offer(command,'NIGHT_CASHOUT',item=name,quoted_stock_value=value)
    return None, {'actor':actor.id,'stage':'NIGHT_SUPPLY_UNAVAILABLE','reason':'no currently reachable funded checkout'}


def deliver_after_dawn(world, actor, rules, policy, deadline):
    """Use only personally observed vouchers through today's real topology."""
    from . import procurement
    targets,_,_,_=procurement.upgrade_demand(world,policy,rules=rules)
    choices=[]
    for entry in targets.values():
        if not actor.inventory[entry['name']]:continue
        target=entry['unit']
        route=distance_field(world,interaction_cells(world,[target.pos],actor.pos),actor.pos,deadline)
        if actor.pos not in route:continue
        command=(dict(action='use',name=entry['name'],targetPos=[pos_json(target.pos)])
                 if route[actor.pos]==0 else None)
        if command is None:
            steps=sorted(p for p in neighbours(actor.pos) if route.get(p,float('inf'))<route[actor.pos])
            if steps:command=dict(action='move',targetPos=[pos_json(steps[0])])
        if command:choices.append((entry['rank'],route[actor.pos],target.id,command))
        if time.monotonic()>=deadline:return None
    return min(choices,key=lambda r:r[:3])[3] if choices else None
