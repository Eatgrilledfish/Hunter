"""One-step cooldown handoff, recomputed from observed positions each turn."""
from itertools import product
import time

from .night_roles import operators
from .navigation import neighbours
from .protocol import distance


def advance(world, clock, stands, deadline, exposure, *, task_actor=None,
            allow_task_control=False, unavailable=(), failed_steps=None):
    if (clock.phases != {'night'} or not world.build_interior
            or any((clock.round-o) % 130 == 129 for o in clock.offsets)):
        return stands
    guns = [g for g in world.weapons if g.kind == 'rocket']
    if len(guns) > 3 or len(guns) != len(world.weapons) or any(g.cooldown is None or g.level not in (1,2,3) for g in guns):
        return stands
    guns = [g for g in guns if g.attack_range is not None and any(
        r.alive and distance(g.pos,r.pos) <= g.attack_range for r in world.robots.values())]
    if not any(g.cooldown == 1 for g in guns):
        return stands
    actors = operators(world, task_actor=task_actor, allow_task_control=allow_task_control)
    if len(actors) > 3:
        return stands
    points = {a.id:a.pos for a in actors}

    def capacity(positions, cooldown, excluded=()):
        # Ready guns can fire now and then cool down; they are not also
        # counted as tomorrow's new firing opportunities.
        available = [g for g in guns if g.cooldown == cooldown]
        masks = [[None]+[g.id for g in available if distance(p,g.pos)<=1]
                 for identity,p in positions.items() if identity not in excluded]
        values = {g.id:g.level for g in available}
        best = (0,0)
        for choice in product(*masks):
            used = [x for x in choice if x is not None]
            if len(set(used)) == len(used):
                best = max(best,(len(used),sum(values[x] for x in used)))
        return best

    ready = capacity(points,0)
    future = capacity(points,1)
    workers, choices = [], []
    for actor in actors:
        if (actor.kind != 'worker' or actor.id in unavailable or actor.health is None or actor.health <= 110
                or actor.pos not in world.build_interior or stands.get(actor.id) != actor.pos):
            continue
        risk = exposure(actor.pos)
        if risk['unknown_robot_damage'] or risk['upper_per_attack_opportunity']*2 >= actor.health:
            continue
        reserved = {p for identity,p in stands.items() if identity != actor.id}
        options = []
        for p in sorted(neighbours(actor.pos)):
            if p not in world.build_interior or p in world.occupied or p in reserved:
                continue
            if p in (failed_steps or {}).get(actor.id, ()):
                continue
            if not any(g.cooldown == 1 and distance(p,g.pos)<=1 for g in guns):
                continue
            after = exposure(p)
            if not after['unknown_robot_damage'] and after['upper_per_attack_opportunity'] <= risk['upper_per_attack_opportunity']:
                options.append(p)
        if options:
            workers.append(actor)
            choices.append([actor.pos]+options)
    best = None
    for destinations in product(*choices):
        if time.monotonic() >= deadline:
            return stands  # Do not publish a partially compared handoff.
        if len(set(destinations)) != len(destinations):
            continue
        changed = {a.id:p for a,p in zip(workers,destinations) if p != a.pos}
        if not changed or capacity(points,0,changed) < ready:
            continue  # Moving consumes the controller turn; never count both.
        after = capacity({**points,**changed},1)
        if after <= future:
            continue
        key = (-after[0],-after[1],len(changed),destinations)
        if best is None or key < best[0]:
            best = key,changed
    return {**stands,**best[1]} if best else stands
