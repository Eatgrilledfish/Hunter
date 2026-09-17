"""Repair, harvest, sell, buy, close the perimeter, then apply personal vouchers.

The end-of-day circuit is reserved before admitting another repair or harvest.
Income is only a timing estimate until the next observed sale receipt.
"""
from copy import copy
from dataclasses import dataclass, field
from math import ceil
import time

from . import defence_duties, procurement
from .arbitration import Candidate
from .day_schedule import DaySchedule, weighted_field
from .day_division import DayDivision
from .navigation import distance_field, interaction_cells
from .protocol import MINERALS, pos_json, distance
from .rear_open import enabled as rear_enabled
from .rules import station_rings


@dataclass
class CaretakerDay:
    day: int | None = None
    identity: str | None = None
    phase: str = 'harvest'
    last_required: int | None = None
    use_budget: int = 0
    construction_only: bool = False
    front_rebuild_pending: bool = False
    diagnostic: dict = field(default_factory=dict)

    @staticmethod
    def adjacent_paid(world, actor, rules, policy):
        from .wall_policy import upgrade_rank, priority_units
        from .wall_service import service_key, use_permitted
        choices=[]
        for target in world.ours.values():
            if (not target.alive or target.level not in (1,2)
                    or not procurement.upgrade_allowed(world,target,policy,rules)
                    or max(abs(actor.pos[0]-target.pos[0]),abs(actor.pos[1]-target.pos[1]))>1):
                continue
            prefix=('Weapon' if target in world.weapons else 'Wall' if target.kind=='wall'
                    else 'Station' if target.kind=='station' and (target.id in getattr(world,'base_restore_ids',())
                        or target.id in {u.id for u in priority_units(world)}) else None)
            name=f'{prefix}UpgradeVoucher{target.level}'
            if not prefix or not actor.inventory[name]:continue
            candidate=Candidate(actor.id,dict(action='use',name=name,targetPos=[pos_json(target.pos)]),240,
                'apply paid adjacent voucher from the observed duty stand')
            if use_permitted(world,candidate):
                choices.append((upgrade_rank(world,target),service_key(world,target),candidate))
        return [min(choices,key=lambda t:t[:2])[2]] if choices else []

    def wall_tour(self, world, actor, missing, ring, rule, deadline):
        """Reserve work funded by personal stone, plus the next harvest action.

        The remaining wall goal is unchanged. Unfunded walls do not consume
        the entire day's clock or prevent collecting their first material.
        """
        cost = rule.items.get('stone',0) if rule else 0
        if not cost:
            return set(), None
        planned_stock = actor.inventory['stone'] + (self.phase == 'harvest' and len(actor.backpack) < actor.capacity)
        count = min(len(missing), planned_stock // cost)
        from .day_access import gate as access_gate
        gate = None if getattr(world,'wall_stage',None) == 'front10' else access_gate(world)
        ordered = sorted(missing-{gate},key=lambda p:(max(abs(actor.pos[0]-p[0]),abs(actor.pos[1]-p[1])),p))
        subset = set(ordered[:count])
        if count == len(missing):
            subset = set(missing)
        if not subset:
            return subset, None
        topology = copy(world)
        topology.occupied = (world.occupied - {u.pos for u in world.movers}) | {world.task_side_plan['w']}
        tour = DayDivision(gate=gate).tour(topology,actor,subset,ring,deadline,world.occupied)
        # A teammate may occupy the nearest missing wall. That blocks this
        # subset, not the material trip for every other unfinished wall.
        # Keep the same funded work count and validate an alternative tour.
        if tour is None and count < len(missing):
            for offset in range(1,len(ordered)):
                if time.monotonic() >= deadline:break
                alternative = set((ordered[offset:]+ordered[:offset])[:count])
                candidate = DayDivision(gate=gate).tour(
                    topology,actor,alternative,ring,deadline,world.occupied)
                if candidate is not None:
                    return alternative,candidate
        return subset,tour

    def prepare(self, world, clock, rules, policy, guidance, jobs, excluded, deadline):
        self.diagnostic = {}
        actor = world.ours.get(world.night_roster.w)
        if (not actor or not actor.alive or actor.backpack is None or actor.capacity is None
                or len(world.weapons) != rules.weapon_limit or actor.id in excluded):
            return None
        walls = {u.pos for u in world.ours.values() if u.alive and u.kind == 'wall'}
        if (self.day, self.identity) != (clock.day, actor.id):
            self.day, self.identity, self.phase = clock.day, actor.id, 'harvest'
            self.last_required = None
            self.use_budget = 0
            self.construction_only = False
            self.front_rebuild_pending = bool(actor.inventory['stone'] and clock.day > 1
                and set(getattr(world,'monster_front_walls',()))-walls)
        world.caretaker_day_actor = actor.id
        if getattr(world,'worker_checkout_wait',False) and self.phase in {'close','home'}:
            self.phase = 'harvest'
        world.caretaker_day_phase = self.phase
        # Generic voucher proposals must not create a second, conflicting
        # worker itinerary. The free-pioneer dispatcher uses its own view.
        world.upgrade_dispatch_ids = set()
        job = jobs.get(actor.id, {})
        if job and job.get('name') != 'wall':
            return None
        from .day_access import gate as access_gate
        plan = dict(world.task_side_plan,gate=(None if getattr(world,'wall_stage',None) == 'front10'
                                              else access_gate(world)))
        _, yellow = station_rings(plan['anchor'])
        missing = set(world.wall_targets or ()) - walls - set(getattr(world,'helper_wall_targets',()))
        rule = rules.build_rule(world, 'wall')
        stone = len(missing) * rule.items.get('stone', 0) if rule else 0
        from .wall_policy import monster_face
        door=next((u for u in world.ours.values() if u.alive and u.kind=='wall' and u.pos==plan['gate']),None)
        next_exit_stone=(rule.items.get('stone',0) if rule and policy.night_foraging_enabled
            and clock.day is not None and clock.day<10 and set(world.wall_targets or ())==yellow
            and plan['gate'] not in monster_face(world,plan['anchor'])
            and (plan['gate'] in missing or door and door.level==1) else 0)
        retained_stone = stone
        if (clock.day is not None and clock.day<10 and set(world.wall_targets or ())==yellow
                and missing<={plan['gate']} and len(world.weapons)==rules.weapon_limit
                and all(u.level==3 for u in world.weapons) and rule):
            # The daily door consumes personal stone again tomorrow. Keep
            # already collected surplus instead of selling it and travelling
            # back to a distant deposit for one stone on each following day.
            # Current upgrade funding wins over this future material reserve.
            from .wall_policy import investment_fund
            price=world.vendor.get('stone',0)
            other_income=sum(actor.inventory[k]*world.vendor.get(k,0)
                             for k in MINERALS if k!='stone')
            cash=max(0,(world.gold or 0)-getattr(world,'treasure_reserved_gold',0))
            shortfall=max(0,investment_fund(world)[0]-cash-other_income)
            sell_needed=ceil(shortfall/price) if price>0 else 0
            future=(10-clock.day)*rule.items.get('stone',0)
            retained_stone=max(stone,min(stone+future,actor.inventory['stone']-sell_needed))
        stock = {k:max(0, actor.inventory[k] - (retained_stone if k == 'stone' else 0))
                 for k in MINERALS if world.vendor.get(k, 0) > 0}
        stock = {k:n for k,n in stock.items() if n}
        # A finished batch is not a finished wall project. Reopen material work
        # only while gaps assigned to this worker still lack personal stone;
        # the complete route checks below decide whether another trip fits.
        if missing and actor.inventory['stone'] < stone and self.phase in {'close','home'}:
            self.phase = 'harvest'
            self.construction_only = True
        if self.construction_only and actor.inventory['stone'] >= stone:
            # A material commitment ends once its personal stock is observed.
            # Requote checkout before closure rather than locking the whole day.
            self.construction_only = False
            self.phase = 'harvest'
        # Daytime use tours may pass through either side of C. Restricting
        # their starting endpoint to the final front stand can hide a gun
        # behind P's occupied tile and omit its otherwise deliverable coupon.
        # The normal return planner still selects the front stand for night.
        home_cells = defence_duties.stands(world, actor.id)
        home = distance_field(world, home_cells, actor.pos, deadline)
        # The wall tour already contains walking, building, entry and closure.
        # Add the return uncertainty once; P's ingress has its own shared clock.
        margin = policy.return_buffer + defence_duties.seal_service_steps(world)
        self.diagnostic = dict(left=clock.until_night,missing_walls=len(missing),reserved_stone=stone)
        if (self.phase in {'close','home','use'} and home.get(actor.pos)==0
                and (not missing or not actor.inventory['stone']) and clock.until_night>0):
            immediate=self.adjacent_paid(world,actor,rules,policy)
            if immediate:
                return self.finish(world,guidance,jobs,actor,immediate,'use',
                    reason='already at duty: adjacent paid use needs no return buffer')
        if (rear_enabled(world) and rule and missing
                and (self.phase=='home' or self.phase=='close'
                     and clock.until_night<=home.get(actor.pos,float('inf'))+margin+1)
                and all(actor.inventory[k]>=n for k,n in rule.items.items())
                and (world.gold or 0)>=rule.gold):
            # Returning from a helper handoff must not discard a funded wall
            # already beside W. Prove the return with that wall actually
            # blocked, including the unchanged buffer and this build action.
            from .layout import LayoutGuard
            guard=LayoutGuard(world,deadline)
            for point in sorted(missing):
                if distance(actor.pos,point)!=1 or point in world.occupied:continue
                command=dict(action='build',name='wall',targetPos=[pos_json(point)])
                choice=Candidate(actor.id,command,240,'finish adjacent funded wall on return')
                if not guard.check([choice])[0]:continue
                view=copy(world);view.occupied=world.occupied|{point}
                back=distance_field(view,home_cells,actor.pos,deadline)
                required=1+back.get(actor.pos,float('inf'))+margin
                if time.monotonic()<deadline and required<=clock.until_night:
                    return self.finish(world,guidance,jobs,actor,[choice],'close',
                        required=required,reason='adjacent wall and observed return both fit')
        if (self.phase == 'close' and home.get(actor.pos) is not None
                and clock.until_night <= home[actor.pos] + policy.return_buffer + 1):
            return self.finish(world, guidance, jobs, actor,
                DaySchedule.moves(actor, home, 'return before remaining construction can overrun night'),
                'home', reason='remaining return time insufficient')
        if self.phase == 'harvest':
            repairs = getattr(world,'repair_commands',{}).get(actor.id,[])
            repair_steps = getattr(world,'day_repair_steps',{}).get(actor.id,1)
            if repairs and repair_steps*2+home.get(actor.pos,float('inf'))+margin < clock.until_night:
                return self.finish(world,guidance,jobs,actor,
                    [Candidate(actor.id,c,260,'repair owned low-health wall before planning optional shopping') for c in repairs],
                    'repair')
            if home.get(actor.pos)==0 and not actor.inventory['stone']:
                # An unfunded gap must not block a one-action improvement
                # already paid for and usable from the actual duty stand.
                immediate=self.adjacent_paid(world,actor,rules,policy)
                if immediate:
                    return self.finish(world,guidance,jobs,actor,immediate,'use')
            # Repair yesterday's breach with already held stone before starting
            # a new economic trip. A front breach takes the last stone too;
            # saving it for the door cannot close a still-broken perimeter.
            front_missing = (missing & set(getattr(world,'monster_front_walls',()))) - {plan['gate']}
            reserve = 0
            self.front_rebuild_pending = bool(front_missing and clock.day > 1)
            if self.front_rebuild_pending and front_missing and actor.inventory['stone'] > reserve and job:
                from .economy import ready_construction
                emergency_job = dict(job, target=min(front_missing,key=lambda p:(
                    max(abs(actor.pos[0]-p[0]),abs(actor.pos[1]-p[1])),p)),
                    defer_build=False,stock_target=actor.inventory['stone']-reserve)
                choices = ready_construction(world,clock,rules,policy,deadline,jobs={actor.id:emergency_job})
                if choices and clock.until_night > home.get(actor.pos,float('inf'))+margin+2:
                    return self.finish(world,guidance,jobs,actor,choices,'rebuild_front',
                        reason='rebuild observed front breach before new harvesting')
        # The construction planner already budgets every missing wall, walking,
        # final entry and sealing. Add the use tour after that actual endpoint.
        tail = home
        end = min((p for p in home if home[p] == 0), default=None)
        planned_walls, tour = self.wall_tour(world,actor,missing,yellow,rule,deadline)
        if planned_walls:
            entry, cost = (tour['entry'],tour['tail']) if tour else (None,None)
            if (entry is None or cost is None) and missing == {plan['gate']}:
                seals = interaction_cells(world,[plan['gate']],actor.pos) & defence_duties.stands(world,actor.id)
                closure = distance_field(world,seals,actor.pos,deadline,extra_blocked={plan['w']})
                if closure:
                    entry = min((p for p in closure if closure[p] == 0),default=None)
                    cost = 1
                    end = entry
            if entry is None or cost is None:
                return self.finish(world, guidance, jobs, actor,
                    DaySchedule.moves(actor, home, 'return while wall tour is unavailable'),
                    'home', reason='wall tour unavailable')
            tail = weighted_field(world, {entry:cost}, actor, deadline)
            end = tour['end'] if tour else end
            if job and tour:
                job.update(target=tour['target'],construction_entry=entry,construction_tail=cost,
                           construction_steps=tour['steps'],construction_end=end)
        if tail is None or end is None or time.monotonic() >= deadline:
            return self.finish(world, guidance, jobs, actor, [], 'close', reason='route budget unavailable')

        # One already-paid adjacent base recovery can precede ordinary work.
        # The same remaining construction/closure tail still has to fit.
        if 1+tail.get(actor.pos,float('inf'))+margin <= clock.until_night:
            immediate=[c for c in self.adjacent_paid(world,actor,rules,policy)
                       if c.command['name'].startswith('StationUpgradeVoucher')]
            if immediate:
                return self.finish(world,guidance,jobs,actor,immediate,'use',
                    reason='paid adjacent base recovery with construction suffix reserved')

        # Treatment at the counter is part of this itinerary, not a competing
        # medical plan that its checkout lock can later reject. No spare dose.
        from .medical import needs_treatment
        price=world.shop.get('Medicine',0)
        if (policy.medical_supply_enabled and world.near_zone(actor.pos,'weaponShop')
                and needs_treatment(world,actor,clock)
                and actor.id not in getattr(world,'checkout_pending_actors',())
                and 2+tail.get(actor.pos,float('inf'))+margin<=clock.until_night):
            if actor.inventory['Medicine']:
                return self.finish(world,guidance,jobs,actor,[Candidate(actor.id,
                    dict(action='use',name='Medicine'),260,'treat current injury before continuing checkout')],'repair')
            if len(actor.backpack)<actor.capacity and price>0 and (world.gold or 0)>=price:
                return self.finish(world,guidance,jobs,actor,[Candidate(actor.id,
                    dict(action='buy',name='Medicine',num=1),260,'buy one treatment dose at current counter')],'buy')

        # One shopping basket covers successive voucher tiers and personal
        # night stock. Forecast sale proceeds are timing-only; the buy below
        # still caps its quantity by the observed balance after receipts.
        from . import supply_basket
        trip_world = copy(world)
        trip_world.occupied = world.occupied
        trip = supply_basket.quote(trip_world,actor,clock,rules,policy,deadline,
            home=home,tail=tail,end=end,margin=margin,sale_stock=stock,
            # A funded wall tour is part of this same checkout/return suffix.
            # Stock its future service sites without pretending walls already
            # exist or allowing a purchase before the sale actually settles.
            future_repair_sites=(set(planned_walls) & set(getattr(world,'monster_front_walls',()))
                                 if actor.inventory['stone']>=stone else ()),
            cash=(world.gold or 0)+sum(n*world.vendor[k] for k,n in stock.items()))
        cash = max(0,(world.gold or 0)-getattr(world,'treasure_reserved_gold',0))
        order = None; count = 0; held = []; deliveries = {}; use_steps = 0
        checkout = tail; sale = tail; required = None
        if trip is not None:
            held = trip['held']
            use_steps = trip['use_steps']
            checkout,sale,required = trip['checkout'],trip['sale'],trip['required']
            if trip['orders']:
                name,count = next(iter(trip['orders'].items()))
                order = dict(name=name)
        # Checkout/paid delivery must not strand a repair pack that was bought
        # for a current eligible wall. Reuse the same service candidate, while
        # reserving a conservative round trip plus all quoted use/closure work.
        repairs=getattr(world,'repair_commands',{}).get(actor.id,[])
        repair_steps=getattr(world,'day_repair_steps',{}).get(actor.id)
        if (self.phase!='harvest' and repairs and repair_steps is not None
                and actor.id not in getattr(world,'checkout_pending_actors',())
                and 2*repair_steps+tail.get(actor.pos,float('inf'))+use_steps+margin<=clock.until_night):
            return self.finish(world,guidance,jobs,actor,
                [Candidate(actor.id,c,260,'perform current funded repair within remaining delivery circuit') for c in repairs],
                'repair',reason='repair and paid delivery share the same remaining deadline')
        fields = {}
        def route(carrier,target):
            key = carrier.id,target['unit'].id
            if key not in fields:
                fields[key] = distance_field(trip_world,
                    interaction_cells(trip_world,[target['unit'].pos],carrier.pos),carrier.pos,deadline)
            return fields[key]
        from .wall_pressure import priority
        ready = [(0 if t['unit'].id in getattr(world,'base_restore_ids',()) else 1,
                  t['rank'],priority(world,t['unit']),route(actor,t).get(actor.pos,float('inf')),t['unit'].id,t)
                 for t in held if not t.get('pending') and t['level']==t['unit'].level]
        if ready:
            _,_,_,length,_,target = min(ready,key=lambda t:t[:5])
            if length != float('inf'):deliveries[actor.id]=(target,length)
        if self.construction_only:
            # Paid coupons may still be used after construction, but new
            # purchases cannot displace the material trip already chosen.
            checkout = sale = tail
            required = tail.get(actor.pos)
            order, count = None, 0
        affordable_upgrade = bool(order and cash >= world.shop[order['name']])
        self.use_budget = use_steps
        if required is not None:
            self.last_required = required + margin
        self.diagnostic = dict(required=None if required is None else required+margin,
            left=clock.until_night,reserved_stone=stone,use_steps=use_steps,
            future_gate_stone=max(0,retained_stone-stone),
            missing_walls=len(missing),planned_walls=len(planned_walls),construction_only=self.construction_only,
            basket=dict(trip['orders']) if trip else {})
        funded_checkout = bool(trip and trip['orders'] and
            sum(world.shop[name] * amount for name, amount in trip['orders'].items()) <= cash)
        reopen_checkout = (self.phase == 'close' and not self.construction_only
            and missing <= {plan['gate']} and funded_checkout
            and required is not None and required + margin <= clock.until_night
            and not getattr(world, 'worker_checkout_wait', False))
        if (self.phase == 'home' or reopen_checkout) and home.get(actor.pos) == 0:
            # Reaching duty completes the previous trip, not the entire day.
            # Waiting for the final seal also completes that trip. Reconsider
            # newly affordable personal stock through the full work circuit.
            # Re-enter only here; the full circuit proof below prevents an
            # outbound/return reversal half way through an existing trip.
            if clock.until_night > margin+2:
                self.phase = 'harvest'
        if self.phase in {'harvest','close','use'} and actor.id in deliveries:
            target,length=deliveries[actor.id]
            weapon=target['name'].startswith('WeaponUpgradeVoucher')
            wall=(target['unit'].kind=='wall' and not missing-{plan['gate']})
            from .wall_policy import priority_units
            station=(target['unit'].kind=='station' and (target['unit'].id in getattr(world,'base_restore_ids',())
                     or not missing-{plan['gate']} and target['unit'].id in {u.id for u in priority_units(world)}))
            if weapon or wall or station:
                # Paid tiers can need the open passage, including front-wall
                # corners served from outside. Do not budget them only after
                # closing the gate, when the same tour may no longer fit.
                delivery=([entry for entry in held if entry['name'].startswith('WeaponUpgradeVoucher')
                    and not entry.get('pending') and entry['level']==entry['unit'].level] if weapon else [target])
                deficit = max(0,stone-actor.inventory['stone'])
                delivery_tail = home
                extra_tail = tail.get(actor.pos)
                if deficit:
                    # Connect the last coupon use to actual material collection
                    # and construction, rather than budgeting an empty-handed
                    # return as though the wall were already funded.
                    costs = {}
                    if actor.capacity-len(actor.backpack) >= deficit:
                        for mine in world.zones.get('stone', ()):
                            if getattr(world,'batch_mine_owners',{}).get(mine,actor.id)!=actor.id:
                                continue
                            for point in interaction_cells(world,[mine],actor.pos) & tail.keys():
                                costs[point] = deficit + tail[point]
                    delivery_tail = weighted_field(world,costs,actor,deadline) if costs else {}
                    extra_tail = 0
                circuit=supply_basket.use_tour(trip_world,actor,delivery,actor.pos,delivery_tail,deadline)
                if (circuit is not None and extra_tail is not None
                        and circuit+extra_tail+margin<=clock.until_night):
                    choices=([Candidate(actor.id,dict(action='use',name=target['name'],
                        targetPos=[pos_json(target['unit'].pos)]),240,
                        'deliver paid weapon before ordinary harvesting and seal' if weapon else
                        'deliver paid base before final seal' if station else
                        'deliver paid wall before final seal')]
                        if not length else DaySchedule.moves(actor,route(actor,target),
                            'deliver paid weapon while actual daytime passage remains open' if weapon else
                            'deliver paid base while actual daytime passage remains open' if station else
                            'deliver paid wall while actual daytime passage remains open'))
                    return self.finish(world,guidance,jobs,actor,choices,'use')
        if (self.phase == 'harvest' and missing-{plan['gate']}
                and actor.inventory['stone'] >= stone and self.front_rebuild_pending):
            self.phase = 'close'
        investment=bool(trip and getattr(world,'upgrade_checkout_actor',None)==actor.id
                        and any('UpgradeVoucher' in name for name in trip['orders']))
        sale_funded=bool(investment and stock and required is not None
            and required+margin<=clock.until_night
            and sum(world.shop[name]*amount for name,amount in trip['orders'].items())
                <= cash+sum(n*world.vendor[name] for name,n in stock.items()))
        if (self.phase=='harvest' and not self.construction_only
                and actor.inventory['stone']>=stone
                and (sale_funded or funded_checkout and checkout is not None
                     and checkout.get(actor.pos,float('inf'))+margin<=clock.until_night)):
            # Restocking must precede the next attack, including when the
            # damaged wall has already fallen. The basket has already matched
            # personal stock and funded the bounded maintenance requirement.
            # Restore its existing two-pack working reserve first; optional
            # top-ups must not interrupt a funded material trip while that
            # reserve is still present.
            repair_reserve=(actor.inventory['WallFixer']<2
                            and trip['orders'].get('WallFixer',0)>0)
            if investment or repair_reserve:
                # A fully funded executable investment no longer waits until
                # all remaining harvesting time has been consumed. Material,
                # use, closure and return are already in this very quote.
                self.phase=('sell' if not funded_checkout or stock and world.near_zone(actor.pos,'vendor')
                            and required is not None and required+margin<=clock.until_night else 'buy')
                required=(required if self.phase=='sell' else checkout[actor.pos])
                self.last_required=required+margin
                self.diagnostic.update(required=self.last_required,
                    harvest_released='funded investment' if investment else 'funded personal repair reserve')
        if (self.phase in {'harvest','close','home'} and next_exit_stone and missing<={plan['gate']}
                and stone<=actor.inventory['stone']<stone+next_exit_stone
                and actor.capacity-len(actor.backpack)>=stone+next_exit_stone-actor.inventory['stone']
                and not held and not getattr(world,'critical_base_ids',())):
            # Today's seal must not consume the only material needed to reopen
            # the ordinary door after clearing. Fund the extra stone with a
            # complete mine -> current construction -> duty route, never by
            # postponing an already selected necessary checkout or paid use.
            need=stone+next_exit_stone-actor.inventory['stone']
            start=distance_field(world,{actor.pos},actor.pos,deadline)
            options=[]
            for mine in world.zones.get('stone',()):
                if getattr(world,'batch_mine_owners',{}).get(mine,actor.id)!=actor.id:continue
                for point in interaction_cells(world,[mine],actor.pos)&start.keys()&tail.keys():
                    required=start[point]+need+tail[point]+margin+2
                    if required<=clock.until_night:options.append((required,start[point],mine,point))
            if options and time.monotonic()<deadline:
                required,length,mine,point=min(options)
                choices=([Candidate(actor.id,dict(action='collect',targetPos=[pos_json(mine)]),240,
                                     'collect personal next-clear exit material before sealing')]
                         if not length else DaySchedule.moves(actor,distance_field(world,{point},actor.pos,deadline),
                            'reserve next-clear exit material inside the proven closure deadline'))
                return self.finish(world,guidance,jobs,actor,choices,'harvest',
                    next_clear_stone=next_exit_stone,required=required)
        if self.phase == 'harvest':
            repairs = getattr(world, 'repair_commands', {}).get(actor.id, [])
            repair_steps = getattr(world, 'day_repair_steps', {}).get(actor.id, 1)
            if repairs and required is not None and repair_steps * 2 + required + margin < clock.until_night:
                return self.finish(world, guidance, jobs, actor,
                    [Candidate(actor.id, c, 260, 'repair walls before harvesting') for c in repairs], 'repair')
            start = distance_field(world, {actor.pos}, actor.pos, deadline)
            options = []
            deficit = max(0, stone - actor.inventory['stone'])
            for name in (('stone',) if self.construction_only and deficit else () if self.construction_only else sorted(MINERALS)):
                if not (deficit and name == 'stone') and world.vendor.get(name, 0) <= 0:
                    continue
                # Reserve the sale interaction for a newly collected ore type.
                future = dict(stock)
                future[name] = future.get(name, 0) + (0 if deficit and name == 'stone' else 1)
                future = {k:n for k,n in future.items() if n}
                mine_tail = checkout
                if future and checkout is not None:
                    mine_tail = weighted_field(world, {p:checkout[p] + len(future) for p in
                        interaction_cells(world, world.zones.get('vendor', ()), actor.pos) if p in checkout}, actor, deadline)
                if mine_tail is None:
                    continue
                for mine in world.zones.get(name, ()):
                    if getattr(world,'batch_mine_owners',{}).get(mine,actor.id) != actor.id:
                        continue
                    for p in interaction_cells(world, [mine], actor.pos) & start.keys() & mine_tail.keys():
                        if start[p] + 1 + mine_tail[p] + margin + 2 <= clock.until_night:
                            options.append((bool(deficit and name != 'stone'), -world.vendor.get(name, 0)/(start[p]+1), start[p], name, mine, p))
            if deficit and not self.construction_only and not any(o[-3] == 'stone' for o in options):
                # A shop/use detour can fit from here while making every stone
                # collection infeasible. Drop new shopping before dropping
                # necessary wall material; prove the full construction return.
                material_options = []
                for mine in world.zones.get('stone', ()):
                    if getattr(world, 'batch_mine_owners', {}).get(mine, actor.id) != actor.id:
                        continue
                    for point in interaction_cells(world, [mine], actor.pos) & start.keys() & tail.keys():
                        if start[point] + 1 + tail[point] + margin + 2 <= clock.until_night:
                            material_options.append((False, -world.vendor.get('stone', 0)/(start[point]+1),
                                                     start[point], 'stone', mine, point))
                if material_options:
                    options = material_options
                    self.construction_only = True
                    self.diagnostic.update(construction_only=True,
                        reason='defer shopping to preserve feasible wall-material circuit')
            if options and len(actor.backpack) < actor.capacity and time.monotonic() < deadline:
                _, _, length, name, mine, point = min(options)
                choices = ([Candidate(actor.id, dict(action='collect', targetPos=[pos_json(mine)]), 240,
                                      'harvest with sale, checkout, closure and use time reserved')] if not length else
                           DaySchedule.moves(actor, distance_field(world, {point}, actor.pos, deadline),
                                             'harvest only inside complete evening circuit budget'))
                return self.finish(world, guidance, jobs, actor, choices, 'harvest')
            if (not options and deficit and required is None and not world.phase_task
                    and len(actor.backpack) < actor.capacity and time.monotonic() < deadline):
                # A guard at the distant entrance must not prevent the one
                # material action already available here. This permits no
                # movement through that guard and claims no completed return.
                pioneer = world.ours.get(world.night_roster.p)
                adjacent = [mine for mine in world.zones.get('stone', ())
                            if actor.pos in interaction_cells(world,[mine],actor.pos)
                            and getattr(world,'batch_mine_owners',{}).get(mine,actor.id)==actor.id]
                if adjacent and pioneer and pioneer.alive:
                    preview = copy(world)
                    preview.occupied = world.occupied - {pioneer.pos}
                    back = distance_field(preview,home_cells,actor.pos,deadline).get(actor.pos)
                    if (back is not None and back + deficit + margin + 2 < clock.until_night
                            and time.monotonic() < deadline):
                        command = dict(action='collect',targetPos=[pos_json(min(adjacent))])
                        return self.finish(world,guidance,jobs,actor,
                            [Candidate(actor.id,command,240,'collect necessary adjacent stone while entrance clearance is pending')],
                            'harvest',reason='material collection precedes blocked return')
            if required is None or time.monotonic() >= deadline:
                return self.finish(world,guidance,jobs,actor,[], 'harvest', reason='wait for an observed complete route')
            self.phase = 'close' if self.construction_only else 'sell'
        if (self.phase == 'sell' and funded_checkout and checkout is not None
                and not world.near_zone(actor.pos, 'vendor')
                and checkout.get(actor.pos, float('inf')) + margin <= clock.until_night):
            # Sale proceeds are unnecessary for this whole observed basket.
            # Keep the ore and complete checkout before a distant vendor trip
            # can consume the time reserved for paid upgrades and the return.
            self.phase = 'buy'
            required = checkout[actor.pos]
            self.last_required = required + margin
            self.diagnostic.update(required=self.last_required, sale_deferred='basket already funded')
        if (self.phase == 'buy' and stock and not funded_checkout
                and trip and trip['orders'] and required is not None
                and required + margin <= clock.until_night):
            # Shared cash may have changed, or a blocked prior frame may have
            # lost its quote. Re-establish the actual sale before spending its
            # forecast proceeds; current stock and the whole tour are proven.
            self.phase = 'sell'
        if self.phase == 'sell':
            if (stock and required is None and self.last_required is not None
                    and self.last_required + 2 < clock.until_night):
                return self.finish(world,guidance,jobs,actor,[],'sell',
                    reason='temporarily blocked sale route; preserve unsold personal stock')
            if stock and sale is not None and required is not None and required + margin <= clock.until_night:
                name = max(stock, key=lambda k:(stock[k]*world.vendor[k], k))
                choices = ([Candidate(actor.id, dict(action='sell', name=name, num=stock[name]), 240,
                                      'sell personal surplus; keep every missing wall stone')]
                           if world.near_zone(actor.pos, 'vendor') else DaySchedule.moves(actor, sale, 'sell before buying vouchers'))
                return self.finish(world, guidance, jobs, actor, choices, 'sell')
            self.phase = 'buy'
        if self.phase == 'buy':
            if count and checkout is not None and actor.pos in checkout:
                # Once the sale is skipped, preserve the confirmed checkout
                # circuit rather than expiring it against the longer sale tour.
                self.last_required = checkout[actor.pos] + margin
                self.diagnostic['required'] = self.last_required
            if (required is None and self.last_required is not None
                    and self.last_required+2 < clock.until_night):
                # A teammate can briefly occupy the single return corridor.
                # Keep the already funded checkout until its last valid budget
                # expires; do not turn one blocked frame into a daylong close.
                return self.finish(world,guidance,jobs,actor,[],'buy',
                    reason='temporarily blocked checkout route; preserve observed trip')
            if count and checkout is not None and checkout.get(actor.pos, float('inf')) + margin <= clock.until_night:
                amount = min(count, cash // world.shop[order['name']], actor.capacity-len(actor.backpack))
                if amount:
                    choices = ([Candidate(actor.id, dict(action='buy', name=order['name'], num=amount), 240,
                                          'buy vouchers with observed proceeds before returning to seal',
                                          gold_reserve=getattr(world, 'treasure_reserved_gold', 0))]
                               if world.near_zone(actor.pos, 'weaponShop') else DaySchedule.moves(actor, checkout, 'voucher checkout before closing walls'))
                    return self.finish(world, guidance, jobs, actor, choices, 'buy')
            if (not held and affordable_upgrade and len(actor.backpack) < actor.capacity
                    and clock.until_night > (home[actor.pos] + margin if actor.pos in home
                        else self.last_required if self.last_required is not None else float('inf')) + 3):
                # A teammate crossing the future service endpoint can make
                # this frame's delivery proof unavailable. Keep the checkout
                # commitment and re-observe; do not abandon it for early sealing.
                return self.finish(world,guidance,jobs,actor,[], 'buy', reason='wait for a complete checkout and use route')
            self.phase = 'close'
        if self.phase=='close' and getattr(world,'worker_checkout_wait',False):
            return self.finish(world,guidance,jobs,actor,[],'close',
                reason='no additional complete circuit fits while pioneer checks out')
        if self.phase == 'close' and missing:
            # Actual construction and the pioneer-first gate handshake own
            # funded wall work. Unfunded gaps remain visible in diagnostics.
            if clock.until_night <= home.get(actor.pos,float('inf')) + policy.return_buffer + 1:
                return self.finish(world, guidance, jobs, actor,
                    DaySchedule.moves(actor, home, 'return before remaining construction can overrun night'),
                    'home', reason='remaining return time insufficient')
            if actor.inventory['stone']:
                # Follow the complete funded tour used by the time budget.
                # Replacing it with the cheapest one-wall return on every
                # frame can leave exterior corners until a second worker is
                # recalled, despite reserving enough time to build them all.
                if (tour and planned_walls == missing and actor.inventory['stone'] >= stone
                        and tour['steps'] + margin <= clock.until_night
                        and tour['target'] != plan['gate']):
                    from .layout import LayoutGuard
                    target, stand = tour['target'], tour['entry']
                    command = dict(action='build',name='wall',targetPos=[pos_json(target)])
                    route_world = copy(world)
                    route_world.occupied = world.occupied | (missing-{plan['gate']})
                    route = distance_field(route_world,{stand},actor.pos,deadline)
                    required = route.get(actor.pos,float('inf')) + tour['tail'] + margin
                    if (required <= clock.until_night and
                            LayoutGuard(world,deadline).check([Candidate(actor.id,command,240,'funded wall tour')])[0]):
                        choices = ([Candidate(actor.id,command,240,'complete funded wall tour before partial repairs')]
                            if actor.pos == stand else DaySchedule.moves(actor,route,
                                'follow funded wall tour with complete return reserved'))
                        if choices:
                            if job:job.update(target=target,defer_build=False)
                            return self.finish(world,guidance,jobs,actor,choices,'close')
                # A full perimeter tour may use distant exterior stands. Spend
                # held material on a reachable front gap when its own build and
                # observed return fit, without assuming the other gaps close.
                from .layout import LayoutGuard
                guard = LayoutGuard(world,deadline)
                reachable = distance_field(world,{actor.pos},actor.pos,deadline)
                options = []
                for target in sorted((missing-{plan['gate']}) & set(getattr(world,'monster_front_walls',()))):
                    if target in world.occupied:continue
                    command = dict(action='build',name='wall',targetPos=[pos_json(target)])
                    if not guard.check([Candidate(actor.id,command,240,'funded front repair')])[0]:continue
                    after = copy(world)
                    after.occupied = (world.occupied-{actor.pos})|{target}
                    for stand in interaction_cells(world,[target],actor.pos) & reachable.keys():
                        back = distance_field(after,home_cells,stand,deadline)
                        total = reachable[stand]+1+back.get(stand,float('inf'))+policy.return_buffer
                        if total <= clock.until_night and time.monotonic()<deadline:
                            options.append((total,reachable[stand],target,stand,command))
                if options:
                    _,length,target,stand,command=min(options)
                    if job:job.update(target=target,defer_build=False,stock_target=1)
                    choices=([Candidate(actor.id,command,240,'build funded front gap before returning')]
                        if not length else DaySchedule.moves(actor,distance_field(world,{stand},actor.pos,deadline),
                            'approach funded front gap with actual return reserved'))
                    return self.finish(world,guidance,jobs,actor,choices,'close')
                choices = DaySchedule.moves(actor, home, 'return before the worker closes the final gap')
                if missing - {plan['gate']} and job:
                    from .economy import ready_construction
                    job['defer_build'] = False
                    job['stock_target'] = min(job.get('stock_target',0), actor.inventory['stone'])
                    choices = ready_construction(world,clock,rules,policy,deadline,jobs={actor.id:job}) or choices
                return self.finish(world, guidance, jobs, actor, choices, 'close')
            if home.get(actor.pos) != 0:
                return self.finish(world,guidance,jobs,actor,DaySchedule.moves(actor,home,
                    'personal wall stock exhausted: return before using held vouchers'),'home',
                    reason='unfunded wall gaps remain')
            # Materials for the remaining gaps are unavailable today. Once
            # home, this must not suppress a legal use of an already held item.
            self.diagnostic['deferred_walls'] = len(missing)
        if self.phase == 'use' and not held and affordable_upgrade and checkout is not None:
            if checkout.get(actor.pos,float('inf')) + margin < clock.until_night:
                self.phase = 'buy'  # Continue the observed upgrade tier next frame.
                return self.finish(world,guidance,jobs,actor,[], 'buy',reason='next affordable upgrade circuit fits')
        self.phase = 'use'
        if actor.id in deliveries:
            target, length = deliveries[actor.id]
            use_route = weighted_field(world, {p:home[p]+1 for p in
                interaction_cells(world,[target['unit'].pos],actor.pos) if p in home},actor,deadline)
            if use_route is None or use_route.get(actor.pos,float('inf')) + policy.return_buffer > clock.until_night:
                return self.finish(world,guidance,jobs,actor,
                    DaySchedule.moves(actor,home,'changed use route no longer fits: complete return first'),'home')
            choices = ([Candidate(actor.id, dict(action='use', name=target['name'],
                        targetPos=[pos_json(target['unit'].pos)]), 240, 'apply personal voucher after feasible wall work and return')]
                       if not length else DaySchedule.moves(actor, route(actor, target), 'deliver personal voucher after feasible wall work'))
            return self.finish(world, guidance, jobs, actor, choices, 'use')
        orders=[name for name,n in actor.inventory.items() if n and name.endswith('SummonOrder')]
        if (orders and getattr(world,'summon_use_remaining',0) and not getattr(world,'critical_base_ids',())
                and home.get(actor.pos,float('inf'))+policy.return_buffer+1 < clock.until_night):
            return self.finish(world,guidance,jobs,actor,[Candidate(actor.id,
                dict(action='use',name=orders[0]),240,'use personal next-wave order after maintenance')],'use')
        self.phase='home'
        return self.finish(world, guidance, jobs, actor, DaySchedule.moves(actor, home, 'return to single turret after daily work'), 'home')

    def finish(self, world, guidance, jobs, actor, choices, phase, **details):
        world.caretaker_day_phase = phase
        self.diagnostic.update(stage=phase, actor=actor.id, **details)
        if phase == 'close':
            if actor.id in jobs:
                jobs[actor.id]['defer_build'] = False
                jobs[actor.id]['stock_target'] = min(jobs[actor.id].get('stock_target',0),actor.inventory['stone'])
        preview = copy(guidance)
        preview.return_routes = {i:r for i,r in guidance.return_routes.items() if i != actor.id}
        world.sunset_actions[actor.id] = [c.command for c in choices]
        if phase == 'buy' and any(c.command['action'] == 'buy' for c in choices):
            world.sunset_buyer = actor.id
        choices = [c for c in choices if preview.permit(c)]
        world.sunset_actions[actor.id] = [c.command for c in choices]
        guidance.funded_actions[actor.id] = list(world.sunset_actions[actor.id])
        guidance.day_actions[actor.id] = list(world.sunset_actions[actor.id])
        if choices:
            guidance.work_plans[actor.id] = dict(owner='caretaker_day',phase=phase,
                commands=[c.command for c in choices])
        else:
            # A blocked plan records its phase but must not intersect every
            # later movement/return proposal with an empty permission set.
            world.sunset_actions.pop(actor.id,None)
            guidance.funded_actions.pop(actor.id,None)
            guidance.day_actions.pop(actor.id,None)
            guidance.work_plans.pop(actor.id,None)
            waiting_for_seal=phase=='close' and jobs.get(actor.id,{}).get('gate') and actor.inventory['stone']>0
            if (phase == 'home' or waiting_for_seal) and actor.pos in defence_duties.stands(world, actor.id):
                guidance.work_plans[actor.id] = dict(owner='caretaker_day', phase=phase,
                    commands=[], at_duty=True)
                self.diagnostic['completed_return'] = True
            else:
                self.diagnostic['blocked'] = True
                if phase in {'buy','sell'}:
                    # Keep ownership while a service route is blocked; an
                    # unrelated material trip must not move the carrier away.
                    guidance.work_plans[actor.id] = dict(owner='caretaker_day',phase=phase,
                        commands=[],waiting_for_route=True)
        return choices
