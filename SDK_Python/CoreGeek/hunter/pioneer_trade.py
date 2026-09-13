"""Daytime trade after finite evolution work; inventories remain personal."""
import time

from .arbitration import Candidate
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import MINERALS, pos_json


def prepare(world, clock):
    world.strategy_clock = clock
    world.pioneer_trade_ids = set()
    if getattr(getattr(world, "strategy_policy", None), "pioneer_rotation_enabled", False):
        world.pioneer_trade_reason = "daytime tasks and treasure; worker owns maintenance trade"
        return
    # A late attachment may have no lifetime history. Current absence can still
    # release useful daytime work, without claiming six confirmed completions.
    listed = world.raw.get('teamOur', {}).get('playerTasks')
    exhausted = getattr(world, 'tasks_exhausted', False)
    ready=any(t.get('isValid') is True and type(t.get('coldDownRounds')) is int and t['coldDownRounds']==0 for t in world.available_tasks)
    absent = isinstance(listed, list) and not ready
    world.pioneer_trade_reason = 'six_task_lifecycles_consumed' if exhausted else 'no_currently_eligible_task' if absent else 'evolution_work_remaining'
    if clock.phases == {'day'} and world.phase_task_observed and not world.phase_task and (exhausted or absent):
        world.pioneer_trade_ids = {u.id for u in world.movers if u.kind == 'pioneer'}


def home_field(world, actor, deadline):
    stands = getattr(world, 'pioneer_trade_stands', {})
    if actor.id in stands:
        goals = {stands[actor.id]}
    elif getattr(world, 'task_side_plan', None):
        goals = set(world.task_side_plan['c_stands'])
    else:
        landmarks = [g.pos for g in world.weapons] or [p for b in world.stations for p in b.cells]
        goals = interaction_cells(world, landmarks, actor.pos)
    return distance_field(world, goals, actor.pos, deadline) if goals else {}


def delivery_field(world, actor, target, deadline):
    """Cost from each cell through an actual voucher use and back to duty."""
    from .day_schedule import weighted_field
    home = home_field(world, actor, deadline)
    ends = interaction_cells(world, [target.pos], actor.pos)
    return weighted_field(world, {p:home[p]+1 for p in ends if p in home}, actor, deadline) or {}


def return_margin(world, policy):
    return policy.return_buffer+(8 if world.defence_cells else 0)


def candidates(world, clock, policy, plans, deadline):
    result = []
    margin = return_margin(world, policy)
    for identity in sorted(getattr(world, 'pioneer_trade_ids', ())):
        if time.monotonic() >= deadline:
            break
        actor = world.ours[identity]
        if actor.backpack is None:
            continue
        home = home_field(world, actor, deadline)
        if actor.pos not in home or home[actor.pos]+margin >= clock.until_night:
            continue  # The director owns the due return, including blocked gates.
        owned = {k:actor.inventory[k] for k in MINERALS if actor.inventory[k] and world.vendor.get(k, 0)>0}
        if owned:
            sales = interaction_cells(world, world.zones.get('vendor', ()), actor.pos)
            start = distance_field(world, [actor.pos], actor.pos, deadline)
            feasible = [p for p in sales if p in start and p in home
                        and start[p]+len(owned)+home[p]+margin < clock.until_night]
            if feasible:
                stand = min(feasible, key=lambda p:(start[p]+home[p], start[p], p))
                if actor.pos == stand:
                    name = max(owned, key=lambda k:(owned[k]*world.vendor[k], k))
                    result.append(Candidate(identity, {'action':'sell', 'name':name, 'num':owned[name]}, 50,
                                            'pioneer merchant sells only personally carried ore'))
                else:
                    field = distance_field(world, [stand], actor.pos, deadline)
                    result.extend(_moves(actor, field, 'pioneer merchant cashes personal ore before shopping'))
                continue
        plan = plans.get(identity)
        if plan and plan['steps']+margin < clock.until_night:
            result.extend(plan['candidates'])
            continue
        # No affordable observed equipment is a reason to rejoin the battery,
        # not to wait forever at a vanished task point or an empty shop.
        result.extend(_moves(actor, home, 'pioneer merchant returns to duty while no funded trade is available'))
    return result


def _moves(actor, field, reason):
    length = field.get(actor.pos)
    if not length:
        return []
    steps = sorted(p for p in neighbours(actor.pos) if p in field and field[p]<length)
    return [Candidate(actor.id, {'action':'move', 'targetPos':[pos_json(p)]}, 35-i*.01, reason)
            for i,p in enumerate(steps[:4])]
