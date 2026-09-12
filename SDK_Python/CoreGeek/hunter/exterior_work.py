"""An already exterior worker can build, sell and mine without guard service."""
import time
from .arbitration import Candidate
from .navigation import neighbours
from .protocol import distance, pos_json
from .robot_threats import active
from .rules import station_rings
from .task_side_layout import _field, BudgetExpired


def propose(world, clock, rules, policy, deadline, *, keep_economy=False):
    plan=getattr(world,'task_side_plan',None)
    dusk=clock.phases=={'day'} and clock.until_night<=18
    if (clock.phases!={'night'} and not dusk) or not policy.night_foraging_enabled or not plan:
        return None,{}
    roster=world.night_roster
    m=world.ours.get(roster.m)
    blue,yellow=station_rings(plan['anchor'])
    interior=blue|yellow|world.stations[0].cells
    if not m or not m.alive or m.id in world.night_defenders or m.pos in interior or m.backpack is None:
        return None,{}
    report={'actor':m.id,'independent':True,'gate':plan['gate']}
    threats=active(world)
    if any(r.attack_range is None or r.attack_power is None for r in threats):
        return None,dict(report,reason='unknown robot kind prevents safe route')
    if any(r.attack_power>0 and distance(m.pos,r.pos)<=r.attack_range for r in threats):
        return None,dict(report,hold=True,reason='current tile exposed; escape takes priority')
    blocked=world.occupied|interior|world.navigation_avoided.get(m.pos,set())
    for robot in threats:
        if robot.attack_power<=0:continue
        radius=robot.attack_range
        for x in range(max(0,robot.pos[0]-radius),min(world.width,robot.pos[0]+radius+1)):
            if time.monotonic()>=deadline:raise BudgetExpired
            for y in range(max(0,robot.pos[1]-radius),min(world.height,robot.pos[1]+radius+1)):
                blocked.add((x,y))
    blocked.discard(m.pos)
    reach=_field(world,{m.pos},blocked,deadline)
    def step(goals):
        field=_field(world,set(goals),blocked,deadline)
        if m.pos not in field:return None
        choices=sorted(q for q in neighbours(m.pos) if field.get(q,float('inf'))<field[m.pos])
        return {'action':'move','targetPos':[pos_json(choices[0])]} if choices else None
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
    options=[]
    for name in ('stone','iron','copper'):
        if world.vendor.get(name,0)<=0:continue
        for mine in world.zones.get(name,()):
            for stand in neighbours(mine):
                if stand in reach:
                    actions=reach[stand]+1
                    options.append((-world.vendor[name]/actions,reach[stand],mine,stand))
    if not options:return None,dict(report,hold=True,reason='no safe reachable mine')
    _,travel,mine,stand=min(options)
    command=step({stand}) if travel else {'action':'collect','targetPos':[pos_json(mine)]}
    return candidate(command,'NIGHT_FORAGE',mine=mine,reason='mine until observed dawn') if command else (None,report)
