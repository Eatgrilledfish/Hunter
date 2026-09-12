"""Bounded task-point choice using public rewards and conservative time bounds.

Reward rates compare the full-correct scenario, not calibrated probabilities.
timeoutRounds is a holding-time bound, never an expected solver duration.
"""
import time

from .arbitration import Candidate
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import pos_json
from .director import exposure
from .task_timing import TaskTiming, descriptor


COLD_TASK_SOLVE_WINDOW = 20  # Strategy starting budget, not an official solver duration.


def solve_window(task, estimate):
    if estimate['source'] != 'deadline_fallback':
        return min(task['timeoutRounds'], estimate['duration'])
    return min(task['timeoutRounds'], COLD_TASK_SOLVE_WINDOW)


def home_cells(world, actor):
    plan = getattr(world, 'task_side_plan', None)
    targets = [plan['c']] if plan else [g.pos for g in world.weapons]
    if not targets:
        targets = [p for station in world.stations for p in station.cells]
    return interaction_cells(world, targets, actor.pos) if targets else set()


def can_accept(world, clock, policy, timing, actor, task, deadline):
    """Shared adjacent fallback guard, using the same real return and solve budget."""
    if clock.phases != {'day'} or type(task.get('timeoutRounds')) is not int or task['timeoutRounds'] <= 0:
        return False
    home = home_cells(world, actor)
    try:
        back = distance_field(world, home, actor.pos, deadline).get(actor.pos) if home else 0
    except TimeoutError:
        return False
    estimate = timing.estimate(descriptor(task, world.task_cells(task)), task['timeoutRounds'])
    return (back is not None and 1+solve_window(task, estimate)+back+policy.return_buffer <= clock.until_night
            and (not policy.task_full_timeout_guard_enabled
                 or 1+task['timeoutRounds']+back+policy.return_buffer <= clock.until_night))


def choose(world, clock, policy, deadline, timing=None):
    if (not policy.task_schedule_enabled or world.phase_task or not world.phase_task_observed
            or clock.phases != {'day'}):
        return None
    actors = [a for a in world.movers if a.kind == 'pioneer']
    if len(actors) != 1:
        return None
    actor = actors[0]
    tasks = [t for t in world.available_tasks if t.get('isValid') is True
             and type(t.get('coldDownRounds')) is int and t['coldDownRounds'] == 0
             and world.task_cells(t)]
    # Do not impute a missing reward, deadline, or valid task population.
    if any(type(t.get(k)) is not int or t[k] < (1 if k == 'timeoutRounds' else 0)
                        for t in tasks for k in ('scoreReward','goldReward','timeoutRounds')):
        return None
    # A cooling point can be approached only when its public descriptor still
    # supplies a positive task deadline. No 30-round reset is invented when
    # remaining cooldown/deadline is absent; re-evaluate fresh rewards each turn.
    tasks += [t for t in world.available_tasks if type(t.get('coldDownRounds')) is int
              and t['coldDownRounds'] > 0 and world.task_cells(t)
              and all(type(t.get(k)) is int and t[k] >= (1 if k=='timeoutRounds' else 0)
                      for k in ('scoreReward','goldReward','timeoutRounds'))]
    if not tasks:
        return None
    field = distance_field(world,[actor.pos],actor.pos,deadline)
    home = home_cells(world, actor)
    home_field = distance_field(world,home,actor.pos,deadline) if home else {}
    rows = []
    interaction = {id(t):interaction_cells(world,world.task_cells(t),actor.pos) for t in tasks}
    for task in tasks:
        estimate = (timing or TaskTiming()).estimate(descriptor(task, world.task_cells(task)), task['timeoutRounds'])
        solve_rounds = solve_window(task, estimate)
        # acceptTask has no target ID. Do not promise a choice from a cell
        # touching multiple advertised task points.
        cells = interaction[id(task)] - set().union(*(interaction[id(t)] for t in tasks if t is not task))
        routes = []
        for cell in sorted(cells & field.keys()):
            risk = exposure(world,clock,cell)
            if risk['unknown_robot_damage'] or risk['upper_per_attack_opportunity']*2 >= actor.health:
                continue
            travel = field[cell]
            back = home_field.get(cell) if home else 0
            duration = task['timeoutRounds']
            ready_in = max(travel,task['coldDownRounds'])
            # Avoid consuming a finite task immediately before the return.
            # Cold tasks reserve a declared strategy window, not their whole
            # possibly 120-round timeout; learned durations remain observations.
            if back is None or ready_in+1+solve_rounds+back+policy.return_buffer > clock.until_night:
                continue
            # Accept and finish use their own turns. Preserve the current
            # defence policy; conditional night release is a separate change.
            if policy.task_full_timeout_guard_enabled and clock.phases == {'day'} and home and (back is None or
                    ready_in+1+duration+back+policy.return_buffer > clock.until_night):
                continue
            routes.append((risk['upper_per_attack_opportunity'],travel,cell,back))
        if not routes:
            continue
        risk,travel,cell,back = min(routes)
        score,gold = task['scoreReward'],task['goldReward']
        wait = max(0,task['coldDownRounds']-travel)
        duration = estimate['duration']
        # Taskbook: standard rounds == timeoutRounds. This is a conditional
        # full-correct score, not a forecast of pass rate or spendable income.
        speed_bonus = 5 * task['timeoutRounds'] / duration
        utility = (score+speed_bonus+policy.task_gold_weight*gold)/(travel+wait+1+duration)
        rows.append(dict(task=task,goal=cell,travel=travel,return_rounds=back,risk=risk,
                         full_correct_rate=utility,holding_bound=task['timeoutRounds'],
                         full_hold_fits_day=(None if clock.phases != {'day'} or not home else
                             back is not None and travel+wait+1+task['timeoutRounds']+back+policy.return_buffer<=clock.until_night),
                         waiting_bound=wait,score_interval=[0,score+5*task['timeoutRounds']],
                         base_score_interval=[0,score],gold_interval=[0,gold],
                         minimum_solve_strategy_rounds=solve_rounds,
                         timing=estimate,full_correct_speed_bonus=speed_bonus))
    if time.monotonic() >= deadline:
        return None
    if not rows:
        return {'actor':actor.id,'selected':None,'candidates':[], 'reason':'no feasible bounded task route'}
    winner = min(rows,key=lambda r:(r['risk'],-r['full_correct_rate'],r['travel'],r['goal']))
    goal = winner['goal']
    candidates = []
    if goal != actor.pos:
        backfield = distance_field(world,[goal],actor.pos,deadline)
        if time.monotonic() >= deadline or actor.pos not in backfield:
            return None
        steps = [p for p in neighbours(actor.pos) if p in backfield and backfield[p]<backfield[actor.pos]]
        candidates = [Candidate(actor.id,{'action':'move','targetPos':[pos_json(p)]},
                                8/(1+winner['travel']*.1),'approach reward-ranked feasible task') for p in steps]
    return {'actor':actor.id,'selected':winner,'candidates':candidates,
            'reason':'full-correct score including stated speed bonus; submission latency scenario, correctness unknown'}
