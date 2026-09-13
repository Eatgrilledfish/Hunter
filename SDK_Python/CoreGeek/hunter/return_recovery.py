"""Recover the fixed two-gun worker through observed walls, one action at a time."""
from heapq import heappop, heappush
from copy import copy
import time
from .navigation import distance_field, neighbours
from .protocol import pos_json
from .robot_threats import active
from .arbitration import Candidate


def propose(world, clock, policy, deadline):
    world.return_recovery_actions = {}
    world.return_recovery_cells = set()
    roster = world.night_roster
    plan = getattr(world, 'task_side_plan', None)
    worker = world.ours.get(roster.w)
    report = {'stage':'inactive'}
    from .defence_duties import stands
    if not plan or not worker or not worker.alive or worker.pos in stands(world, worker.id):
        roster.return_recovery = {}
        return [], report
    pending = roster.return_recovery
    # Keep confirmed/planned openings reserved even on a traffic hold or a
    # budget-limited frame. Replanning failure must not authorize rebuilding
    # the doorway before the worker has actually reached its stand.
    world.return_recovery_cells.update(pending.get('openings', ()))
    if roster.traffic or roster.exit_pending or worker.id == getattr(world, 'task_actor', None):
        return [], report
    if clock.phases == {'day'} and clock.until_night > 20 and not pending:
        return [], report
    from .defence_duties import stands
    targets = stands(world, worker.id)
    target = min(targets, key=lambda q: max(abs(q[0]-worker.pos[0]), abs(q[1]-worker.pos[1])))
    # A reachable ordinary route needs no demolition. An ongoing recovery owns
    # that route until actual arrival, so builders cannot close its new opening.
    field = distance_field(world, {target}, worker.pos, deadline)
    if worker.pos in field and not pending:
        return [], report
    if not pending:
        topology=copy(world)
        topology.occupied=world.occupied-{u.pos for u in world.movers}
        if worker.pos in distance_field(topology,{target},worker.pos,deadline):
            return [], {'stage':'traffic','actor':worker.id,'target':target}
    walls = {u.pos:u for u in world.ours.values() if u.alive and u.kind == 'wall'}
    forbidden = world.occupied - set(walls) - {worker.pos}
    threats = active(world)
    if any(r.attack_range is None or r.attack_power is None for r in threats):
        return [], {'stage':'blocked', 'reason':'unknown exposure on demolition route'}
    # Minimize removed walls, then exposure and travel. Two openings suffice
    # for leaving one camp compartment and entering the assigned compartment.
    queue = [(0, 0, worker.pos, 0, ())]
    best = {}
    while queue:
        if time.monotonic() >= deadline:
            return [], {'stage':'blocked', 'reason':'demolition route budget'}
        removed, cost, point, harm, path = heappop(queue)
        key = point, removed
        if cost >= best.get(key, float('inf')):continue
        best[key] = cost
        if point == target:
            if not path:return [], report
            next_cell = path[0]
            command = {'action':'remove' if next_cell in walls else 'move', 'targetPos':[pos_json(next_cell)]}
            world.return_recovery_cells = set(path) | set(pending.get('openings', ()))
            openings = set(pending.get('openings', ())) | (set(path) & walls.keys())
            roster.return_recovery = {'openings':sorted(openings), 'target':target}
            world.return_recovery_actions = {worker.id:[command]}
            return [Candidate(worker.id,command,1500,'recover blocked return to two-gun stand')], {
                'stage':command['action'], 'actor':worker.id, 'target':target,
                'next':next_cell, 'walls':removed, 'steps':len(path), 'estimated_damage':harm}
        for q in neighbours(point):
            if not world.inside(q) or q in forbidden or q in path:continue
            n = removed + int(q in walls)
            if n > 2:continue
            damage = sum(r.attack_power for r in threats
                         if max(abs(q[0]-r.pos[0]),abs(q[1]-r.pos[1])) <= r.attack_range)
            damage *= 2 if q in walls else 1
            if harm + damage >= max(1, worker.health//2):continue
            heappush(queue,(n,cost+1+int(q in walls)+damage*4,q,harm+damage,path+(q,)))
    return [], {'stage':'blocked','reason':'no nonlethal route through at most two own walls','target':target}
