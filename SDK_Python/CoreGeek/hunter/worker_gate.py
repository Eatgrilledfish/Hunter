"""Observed pioneer-first ingress; the maintenance worker closes from inside.

The exterior miner has a separate dawn cashout. No command assumes another
role's simultaneous move, future stone, or a successful unobserved sale/build.
"""
import time
from copy import copy

from .arbitration import Candidate
from .defence_duties import rotator, stands, ingress_reserve
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import MINERALS, distance, pos_json
from .rules import station_rings


def prepare(state, world, clock, rules, policy, deadline):
    plan = world.task_side_plan
    roster = world.night_roster
    state.w, state.m, state.p = roster.w, roster.m, roster.p
    worker = world.ours.get(roster.w)
    pioneer = world.ours.get(rotator(world))
    miner = world.ours.get(roster.m)
    blue, yellow = station_rings(plan['anchor'])
    interior = blue | yellow | world.stations[0].cells
    gate = plan['gate']
    enclosing = set(world.wall_targets or ()) == yellow
    walls = {u.pos:u for u in world.ours.values() if u.alive and u.kind == 'wall'}
    result = []
    state.stage = 'WORKER_DAY'
    state.diagnostic = {'stage':state.stage, 'builder':roster.w, 'rotator':rotator(world), 'gate':gate}
    world.gate_worker_duty = dict(round=world.round, gate=gate, worker=roster.w)
    world.ordered_gate_actions = {}
    world.ordered_ingress_due = False
    from .procurement import upgrade_demand
    delivery_targets, _, _, _ = upgrade_demand(world, policy, rules=rules)
    supplying = bool(miner and miner.backpack is not None and any(
        miner.inventory[r['name']] for r in delivery_targets.values()))
    if (clock.phases == {'day'} and miner and miner.alive
            and miner.id not in world.night_defenders and miner.pos not in interior and not supplying):
        # The exit and the harvest route must agree. Otherwise the miner exits
        # on one frame and takes a shortcut back across the empty wall ring on
        # the next, repeating until construction happens to block that shortcut.
        world.navigation_avoided.setdefault(miner.pos,set()).update(interior)

    def offer(actor, command, reason):
        state.commands[actor.id] = [command] if command else []
        if command:
            result.append(Candidate(actor.id, command, 1100, reason))
        return command

    def step(actor, goals, extra=()):
        field = distance_field(world, goals, actor.pos, deadline, extra_blocked=extra)
        if time.monotonic() >= deadline or actor.pos not in field:
            return None
        choices = sorted(q for q in neighbours(actor.pos) if field.get(q, float('inf')) < field[actor.pos])
        return dict(action='move', targetPos=[pos_json(choices[0])]) if choices else None

    def hold(actor):
        state.commands[actor.id] = []
        sites = (plan['a'],plan['b']) if actor.id == rotator(world) else (plan['c'],)
        if actor.pos in stands(world,actor.id):
            state.firearms[actor.id] = [g.id for g in world.weapons if g.pos in sites and distance(g.pos,actor.pos)<=1]

    def clear_worker_for(traveller, goals):
        reach = distance_field(world,{worker.pos},worker.pos,deadline)
        unblocked = copy(world)
        unblocked.occupied = world.occupied-{worker.pos}
        shortest = distance_field(unblocked,goals,traveller.pos,deadline).get(traveller.pos)
        if shortest is None:return None
        for q in sorted(reach,key=lambda q:(reach[q],distance(q,gate),q)):
            if q in interior or reach[q] > 6 or q == traveller.pos:
                continue
            preview = copy(world)
            preview.occupied = (world.occupied - {worker.pos}) | {q}
            passage = distance_field(preview,goals,traveller.pos,deadline)
            if (passage.get(traveller.pos,float('inf'))<=shortest+1
                    and time.monotonic() < deadline):
                return step(worker,{q})
            if time.monotonic() >= deadline:
                break
        return None

    # One cashout per observed morning, completed only by an empty ore stock.
    if clock.phases == {'day'} and clock.day > 1 and miner and miner.alive and miner.id not in world.night_defenders:
        if state.cashout_day != clock.day:
            state.cashout_day = clock.day
            state.cashout_done = False
        if not state.cashout_done:
            stock = {k:miner.inventory[k] for k in MINERALS if miner.inventory[k]}
            if miner.backpack is not None and not stock:
                state.cashout_done = True
            else:
                offer(miner, None, 'dawn cashout waits for observed inventory and vendor')
                quoted = {k:n for k,n in stock.items() if world.vendor.get(k,0)>0}
                if quoted and world.near_zone(miner.pos,'vendor'):
                    name = max(quoted,key=lambda k:(quoted[k]*world.vendor[k],k))
                    offer(miner,dict(action='sell',name=name,num=quoted[name]),'sell entire personal ore type after dawn')
                elif quoted:
                    offer(miner,step(miner,interaction_cells(world,world.zones.get('vendor',()),miner.pos)),
                          'dawn miner goes directly to merchant before any new job')
                state.diagnostic['cashout'] = dict(actor=miner.id, stock=stock, done=False)
    if not worker or not worker.alive or not pioneer or not pioneer.alive:
        return result
    if (clock.phases == {'day'} and supplying and miner.alive
            and miner.id not in world.night_defenders and miner.id not in state.commands):
        from .night_resupply import deliver_after_dawn
        command=deliver_after_dawn(world,miner,rules,policy,deadline)
        offer(miner,command,'deliver personal prepaid night vouchers after actual dawn cashout')
        state.diagnostic['day_delivery']=dict(actor=miner.id,route_observed=command is not None)

    # Turn a useful personal ore batch into shared defence money while the
    # guards can still visit the shop and the daylight gate is open. Dawn's
    # mandatory liquidation above remains first; neither sale is projected as
    # spendable cash before its next observed receipt.
    if (clock.phases == {'day'} and miner and miner.alive and miner.backpack is not None
            and miner.id not in world.night_defenders and miner.pos not in interior
            and miner.id not in state.commands and clock.until_night > 18):
        from .supply_basket import requirements
        needed = requirements(world,rules,policy)
        prices = [world.shop[r['name']] for r in needed if world.shop.get(r['name'],0)>0]
        stock = {k:miner.inventory[k] for k in MINERALS if miner.inventory[k] and world.vendor.get(k,0)>0}
        if 'stone' in stock:
            missing_count=len(set(world.wall_targets or ())-walls.keys())
            stock['stone']=max(0,stock['stone']-max(1,missing_count-worker.inventory['stone']))
            if not stock['stone']:stock.pop('stone')
        value=sum(n*world.vendor[k] for k,n in stock.items())
        if prices and stock and ((world.gold or 0)<prices[0]<=(world.gold or 0)+value or value>=prices[0]
                or (miner.capacity is not None and len(miner.backpack)>=miner.capacity)):
            vendors=interaction_cells(world,world.zones.get('vendor',()),miner.pos)
            route=distance_field(world,vendors,miner.pos,deadline)
            if route.get(miner.pos,float('inf'))+len(stock)<clock.until_night:
                name=max(stock,key=lambda k:(stock[k]*world.vendor[k],k))
                command=(dict(action='sell',name=name,num=stock[name]) if world.near_zone(miner.pos,'vendor')
                         else step(miner,vendors))
                if command:
                    offer(miner,command,'cash out funded defence ore before the guards close the day')
                    state.diagnostic['day_sale']=dict(actor=miner.id,value=value,item=name,steps=route[miner.pos])

    if clock.phases == {'night'}:
        state.stage = 'WORKER_NIGHT'
        # Ordinary return/combat/repair planners own the guards at night.
        # Exterior supply uses its own actual routes; no night gate removal.
        return state._independent_work(world,clock,rules,policy,deadline,result)
    if clock.phases != {'day'}:
        return result

    # The worker next to C opens the daytime exit. M never returns to do it.
    if (clock.day > 1 and gate in walls and clock.until_night > 35 and walls[gate].level == 1
            and not getattr(world,'worker_close_requested',False)):
        state.stage = 'WORKER_DAWN_OPEN'
        goals = (set(neighbours(gate)) & blue) - {plan['w']}
        command = (dict(action='remove',targetPos=[pos_json(gate)]) if distance(worker.pos,gate)==1
                   else step(worker,goals))
        if command and command['action']=='remove':
            world.ordered_gate_actions[worker.id] = [command]
        offer(worker,command,'maintenance worker opens observed gate at dawn')
        state.diagnostic.update(stage=state.stage, opened_observed=False)
        return result

    # Release an interior economy worker before either guard plugs the exit.
    # This also covers the first day's construction, when M has no overnight ore.
    if (miner and miner.alive and miner.id not in world.night_defenders and not supplying
            and miner.pos in interior and gate not in walls and len(world.weapons)==rules.weapon_limit):
        exits = {q for q in neighbours(gate) if world.inside(q) and q not in interior}
        command = step(miner,exits)
        offer(miner,command,'economy worker exits before the ordered guard ingress')
        if not command:
            clearance = clear_worker_for(miner,exits)
            if clearance:offer(worker,clearance,'maintenance worker clears economy worker exit')
        state.diagnostic.update(stage='MINER_EXIT',miner_outside_observed=False)
        return result

    # Construction owns the remaining walls, but an unfinished perimeter must
    # not disable guard ingress. A worker at C can block the only path to A/B
    # even when another wall elsewhere is still missing.
    missing = set(world.wall_targets or yellow) - walls.keys() - ({gate} if enclosing else set())
    if not enclosing:
        # A front-only first-day layout has no final enclosing-wall closure.
        # Ordinary return planning still owns both guards; do not reserve W
        # while P walks home and consume a feasible first-night shop window.
        state.diagnostic.update(stage='PARTIAL_RING_DAY')
        return result

    relaxed = copy(world)
    relaxed.occupied = world.occupied - {u.pos for u in world.movers}
    home = distance_field(relaxed,{plan['w']},pioneer.pos,deadline)
    # The exterior walks can happen concurrently. W's day itinerary already
    # reserves its own trip, construction and voucher use. Adding that entire
    # itinerary here recalled P from a nearby shop many turns too early.
    # Reserve P's entry and bounded worker clearance at the actual bottleneck.
    required = ingress_reserve(world, policy, home.get(pioneer.pos,0))
    if clock.until_night > required:
        if not missing and getattr(world,'worker_close_requested',False) and pioneer.pos != plan['w']:
            # W has finished the wall tour and is waiting for final closure.
            # Keep the entrance open now: a W blocking C also makes P's
            # checkout budget see no return route, freezing P at the shop.
            # Clear W without recalling P, so the remaining purchases finish.
            actual = distance_field(world,{plan['w']},pioneer.pos,deadline).get(pioneer.pos)
            if actual is None or actual > home.get(pioneer.pos,actual)+2:
                command = clear_worker_for(pioneer,{plan['w']})
                if command:offer(worker,command,'open return corridor for pioneer checkout before dusk')
            elif worker.pos not in interior:
                hold(worker)
            state.diagnostic.update(stage='CHECKOUT_PASSAGE',pioneer_return_ordered=False)
        if missing:state.diagnostic.update(stage='WORKER_CONSTRUCTION',remaining_walls=len(missing))
        return result
    world.ordered_ingress_due = True
    state.stage = 'PIONEER_FIRST'

    if pioneer.pos != plan['w']:
        actual = distance_field(world,{plan['w']},pioneer.pos,deadline).get(pioneer.pos)
        # A long walk around the entire perimeter is not evidence that the
        # worker has finished clearing the short gate corridor. Continue the
        # clearance until P's actual path is close to its unobstructed path.
        detour = actual is None or actual>home.get(pioneer.pos,actual)+2
        command = None if detour else step(pioneer,{plan['w']})
        if command:
            offer(pioneer,command,'pioneer enters shared two-turret stand before worker')
            # A worker outside waits away from the only entry, then follows.
            hold(worker)
        else:
            # If the worker is already inside/on the passage, yield to a real
            # free tile that leaves both entry and final common stand reachable.
            offer(pioneer,None,'wait for worker clearance to be observed')
            command = clear_worker_for(pioneer,{plan['w']})
            if command:
                offer(worker,command,'worker yields passage before pioneer enters')
            else:
                hold(worker)
                state.diagnostic['blocked'] = 'no observed clear ingress route'
        state.diagnostic['stage'] = state.stage
        return result

    hold(pioneer)
    state.stage = 'WORKER_LAST'
    if missing:
        # P has cleared the bottleneck. W's single daily itinerary now owns
        # construction/use AND its return budget. Forcing W home on every
        # off-stand frame fights that itinerary and makes it walk back/forth.
        # Do not claim a sealed ring or relax any wall build permission.
        state.diagnostic.update(stage='GUARDS_IN_WITH_GAPS',remaining_walls=len(missing))
        return result
    if gate in walls:
        state.stage = 'WORKER_SEALED'
        return result
    # First-day front-only layouts do not manufacture a full enclosing ring.
    if not enclosing:
        return result
    entries = (set(neighbours(gate)) & blue) - {plan['w']}
    # Prefer a sealing cell which can already serve the single turret.
    service = entries & stands(world,worker.id)
    goals = service or entries
    if worker.pos not in goals:
        offer(worker,step(worker,goals),'maintenance worker enters last with own sealing stone')
    elif worker.backpack is not None and worker.inventory['stone'] >= 1 and gate not in world.occupied:
        state.commands.setdefault(worker.id,[])
        world.external_gate_permit = dict(m=roster.m,w=roster.w,p=roster.p,gate=gate,builder=worker.id,
                                          inner=True,ordered=True,rotator=pioneer.id)
        from .external_gate import valid_seal
        if valid_seal(world,((worker.id,'wall',gate),)):
            command = dict(action='build',name='wall',targetPos=[pos_json(gate)])
            world.seal_cells = frozenset({gate})
            world.ordered_gate_actions[worker.id] = [command]
            offer(worker,command,'worker closes final wall after observed pioneer arrival')
            state.stage = 'WORKER_SEAL_PENDING'
    state.diagnostic.update(stage=state.stage, pioneer_inside_observed=True, worker_inside_observed=worker.pos in blue)
    return result
