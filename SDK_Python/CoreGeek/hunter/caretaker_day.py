"""Repair, harvest, sell, buy, close the perimeter, then apply personal vouchers.

The end-of-day circuit is reserved before admitting another repair or harvest.
Income is only a timing estimate until the next observed sale receipt.
"""
from copy import copy
from dataclasses import dataclass, field
import time

from . import defence_duties, procurement
from .arbitration import Candidate
from .day_schedule import DaySchedule, weighted_field
from .day_division import DayDivision
from .navigation import distance_field, interaction_cells
from .protocol import MINERALS, pos_json
from .rules import station_rings


@dataclass
class CaretakerDay:
    day: int | None = None
    identity: str | None = None
    phase: str = 'harvest'
    last_required: int | None = None
    use_budget: int = 0
    diagnostic: dict = field(default_factory=dict)

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
        gate = world.task_side_plan['gate']
        subset = set(sorted(missing-{gate},key=lambda p:(max(abs(actor.pos[0]-p[0]),abs(actor.pos[1]-p[1])),p))[:count])
        if count == len(missing):
            subset = set(missing)
        if not subset:
            return subset, None
        topology = copy(world)
        topology.occupied = (world.occupied - {u.pos for u in world.movers}) | {world.task_side_plan['w']}
        tour = DayDivision(gate=gate).tour(topology,actor,subset,ring,deadline,world.occupied)
        return subset,tour

    def prepare(self, world, clock, rules, policy, guidance, jobs, excluded, deadline):
        self.diagnostic = {}
        actor = world.ours.get(world.night_roster.w)
        if (not actor or not actor.alive or actor.backpack is None or actor.capacity is None
                or len(world.weapons) != rules.weapon_limit or actor.id in excluded):
            return None
        walls = {u.pos for u in world.ours.values() if u.alive and u.kind == 'wall'}
        if clock.day == 1 and not set(world.wall_targets or ()) <= walls:
            # The opening construction project creates the initial defence;
            # subsequent daily maintenance must not replace that bootstrap.
            return None
        if (self.day, self.identity) != (clock.day, actor.id):
            self.day, self.identity, self.phase = clock.day, actor.id, 'harvest'
            self.last_required = None
            self.use_budget = 0
        world.caretaker_day_actor = actor.id
        world.caretaker_day_phase = self.phase
        # Generic voucher proposals must not create a second, conflicting
        # worker itinerary. The free-pioneer dispatcher uses its own view.
        world.upgrade_dispatch_ids = set()
        job = jobs.get(actor.id, {})
        if job and job.get('name') != 'wall':
            return None
        plan = world.task_side_plan
        _, yellow = station_rings(plan['anchor'])
        missing = set(world.wall_targets or ()) - walls - set(getattr(world,'helper_wall_targets',()))
        rule = rules.build_rule(world, 'wall')
        stone = len(missing) * rule.items.get('stone', 0) if rule else 0
        stock = {k:max(0, actor.inventory[k] - (stone if k == 'stone' else 0))
                 for k in MINERALS if world.vendor.get(k, 0) > 0}
        stock = {k:n for k,n in stock.items() if n}
        home = distance_field(world, defence_duties.stands(world, actor.id), actor.pos, deadline,
                              extra_blocked={plan['w']})
        margin = policy.return_buffer + (8 if set(world.wall_targets or ()) == yellow else 0)
        self.diagnostic = dict(left=clock.until_night,missing_walls=len(missing),reserved_stone=stone)
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
                immediate=[]
                for target in world.ours.values():
                    if (target.alive and target.level in (1,2) and
                            procurement.upgrade_allowed(world,target,policy,rules) and
                            max(abs(actor.pos[0]-target.pos[0]),abs(actor.pos[1]-target.pos[1]))<=1):
                        prefix='Weapon' if target in world.weapons else 'Wall' if target.kind=='wall' else None
                        name=f'{prefix}UpgradeVoucher{target.level}'
                        if prefix and actor.inventory[name]:
                            immediate.append((prefix!='Weapon',target.id,name,target))
                if immediate:
                    _,_,name,target=min(immediate,key=lambda t:t[:2])
                    return self.finish(world,guidance,jobs,actor,[Candidate(actor.id,
                        dict(action='use',name=name,targetPos=[pos_json(target.pos)]),240,
                        'apply paid adjacent voucher after repair while remaining gaps lack material')],'use')
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

        # One shopping basket covers successive voucher tiers and personal
        # night stock. Forecast sale proceeds are timing-only; the buy below
        # still caps its quantity by the observed balance after receipts.
        from . import supply_basket
        trip_world = copy(world)
        trip_world.occupied = world.occupied | {plan['w']}
        trip = supply_basket.quote(trip_world,actor,clock,rules,policy,deadline,
            home=home,tail=tail,end=end,margin=margin,sale_stock=stock,
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
        fields = {}
        def route(carrier,target):
            key = carrier.id,target['unit'].id
            if key not in fields:
                fields[key] = distance_field(trip_world,
                    interaction_cells(trip_world,[target['unit'].pos],carrier.pos),carrier.pos,deadline)
            return fields[key]
        ready = [(t['rank'],route(actor,t).get(actor.pos,float('inf')),t['unit'].id,t)
                 for t in held if t['level']==t['unit'].level]
        if ready:
            _,length,_,target = min(ready,key=lambda t:t[:3])
            if length != float('inf'):deliveries[actor.id]=(target,length)
        affordable_upgrade = bool(order and cash >= world.shop[order['name']])
        self.use_budget = use_steps
        if required is not None:
            self.last_required = required + margin
        self.diagnostic = dict(required=None if required is None else required+margin,
            left=clock.until_night,reserved_stone=stone,use_steps=use_steps,
            missing_walls=len(missing),planned_walls=len(planned_walls),
            basket=dict(trip['orders']) if trip else {})
        if self.phase == 'harvest':
            repairs = getattr(world, 'repair_commands', {}).get(actor.id, [])
            repair_steps = getattr(world, 'day_repair_steps', {}).get(actor.id, 1)
            if repairs and required is not None and repair_steps * 2 + required + margin < clock.until_night:
                return self.finish(world, guidance, jobs, actor,
                    [Candidate(actor.id, c, 260, 'repair walls before harvesting') for c in repairs], 'repair')
            start = distance_field(world, {actor.pos}, actor.pos, deadline)
            options = []
            deficit = max(0, stone - actor.inventory['stone'])
            for name in (('stone',) if deficit else sorted(MINERALS)):
                if not deficit and world.vendor.get(name, 0) <= 0:
                    continue
                # Reserve the sale interaction for a newly collected ore type.
                future = dict(stock)
                future[name] = future.get(name, 0) + (0 if deficit else 1)
                future = {k:n for k,n in future.items() if n}
                mine_tail = checkout
                if future and checkout is not None:
                    mine_tail = weighted_field(world, {p:checkout[p] + len(future) for p in
                        interaction_cells(world, world.zones.get('vendor', ()), actor.pos) if p in checkout}, actor, deadline)
                if mine_tail is None:
                    continue
                for mine in world.zones.get(name, ()):
                    for p in interaction_cells(world, [mine], actor.pos) & start.keys() & mine_tail.keys():
                        if start[p] + 1 + mine_tail[p] + margin + 2 <= clock.until_night:
                            options.append((-world.vendor.get(name, 0)/(start[p]+1), start[p], name, mine, p))
            if options and len(actor.backpack) < actor.capacity and time.monotonic() < deadline:
                _, length, name, mine, point = min(options)
                choices = ([Candidate(actor.id, dict(action='collect', targetPos=[pos_json(mine)]), 240,
                                      'harvest with sale, checkout, closure and use time reserved')] if not length else
                           DaySchedule.moves(actor, distance_field(world, {point}, actor.pos, deadline),
                                             'harvest only inside complete evening circuit budget'))
                return self.finish(world, guidance, jobs, actor, choices, 'harvest')
            if required is None or time.monotonic() >= deadline:
                return self.finish(world,guidance,jobs,actor,[], 'harvest', reason='wait for an observed complete route')
            self.phase = 'sell'
        if self.phase == 'sell':
            if stock and sale is not None and required is not None and required + margin <= clock.until_night:
                name = max(stock, key=lambda k:(stock[k]*world.vendor[k], k))
                choices = ([Candidate(actor.id, dict(action='sell', name=name, num=stock[name]), 240,
                                      'sell personal surplus; keep every missing wall stone')]
                           if world.near_zone(actor.pos, 'vendor') else DaySchedule.moves(actor, sale, 'sell before buying vouchers'))
                return self.finish(world, guidance, jobs, actor, choices, 'sell')
            self.phase = 'buy'
        if self.phase == 'buy':
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
        if self.phase == 'close' and missing:
            # Actual construction and the pioneer-first gate handshake own
            # funded wall work. Unfunded gaps remain visible in diagnostics.
            if clock.until_night <= home.get(actor.pos,float('inf')) + policy.return_buffer + 1:
                return self.finish(world, guidance, jobs, actor,
                    DaySchedule.moves(actor, home, 'return before remaining construction can overrun night'),
                    'home', reason='remaining return time insufficient')
            if actor.inventory['stone']:
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
        return choices
