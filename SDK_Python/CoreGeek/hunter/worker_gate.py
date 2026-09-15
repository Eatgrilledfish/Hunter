"""Observed pioneer-first ingress; the maintenance worker closes from inside.

The exterior miner has a separate dawn cashout. No command assumes another
role's simultaneous move, future stone, or a successful unobserved sale/build.
"""
import time
from copy import copy
from collections import deque

from .arbitration import Candidate
from .defence_duties import rotator, stands, ingress_reserve
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import MINERALS, distance, pos_json, position
from .rules import station_rings


def prepare(state, world, clock, rules, policy, deadline, *, task_busy=False):
    plan = world.task_side_plan
    roster = world.night_roster
    state.w, state.m, state.p = roster.w, roster.m, roster.p
    worker = world.ours.get(roster.w)
    pioneer = world.ours.get(rotator(world))
    miner = world.ours.get(roster.m)
    blue, yellow = station_rings(plan['anchor'])
    interior = blue | yellow | world.stations[0].cells
    if clock.phases != {'day'} or not miner or not miner.alive or miner.pos not in interior:
        state.miner_exit_clearer = None
    from .day_access import gate as access_gate
    gate = access_gate(world)
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

    def stage_worker():
        if worker.pos not in interior and distance(worker.pos,gate)>2:
            goals={q for q in neighbours(gate) if world.inside(q) and q not in interior}
            offer(worker,step(worker,goals,extra=interior),
                  'approach outside gate while pioneer enters; do not wait at distant worksite')
        else:
            hold(worker)

    def clear_worker_for(traveller, goals, *, inside=False, blocker=None, prefer_exterior=False):
        blocker = blocker or worker
        reach = distance_field(world,{blocker.pos},blocker.pos,deadline)
        unblocked = copy(world)
        unblocked.occupied = world.occupied-{blocker.pos}
        shortest = distance_field(unblocked,goals,traveller.pos,deadline).get(traveller.pos)
        if shortest is None:return None
        for q in sorted(reach,key=lambda q:((q in interior) if prefer_exterior else (q not in blue),
                                           reach[q],distance(q,gate),q)):
            if (q in interior and (not inside or q not in blue) or q in goals
                    or reach[q] > 6 or q == traveller.pos or q == blocker.pos):
                continue
            if (prefer_exterior and q not in interior
                    and reach[q]+shortest+min((distance(q,p) for p in stands(world,blocker.id)),
                                             default=float('inf'))
                    +policy.return_buffer >= clock.until_night):
                continue
            preview = copy(world)
            preview.occupied = (world.occupied - {blocker.pos}) | {q}
            passage = distance_field(preview,goals,traveller.pos,deadline)
            if (passage.get(traveller.pos,float('inf'))<=shortest+1
                    and time.monotonic() < deadline):
                if q in blue or (blocker.id == pioneer.id and traveller.id == (miner.id if miner else None)):
                    # Yield inside only when P's observed ingress remains
                    # open and W can subsequently reach its own gun with P
                    # occupying the final common stand. Do not trade one
                    # blocked corridor for another.
                    final = copy(preview)
                    final.occupied = (preview.occupied-{traveller.pos,q})|set(goals)
                    home = distance_field(final,stands(world,blocker.id),q,deadline)
                    if q not in home:
                        continue
                    if (blocker.id == pioneer.id and traveller.id == (miner.id if miner else None)
                            and reach[q]+passage[traveller.pos]+home[q]+policy.return_buffer >= clock.until_night):
                        continue
                if prefer_exterior and q not in interior:
                    # A material supplier can continue work outside after
                    # yielding, but must retain an observed return route once
                    # P occupies the common stand. Fall back inside if late.
                    final = copy(preview)
                    final.occupied = (preview.occupied-{traveller.pos,q})|set(goals)
                    home = distance_field(final,stands(world,blocker.id),q,deadline)
                    if (q not in home or reach[q]+passage[traveller.pos]+home[q]
                            +policy.return_buffer >= clock.until_night):
                        continue
                return step(blocker,{q})
            if time.monotonic() >= deadline:
                break
        return None

    def internal_guard_route():
        # In a nearly enclosed ring, neither guard can yield to the exterior.
        # Search the small shared interior together. Both may move only into
        # cells already free in the current state, never into a vacated cell.
        if (clock.phases != {'day'} or world.phase_task or pioneer.pos not in blue
                or worker.pos not in blue
                or any(u.alive and u.pos in blue for u in world.robots.values())):
            return None
        blocked = world.occupied - {worker.pos,pioneer.pos}
        free = blue - blocked
        destinations = stands(world,worker.id) & free
        start = (worker.pos,pioneer.pos)
        queue = deque([(start,None,0)])
        seen = {start}
        while queue and time.monotonic() < deadline:
            positions,first,length = queue.popleft()
            if positions[1] == plan['w'] and positions[0] in destinations:
                return first,length
            if length >= clock.until_night:
                continue
            options = [[positions[i]]+sorted(q for q in neighbours(positions[i])
                if q in free and q != positions[1-i]
                and q not in world.navigation_avoided.get(positions[i],set())) for i in (0,1)]
            for a in options[0]:
                for b in options[1]:
                    moved=(a,b)
                    if a==b or moved in seen:continue
                    seen.add(moved)
                    actions=first or tuple((actor,dict(action='move',targetPos=[pos_json(moved[i])]))
                        for i,actor in enumerate((worker,pioneer)) if moved[i]!=positions[i])
                    queue.append((moved,actions,length+1))
        return None

    if miner and miner.alive and miner.id not in world.night_defenders:
        from .opening_wave import dusk_exit
        command,forecast=dusk_exit(world,clock,miner,interior,deadline)
        if forecast:
            offer(miner,command,'leave previous opening-wave exposure before night')
            state.diagnostic['opening_wave_precaution']=forecast

    if pioneer.pos == plan['w'] and worker.pos in stands(world,worker.id):
        state.interior_clearance_day = None
    if (pioneer.pos == plan['w'] or worker.pos not in blue or clock.phases != {'day'}
            or state.pioneer_return_clearance_day != clock.day):
        state.pioneer_return_clearance_day = None
    if (pioneer.pos in blue and worker.pos in blue
            and (pioneer.pos != plan['w'] or state.interior_clearance_day == clock.day)):
        actual = distance_field(world,{plan['w']},pioneer.pos,deadline).get(pioneer.pos)
        if actual is None or state.interior_clearance_day == clock.day:
            joint = internal_guard_route()
            if joint:
                actions,length = joint
                if (state.interior_clearance_day == clock.day
                        or clock.until_night <= length+policy.return_buffer):
                    state.interior_clearance_day = clock.day
                    offer(worker,None,'wait for observed interior guard clearance')
                    offer(pioneer,None,'wait for observed interior guard clearance')
                    for actor,command in actions:
                        offer(actor,command,'coordinate guards through the enclosed interior')
                    state.diagnostic.update(stage='INTERIOR_GUARD_CLEARANCE',remaining_rounds=length)
                    return result

    # If W has no sealing material, M's personal stone can close from outside.
    # Reserve its actual walk before the build window ends, rather than first
    # considering that walk during the last fixed few daylight turns.
    wall_rule=rules.build_rule(world,'wall')
    if (clock.phases=={'day'} and enclosing and set(walls)==set(yellow)-{gate}
            and miner and miner.alive and worker and worker.alive and pioneer and pioneer.alive
            and miner.id not in world.night_defenders and miner.pos not in interior
            and not supplying and wall_rule and worker.backpack is not None
            and worker.inventory['stone']<wall_rule.items.get('stone',0)
            and miner.backpack is not None and (world.gold or 0)>=wall_rule.gold
            and all(miner.inventory[k]>=n for k,n in wall_rule.items.items())):
        goals=set(neighbours(gate))-interior
        route=distance_field(world,goals,miner.pos,deadline,extra_blocked=interior)
        length=route.get(miner.pos)
        if length is not None and length+1<=clock.until_night<=length+1+policy.return_buffer:
            if length:
                offer(miner,step(miner,goals,extra=interior),'return outside with personal final sealing stone')
            elif worker.pos in blue and pioneer.pos in blue and not world.phase_task:
                from .external_gate import valid_seal
                world.external_gate_permit=dict(m=miner.id,w=worker.id,p=pioneer.id,gate=gate,exterior_gap=True)
                world.seal_cells=frozenset({gate})
                if valid_seal(world,((miner.id,'wall',gate),)):
                    command=dict(action='build',name='wall',targetPos=[pos_json(gate)])
                    world.ordered_gate_actions[miner.id]=[command]
                    offer(miner,command,
                          'exterior worker seals observed guards with its own stone')
                    state.diagnostic.update(stage='EXTERIOR_SEAL_PENDING')
                    return result
            else:
                offer(miner,None,'wait outside for observed guards before final sealing')

    # One cashout per observed morning, completed only by an empty ore stock.
    if (clock.phases == {'day'} and clock.day > 1 and miner and miner.alive
            and miner.id not in world.night_defenders and miner.id not in state.commands):
        if state.cashout_day != clock.day:
            state.cashout_day = clock.day
            state.cashout_done = False
            state.cashout_loop_avoid = None
        if not state.cashout_done:
            stock = {k:miner.inventory[k] for k in MINERALS if miner.inventory[k]}
            if worker and worker.backpack is not None and stock.get('stone'):
                needed = max(0,len(set(world.wall_targets or ())-walls.keys())-worker.inventory['stone'])
                stock['stone'] = max(0,stock['stone']-needed)
                if not stock['stone']:stock.pop('stone')
            if miner.backpack is not None and not stock:
                state.cashout_done = True
                state.cashout_loop_avoid = None
            else:
                offer(miner, None, 'dawn cashout waits for observed inventory and vendor')
                quoted = {k:n for k,n in stock.items() if world.vendor.get(k,0)>0}
                if quoted and world.near_zone(miner.pos,'vendor'):
                    name = max(quoted,key=lambda k:(quoted[k]*world.vendor[k],k))
                    offer(miner,dict(action='sell',name=name,num=quoted[name]),'sell entire personal ore type after dawn')
                elif quoted:
                    goals=interaction_cells(world,world.zones.get('vendor',()),miner.pos)
                    avoid=state.cashout_loop_avoid
                    repeated=getattr(world,'observed_mover_cycles',{}).get(miner.id)
                    # Only an observed successful A-B-A-B walk authorizes a
                    # detour. Keep the same exclusion for this cashout, so a
                    # fresh shortest path cannot immediately undo the escape.
                    command=None
                    for point in dict.fromkeys(p for p in (avoid,repeated) if p is not None):
                        command=step(miner,goals,extra={point})
                        if command:
                            state.cashout_loop_avoid=point
                            break
                    if command is None:
                        state.cashout_loop_avoid=None
                        command=step(miner,goals)
                    offer(miner,command,'dawn miner detours around observed walking loop'
                          if state.cashout_loop_avoid is not None else
                          'dawn miner goes directly to merchant before any new job')
                    if state.cashout_loop_avoid is not None:
                        state.diagnostic['cashout_loop_avoid']=state.cashout_loop_avoid
                state.diagnostic['cashout'] = dict(actor=miner.id, stock=stock, done=False)
    if not worker or not worker.alive or not pioneer or not pioneer.alive:
        return result
    if (clock.phases == {'day'} and supplying and miner.alive
            and miner.id not in world.night_defenders and miner.id not in state.commands):
        from .night_resupply import deliver_after_dawn
        command=deliver_after_dawn(world,miner,rules,policy,deadline)
        delivery_observed=command is not None
        if command is None and miner.pos in interior:
            # A courier standing in the passage must not freeze the two
            # guards because its own use route is temporarily occupied.
            exits={q for p in yellow for q in neighbours(p) if world.inside(q) and q not in interior}
            command=step(miner,exits)
        if command:
            offer(miner,command,'deliver personal prepaid voucher' if delivery_observed else
                'clear blocked daylight passage before retrying personal voucher delivery')
        state.diagnostic['day_delivery']=dict(actor=miner.id,route_observed=delivery_observed,
            clearance=command is not None and not delivery_observed)

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
        if command and state.miner_exit_clearer == pioneer.id and not task_busy and not world.phase_task:
            return_path = distance_field(world,stands(world,pioneer.id),pioneer.pos,deadline)
            if return_path.get(pioneer.pos,float('inf'))+policy.return_buffer+1 < clock.until_night:
                offer(pioneer,None,'keep observed miner exit open until worker passes')
            else:
                state.miner_exit_clearer = None
        if not command:
            clearance = clear_worker_for(miner,exits)
            if clearance:
                offer(worker,clearance,'maintenance worker clears economy worker exit')
            elif not task_busy and not world.phase_task:
                clearance = clear_worker_for(miner,exits,inside=True,blocker=pioneer)
                if clearance:
                    offer(pioneer,clearance,'pioneer clears observed economy worker exit')
                    state.miner_exit_clearer = pioneer.id
        state.diagnostic.update(stage='MINER_EXIT',miner_outside_observed=False,
                                clearer=state.miner_exit_clearer)
        return result

    # A material-only return must not plug P's next daylight departure.
    # Prove W is the blocker and keep the clearance until P actually passes;
    # otherwise ordinary home planning immediately reoccupies the corridor.
    if (pioneer.pos not in interior or clock.phases != {'day'} or task_busy
            or world.phase_task or state.pioneer_exit_clearance_day != clock.day):
        state.pioneer_exit_clearance_day = None
    if (pioneer.pos in blue and worker.pos in blue and gate not in walls
            and not task_busy and not world.phase_task
            and (getattr(world,'worker_material_return',False)
                 or state.pioneer_exit_clearance_day == clock.day)):
        exits = {q for p in yellow for q in neighbours(p)
                 if world.inside(q) and q not in interior}
        unblocked = copy(world)
        unblocked.occupied = world.occupied-{worker.pos}
        shortest = distance_field(unblocked,exits,pioneer.pos,deadline).get(pioneer.pos)
        # This only releases the passage. Task/trade planners still admit the
        # complete service trip using their own deadlines and budgets.
        if shortest is not None and 2*shortest+policy.return_buffer+2 < clock.until_night:
            actual = distance_field(world,exits,pioneer.pos,deadline).get(pioneer.pos)
            if actual is None or actual > shortest+2:
                command = clear_worker_for(pioneer,exits,inside=True)
                if command:
                    offer(worker,command,'clear interior passage for pioneer daylight departure')
                    state.pioneer_exit_clearance_day = clock.day
            elif state.pioneer_exit_clearance_day == clock.day:
                hold(worker)
            if worker.id in state.commands:
                state.diagnostic.update(stage='PIONEER_EXIT_CLEARANCE')
                return result
        else:
            state.pioneer_exit_clearance_day = None

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
    if gate in walls and not missing:
        # Once closure is observed, release the temporary ingress holds. A
        # paid wall delivery may still fit inside the remaining daylight.
        state.stage='WORKER_SEALED'
        state.diagnostic['stage']=state.stage
        return result
    if clock.until_night > required:
        closing = not missing and getattr(world,'worker_close_requested',False)
        unfunded_worker = (worker.backpack is not None and not worker.inventory['stone']
            and not any(n and name.startswith('WeaponUpgradeVoucher') for name,n in worker.inventory.items()))
        task_ready_on_arrival = False
        task_view = copy(world)
        task_view.occupied = world.occupied-{worker.pos}
        for task in world.tasks:
            if task.get('isValid') is True:
                task_ready_on_arrival = True
                break
            cooldown = task.get('coldDownRounds')
            target = position(task.get('taskPosition'))
            if (type(cooldown) is not int or cooldown < 0 or target is None
                    or cooldown >= clock.until_night-policy.return_buffer):
                continue
            cells = interaction_cells(task_view,[target],pioneer.pos)
            arrival = distance_field(task_view,cells,pioneer.pos,deadline).get(pioneer.pos)
            if arrival is not None and cooldown <= arrival:
                task_ready_on_arrival = True
                break
        interior_delivery = (worker.pos in blue and worker.backpack is not None
                             and not worker.inventory['stone'] and task_ready_on_arrival)
        if (closing or unfunded_worker or interior_delivery) and pioneer.pos != plan['w'] and not world.phase_task:
            # W has finished the wall tour and is waiting for final closure.
            # Keep the entrance open now: a W blocking C also makes P's
            # checkout budget see no return route, freezing P at the shop.
            # Clear W without recalling P, so the remaining purchases finish.
            # Held weapon vouchers do not justify blocking a task's proven
            # return route; an interior yield can precede their local use.
            actual = distance_field(world,{plan['w']},pioneer.pos,deadline).get(pioneer.pos)
            without_worker = copy(world)
            without_worker.occupied = world.occupied-{worker.pos}
            unblocked = distance_field(without_worker,{plan['w']},pioneer.pos,deadline).get(pioneer.pos)
            # Attribute a detour to W only by removing W. The fully relaxed
            # field also removes M, whose construction transit must not cause
            # an unrelated W yield and a lost pioneer checkout action.
            if unblocked is not None and (actual is None or actual > unblocked+2):
                # An interior guard should first yield within the enclosure.
                # Exterior-only clearance can send it around a different open
                # wall, losing its otherwise feasible return to the turret.
                # clear_worker_for proves both P's passage and W's return with
                # P occupying the final stand before accepting an interior cell.
                # The interior proof covers the duty stand, not a prepaid
                # multi-building delivery tour. Preserve exterior clearance
                # for P's held coupons rather than obstructing their use tour.
                interior_clearance = worker.pos in blue and not any(
                    n and 'UpgradeVoucher' in name for name,n in pioneer.inventory.items())
                needs_material = bool(missing and worker.backpack is not None
                    and not worker.inventory['stone'] and not any(
                        n and 'UpgradeVoucher' in name for name,n in worker.inventory.items()))
                command = clear_worker_for(pioneer,{plan['w']},inside=interior_clearance,
                                           prefer_exterior=interior_clearance and needs_material)
                if command:
                    offer(worker,command,'open return corridor for pioneer checkout before dusk')
                    if interior_clearance:
                        state.pioneer_return_clearance_day = clock.day
                    # Do not let P step into W's only exit before this observed
                    # clearance completes, including when other walls lack stone.
                    offer(pioneer,None,'wait for observed worker passage clearance')
            elif state.pioneer_return_clearance_day == clock.day:
                # A successful yield is a passage commitment, not a one-turn
                # detour. Returning now would invalidate P's checkout route
                # again before P has observed and traversed the cleared cells.
                hold(worker)
            elif (unfunded_worker and getattr(world,'worker_material_return',False)
                    and not any(n and 'UpgradeVoucher' in name for name,n in worker.inventory.items())
                    and worker.pos not in blue and distance(worker.pos,gate)<=2):
                # A material-exhausted return has no work to perform inside.
                # Keep the observed clear corridor available until P enters;
                # otherwise the home planner walks W back into the same choke.
                # The due-ingress branch below releases this daytime wait.
                clearance = clear_worker_for(pioneer,{plan['w']}) if distance(worker.pos,gate)<=1 else None
                if clearance:
                    offer(worker,clearance,'stage material-exhausted worker clear of gate approach')
                else:
                    hold(worker)
            elif closing and worker.pos not in interior and distance(worker.pos,gate)<=2:
                # Waiting for P does not consume all remaining daylight.
                # Let W's complete circuit planner admit another useful trip;
                # it must keep waiting outside if no full trip fits.
                world.worker_checkout_wait = True
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
            stage_worker()
        else:
            # If the worker is already inside/on the passage, yield to a real
            # free tile that leaves both entry and final common stand reachable.
            offer(pioneer,None,'wait for worker clearance to be observed')
            command = clear_worker_for(pioneer,{plan['w']},inside=True)
            if command:
                offer(worker,command,'worker yields passage before pioneer enters')
            else:
                stage_worker()
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
