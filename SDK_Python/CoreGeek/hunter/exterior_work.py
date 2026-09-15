"""An already exterior worker can build, sell and mine without guard service."""
import time
from .arbitration import Candidate
from .navigation import neighbours
from .protocol import distance, pos_json
from .robot_threats import active
from .rules import station_rings
from .task_side_layout import _field, BudgetExpired


def _step_from_reach(origin, goals, reach, deadline):
    """Recover the same lexicographic first step without a second flood fill."""
    lengths=[reach[p] for p in goals if p in reach]
    if not lengths or min(lengths)==0:return None
    depth=min(lengths)
    frontier={p for p in goals if reach.get(p)==depth}
    while depth>1:
        if time.monotonic()>=deadline:raise BudgetExpired
        previous=set()
        for index,p in enumerate(frontier):
            if index%32==0 and time.monotonic()>=deadline:raise BudgetExpired
            previous.update(q for q in neighbours(p) if reach.get(q)==depth-1)
        frontier=previous;depth-=1
    return min(frontier) if frontier else None


def propose(world, clock, rules, policy, deadline, *, keep_economy=False, allow_resupply=True):
    plan=getattr(world,'task_side_plan',None)
    dusk=clock.phases=={'day'} and clock.until_night<=18
    if (clock.phases!={'night'} and not dusk) or not policy.night_foraging_enabled or not plan:
        return None,{}
    roster=world.night_roster
    m=world.ours.get(roster.m)
    blue,yellow=station_rings(plan['anchor'])
    interior=blue|yellow|world.stations[0].cells
    if (not m or not m.alive or m.id in world.night_defenders or m.backpack is None
            or m.pos in interior and (dusk or not policy.pioneer_rotation_enabled)):
        return None,{}
    report={'actor':m.id,'independent':True,'gate':plan['gate']}
    threats=active(world)
    if any(r.attack_range is None or r.attack_power is None for r in threats):
        return None,dict(report,reason='unknown robot kind prevents safe route')
    if any(r.attack_power>0 and distance(m.pos,r.pos)<=r.attack_range for r in threats):
        return None,dict(report,hold=True,reason='current tile exposed; escape takes priority')
    blocked=world.occupied|world.navigation_avoided.get(m.pos,set())
    for robot in threats:
        if robot.attack_power<=0:continue
        # Low-health economic movement must retain the same pursuit margin
        # as treatment routes, including a fallback from an unavailable shop.
        # Healthy workers keep the original productive range boundary.
        radius=robot.attack_range + int(m.health <= 110)
        for x in range(max(0,robot.pos[0]-radius),min(world.width,robot.pos[0]+radius+1)):
            if time.monotonic()>=deadline:raise BudgetExpired
            for y in range(max(0,robot.pos[1]-radius),min(world.height,robot.pos[1]+radius+1)):
                blocked.add((x,y))
    blocked.discard(m.pos)
    if clock.phases == {'night'} and policy.pioneer_rotation_enabled and allow_resupply:
        from .night_resupply import propose as resupply
        # Resolve a fresh safe mining action before optional multi-stop shopping.
        # Never reuse a previous round's move after occupancy/threats changed.
        fallback, fallback_report = None, {}
        if m.pos not in interior:
            try:
                fallback,fallback_report=propose(world,clock,rules,policy,
                    min(deadline,time.monotonic()+.015),allow_resupply=False)
            except BudgetExpired:
                fallback_report={'reason':'safe forage planning budget exhausted'}
                # Timeout means the route comparison is unknown, not that
                # mining lost to shopping. Keep a freshly confirmed local
                # collect without searching or reusing any prior move.
                mine=getattr(world,'observed_collect_targets',{}).get(m.id)
                if (clock.day is not None and clock.day<10 and mine is not None
                        and distance(m.pos,mine)<=1 and m.capacity is not None
                        and len(m.backpack)<m.capacity
                        and any(mine in world.zones.get(name,()) for name in ('stone','iron','copper'))
                        and not (m.health<=110 and any(r.attack_power>0 and
                            distance(m.pos,r.pos)<=r.attack_range+1 for r in threats))):
                    fallback=Candidate(m.id,dict(action='collect',targetPos=[pos_json(mine)]),1050,
                        'exterior worker: finish confirmed adjacent mine during planning fallback')
                    fallback_report=dict(report,stage='NIGHT_FORAGE',mine=mine,mine_stand=m.pos,
                        mine_travel_steps=0,planning_budget_exhausted=True,
                        reason='current successful collect remains feasible without route search')
                elif (m.health>110 and not any(n and 'UpgradeVoucher' in name
                        for name,n in m.inventory.items())
                        and not getattr(world,'critical_base_ids',())):
                    # A healthy uncommitted miner's primary route gets the
                    # remaining existing allowance before optional shopping.
                    # An unfinished comparison cannot justify reversing course
                    # for a new purchase. Never extend the caller's deadline.
                    try:
                        fallback,fallback_report=propose(world,clock,rules,policy,
                            deadline,allow_resupply=False)
                        fallback_report=dict(fallback_report,initial_forage_budget_exhausted=True)
                    except BudgetExpired:
                        return None,dict(report,stage='NIGHT_FORAGE_REPLAN',hold=True,
                            reason='unfinished primary route cannot authorize optional supply detour')
        try:
            command, supply_report = resupply(world,clock,rules,policy,m,blocked,deadline)
        except BudgetExpired:
            command,supply_report=None,{'stage':'NIGHT_SUPPLY_BUDGET'}
        if command:
            action=command.command.get('action')
            held_delivery=any(n and 'UpgradeVoucher' in name for name,n in m.inventory.items())
            # A dawn-only coupon adds a real return obligation. Buying beside
            # the shop is useful only if the current forage trip can still
            # collect and return, rather than turn around just before mining.
            dawn_prebuy_fits=False
            if (action=='buy' and supply_report.get('stage')=='NIGHT_DAWN_PREBUY'
                    and fallback_report.get('stage')=='NIGHT_FORAGE'):
                stand=fallback_report.get('mine_stand')
                travel=fallback_report.get('mine_travel_steps')
                if stand is not None and travel is not None:
                    try:
                        gate_ends={p for p in neighbours(plan['gate'])
                                   if world.inside(p) and p not in interior|blocked}
                        back=_field(world,gate_ends,blocked|interior,deadline)
                        return_steps=back.get(tuple(stand))
                        remaining=min(130-(clock.round-o)%130 for o in clock.offsets)
                        # Ordinary next-day stock must leave time for the
                        # configured economic batch, not merely one ore after
                        # a long outbound and delivery-return walk. This is a
                        # work budget, never an assumed deposit size.
                        free_after_buy=max(0,m.capacity-len(m.backpack)-command.command.get('num',1))
                        batch=min(policy.sell_batch,free_after_buy)
                        required=(None if return_steps is None or batch<=0 else
                                  1+travel+batch+return_steps+policy.return_buffer+2)
                        dawn_prebuy_fits=required is not None and required<=remaining
                        supply_report=dict(supply_report,forage_return_required=required,
                                           forage_batch_actions=batch,night_remaining=remaining)
                    except BudgetExpired:
                        pass
            immediate=(action=='use' or action=='buy' and world.near_zone(m.pos,'weaponShop')
                       and (supply_report.get('stage')!='NIGHT_DAWN_PREBUY' or dawn_prebuy_fits)
                       or action=='sell' and world.near_zone(m.pos,'vendor'))
            treatment=(m.health<=110 and supply_report.get('item')=='Medicine')
            committed=held_delivery and supply_report.get('stage') in {
                'NIGHT_UPGRADE_DELIVER','NIGHT_WAIT_DAWN_DELIVERY'}
            if (fallback and fallback_report.get('stage')=='NIGHT_FORAGE'
                    and m.pos not in interior and not immediate and not treatment
                    and not committed and not getattr(world,'critical_base_ids',())):
                # A safe productive batch has no reason to pay a new long
                # shopping detour. Already paid delivery and rescue retain
                # priority; shopping encountered on the route remains useful.
                return fallback,dict(fallback_report,supply=supply_report,
                    fallback_reason='continue safe ore work before optional supply detour')
            if fallback is None and fallback_report:
                supply_report=dict(supply_report,forage_planning=fallback_report)
            world.night_resupply_commands = {m.id:[command.command]}
            return command,supply_report
        if supply_report.get('hold'):return None,supply_report
        report['supply'] = supply_report
        if fallback:
            return fallback,dict(fallback_report,supply=supply_report,
                fallback_reason='no completed higher-priority supply action')
        if m.pos in interior:
            outside={(x,y) for x in range(world.width) for y in range(world.height)
                     if (x,y) not in interior|blocked}
            exit_route=_field(world,outside,blocked,deadline)
            steps=sorted(q for q in neighbours(m.pos) if exit_route.get(q,float('inf'))<exit_route.get(m.pos,0))
            if steps:
                return Candidate(m.id,dict(action='move',targetPos=[pos_json(steps[0])]),1050,
                                 'night supplier leaves through observed free path'),dict(report,stage='NIGHT_SUPPLY_EXIT')
            return None,dict(report,hold=True,reason='no observed safe exit from completed delivery')
    blocked |= interior-{m.pos}
    reach=_field(world,{m.pos},blocked,deadline)
    def step(goals):
        point=_step_from_reach(m.pos,set(goals),reach,deadline)
        return {'action':'move','targetPos':[pos_json(point)]} if point is not None else None
    def candidate(command,stage,**detail):
        return Candidate(m.id,command,1050,'exterior worker: '+stage),dict(report,stage=stage,**detail)
    # Complete observed gaps even when the other nineteen walls are not all
    # present. A full seal still requires both defenders actually inside.
    walls={u.pos for u in world.ours.values() if u.alive and u.kind=='wall'}
    targets=set(getattr(world,'wall_targets',yellow))-walls
    w,p=(world.ours.get(i) for i in (roster.w,roster.p))
    inside=all(u and u.alive and u.pos in blue for u in (w,p))
    wall_rule=rules.build_rule(world,'wall')
    funded=wall_rule and (world.gold or 0)>=wall_rule.gold and all(m.inventory[k]>=n for k,n in wall_rule.items.items())
    if dusk and funded and not world.phase_task:
        jobs=[]
        for target in targets-world.occupied:
            for stand in neighbours(target):
                if stand in reach and reach[stand]+1<=clock.until_night:
                    jobs.append((target!=plan['gate'],reach[stand],target,stand))
        if jobs:
            _,travel,target,stand=min(jobs)
            if travel:
                command=step({stand})
            else:
                if yellow<=walls|{target} and not inside:
                    return None,dict(report,stage='DUSK_WAIT_GUARDS',reason='last gap waits for observed defenders inside')
                world.external_gate_permit=dict(m=m.id,w=roster.w,p=roster.p,gate=target,exterior_gap=True)
                world.seal_cells=frozenset({target})
                command={'action':'build','name':'wall','targetPos':[pos_json(target)]}
            if command:return candidate(command,'DUSK_CLOSE_GAP',target=target,steps=travel)
    if dusk:return None,report
    stock={name:m.inventory[name] for name in ('stone','iron','copper') if world.vendor.get(name,0)>0 and m.inventory[name]}
    if 'stone' in stock:
        stock['stone']=max(0,stock['stone']-max(len(targets),1 if clock.day<10 else 0))
        if not stock['stone']:stock.pop('stone')
    value=sum(world.vendor[name]*count for name,count in stock.items())
    if not m.capacity or len(m.backpack)>=m.capacity:
        return None,dict(report,hold=True,reason='bag full; await dawn cashout',quoted_stock_value=value)
    # The next enclosing-ring work needs actual stone, not projected copper
    # proceeds. M can gather its own non-gate share overnight; W's gate stone
    # remains W's responsibility. This is a stock target, not a promised build.
    next_missing=yellow-walls if getattr(world,'staged_walls',False) else targets
    non_gate=next_missing-{plan['gate']}
    stone_cost=wall_rule.items.get('stone',0) if wall_rule else 0
    worker_stock=max(0,w.inventory['stone']-stone_cost*int(plan['gate'] in next_missing)) if w and w.backpack is not None else 0
    material_deficit=max(0,len(non_gate)*stone_cost-worker_stock-m.inventory['stone']) if clock.phases=={'night'} and clock.day<10 else 0
    night_left=min(130-(clock.round-o)%130 for o in clock.offsets)
    material_actions=min(material_deficit,m.capacity-len(m.backpack))
    vendors={q for v in world.zones.get('vendor',()) for q in neighbours(v)
             if world.inside(q) and q not in blocked}
    sale_field=_field(world,vendors,blocked,deadline)
    ongoing=getattr(world,'observed_collect_targets',{}).get(m.id)
    options=[]
    for name in ('stone','iron','copper'):
        price=world.vendor.get(name,0)
        if price<=0:continue
        for mine in world.zones.get(name,()):
            for stand in neighbours(mine):
                if stand not in reach:continue
                travel=reach[stand]
                night_material=bool(material_actions and name=='stone'
                                    and travel+material_actions<=night_left)
                sale_steps=sale_field.get(stand)
                batch=min(policy.sell_batch,m.capacity-len(m.backpack),night_left-travel)
                if clock.day>=10 and sale_steps is not None:
                    batch=min(batch,night_left-travel-sale_steps-1)
                if night_material:
                    value=price/(travel+1);rank=0
                elif sale_steps is not None and batch>0:
                    value=price*batch/(travel+batch+sale_steps+1)
                    rank=1 if mine==ongoing else 2
                elif batch>0 and clock.day<10:
                    # Safe stock remains a fallback when a future daylight
                    # sale route is unknown. Do not label this as monetized
                    # income or let optional shopping win only by omission.
                    value=price*batch/(travel+batch)
                    rank=1 if mine==ongoing else 3
                else:continue
                options.append((rank,-value,travel,mine,stand,name,batch,sale_steps))
    if not options:
        # Dawn already requires liquidation of existing saleable stock. When
        # no mining batch fits tonight, use otherwise idle movement to reach
        # that observed vendor; leave the sale to the actual dawn quote.
        # No next-day benefit exists on the final night.
        if clock.day<10 and stock:
            choices=sorted((reach[q],q) for q in vendors if q in reach)
            if choices:
                travel,stand=choices[0]
                if travel:
                    command=step({stand})
                    if command:
                        return candidate(command,'NIGHT_DAWN_POSITION',vendor_stand=stand,
                            vendor_travel_steps=travel,quoted_stock_value=value,
                            reason='no night batch fits; preposition for observed dawn cashout')
                else:
                    return None,dict(report,hold=True,stage='NIGHT_DAWN_READY',
                        reason='at vendor; await actual dawn quote for existing stock')
        return None,dict(report,hold=True,reason='no useful safe reachable mine within remaining night')
    ordinary,_,travel,mine,stand,name,batch,sale_steps=min(options)
    command=step({stand}) if travel else {'action':'collect','targetPos':[pos_json(mine)]}
    return candidate(command,'NIGHT_FORAGE',mine=mine,mine_stand=stand,mine_travel_steps=travel,
        material_deficit=material_deficit,quoted_batch_actions=batch,sale_travel_steps=sale_steps,
        reason='gather personal stone within remaining night for next-day wall gaps' if not ordinary
        else 'finish observed active mine' if ordinary==1 else 'collect safe stock; sale route not observed' if ordinary==3 else 'compare batch value including observed sale route') if command else (None,report)
