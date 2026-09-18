"""Finite two-point scheduling. Future descriptors are scenarios, not facts."""
import time
from .task_lifecycle import family, FAMILIES
from .task_timing import descriptor
from .navigation import distance_field, interaction_cells, neighbours


def priority(world, clock):
    life=getattr(world,'task_lifecycle',None)
    return bool(clock.phases=={'day'} and clock.day in (1,2) and life and not life.exhausted)


def rank(world, clock, policy, timing, rows, deadline):
    if not priority(world,clock) or not rows:
        return rows
    life=world.task_lifecycle
    actor=next(u for u in world.movers if u.kind=='pioneer')
    from .task_schedule import home_cells, task_return_reserve, solve_window
    offers={family(world,t):t for t in world.available_tasks
            if family(world,t) in FAMILIES and type(t.get('coldDownRounds')) is int
            and t['coldDownRounds']>=0 and type(t.get('timeoutRounds')) is int and t['timeoutRounds']>0}
    goals={k:interaction_cells(world,world.task_cells(t),actor.pos) for k,t in offers.items()}
    fields={k:distance_field(world,g,actor.pos,deadline) for k,g in goals.items()}
    home=home_cells(world,actor)
    to_home=distance_field(world,home,actor.pos,deadline)
    if time.monotonic()>=deadline:
        return rows
    horizon=min(clock.offsets)+200
    remaining={k:3-life.consumed[k] for k in FAMILIES}
    durations={k:solve_window(t,timing.estimate(descriptor(t,world.task_cells(t)),t['timeoutRounds']))
               for k,t in offers.items() if type(t.get('timeoutRounds')) is int and t['timeoutRounds']>0}
    def endpoint(field,pos):
        while field.get(pos,0)>0:
            steps=[q for q in neighbours(pos) if field.get(q,float('inf'))<field[pos]]
            if not steps:return None
            pos=min(steps,key=lambda q:(field[q],q))
        return pos if field.get(pos)==0 else None
    results=[]
    for row in rows:
        first=family(world,row['task'])
        finish=world.round+max(row['travel'],row['task']['coldDownRounds'])+1+row['minimum_solve_strategy_rounds']+1
        rem=dict(remaining);rem[first]-=1
        ready={k:world.round+max(0,t.get('coldDownRounds',0)) for k,t in offers.items()}
        ready[first]=finish+31
        best=[float('inf'),[]]
        prefix=[0,float('inf'),[]]
        def visit(now,pos,counts,cool,trace):
            if time.monotonic()>=deadline:return
            back=to_home.get(pos)
            end=now+task_return_reserve(world,policy,back,deadline) if back is not None else float('inf')
            if end<horizon and (len(trace)>prefix[0] or len(trace)==prefix[0] and end<prefix[1]):
                prefix[:]=[len(trace),end,trace]
            if not any(counts.values()):
                back=to_home.get(pos)
                if back is not None:
                    end=now+task_return_reserve(world,policy,back,deadline)
                    if end<best[0]:best[:]=[end,trace]
                return
            for k,n in counts.items():
                if n<=0 or k not in durations or pos not in fields[k]:continue
                target=endpoint(fields[k],pos)
                if target not in to_home:continue
                begin=max(now+fields[k][pos],cool[k])
                done=begin+durations[k]+2
                night=min(o+((begin-o)//130)*130+70 for o in clock.offsets)
                reserve=task_return_reserve(world,policy,to_home[target],deadline)
                if done+reserve>=night:
                    next_day=night+60+1
                    base=min((p for p in home if p in fields[k]),key=lambda p:(fields[k][p],p),default=None)
                    if base is None:continue
                    target=endpoint(fields[k],base)
                    if target not in to_home:continue
                    reserve=task_return_reserve(world,policy,to_home[target],deadline)
                    begin=max(next_day+fields[k][base],cool[k]);done=begin+durations[k]+2
                if done+reserve>=horizon:continue
                left=dict(counts);left[k]-=1
                refresh=dict(cool);refresh[k]=done+31
                visit(done,target,left,refresh,trace+[(k,begin,done)])
        visit(finish,row['goal'],rem,ready,[(first,finish-row['minimum_solve_strategy_rounds']-2,finish)])
        row['six_task_finish_scenario']=best[0] if best[0]!=float('inf') else None
        row['six_task_sequence']=best[1]
        row['tasks_before_deadline_scenario']=prefix[0]
        row['partial_sequence_scenario']=prefix[2]
        row['partial_finish_with_return']=prefix[1] if prefix[1]!=float('inf') else None
        results.append(row)
    forecast=min((r['six_task_finish_scenario'] for r in results if r['six_task_finish_scenario'] is not None),default=None)
    world.six_task_deadline=dict(deadline=horizon-1,forecast_with_return=forecast,
        status='conditional_schedule' if forecast is not None and forecast<horizon else 'deadline_at_risk',
        consumed=dict(life.consumed),official_success_count=None,
        ended_count=len(life.events),timeout_count=sum('timeout' in e['reason'] for e in life.events),
        assumptions='future task descriptors UNKNOWN; observed public duration scenario, revalidate each accept')
    return results
