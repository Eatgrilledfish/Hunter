"""Role identities stay stable; gun geometry is independent of unit kind."""


def enabled(world):
    return bool(getattr(getattr(world, 'strategy_policy', None), 'pioneer_rotation_enabled', False)
                and getattr(world, 'task_side_plan', None))


def rotator(world):
    roster = world.night_roster
    return (roster.m if roster.substituting else roster.p) if enabled(world) else roster.w


def caretaker(world):
    roster = world.night_roster
    return roster.w if enabled(world) else (roster.m if roster.substituting else roster.p)


def stands(world, identity):
    plan = world.task_side_plan
    return {plan['w']} if identity == rotator(world) else set(plan['c_stands']) - {plan['w']}


def stand_rank(world, actor, position, walk):
    """Keep C staffed from the inside of the monster-facing wall.

    This ranks already reachable legal gun cells. Known lethal exposure takes
    precedence, then wall coverage, then travel; it never invents a free cell.
    """
    from .protocol import distance
    from .robot_threats import active
    if not enabled(world) or actor.id != caretaker(world):
        return (walk, position)
    from .guard_risk import evidence
    risk=evidence(world,actor,position)
    damage=risk['two_opportunity_upper']
    front = getattr(world, 'monster_front_walls', set())
    coverage = sum(distance(position, p) <= 1 for p in front)
    # Prefer covering a wall whose observed loss is closing its service window.
    # This only ranks safe legal gun stands; it never authorizes a lethal move.
    losses=getattr(world,'observed_wall_losses',{})
    urgent=[u for u in world.ours.values() if u.alive and u.kind=='wall' and u.pos in front
            and losses.get(u.id,0)>0 and u.health<=losses[u.id]*5]
    missed=sum(distance(position,u.pos)>1 for u in urgent)
    return (risk['lethal'],missed,-coverage,damage if damage is not None else float('inf'),walk,position)


def seal_service_steps(world):
    """A carried front-wall coupon needs a real post-seal use and return."""
    if not enabled(world):return 0
    from .rear_open import enabled as rear_enabled
    if rear_enabled(world):return 0
    from .day_access import gate
    from .protocol import distance
    opening=gate(world)
    if opening not in getattr(world,'monster_front_walls',()):return 0
    if any(u.alive and u.kind=='wall' and u.pos==opening for u in world.ours.values()):return 0
    roster=getattr(world,'night_roster',None)
    actor=world.ours.get(rotator(world)) if roster else None
    if not actor or not actor.inventory['WallUpgradeVoucher1']:return 0
    # The rotator's actual stand is the origin of this short service excursion.
    return 2*max(0,distance(world.task_side_plan['w'],opening)-1)+1


def alternate_seal_tour(world, deadline):
    """Quote W's actual entry, seal and post-closure gun return with P in place."""
    from copy import copy
    import time
    from .day_access import gate
    from .navigation import distance_field, neighbours
    from .rules import station_rings
    plan = world.task_side_plan
    opening = gate(world)
    if opening == plan['gate']:
        return 0, set()
    cached = getattr(world, 'alternate_seal_quote', None)
    if cached is not None:return cached
    worker = world.ours.get(world.night_roster.w)
    pioneer = world.ours.get(rotator(world))
    if not worker or not pioneer:return float('inf'), set()
    blue, _ = station_rings(plan['anchor'])
    view = copy(world)
    view.occupied = (world.occupied - {worker.pos, pioneer.pos}) | {plan['w']}
    reach = distance_field(view, {worker.pos}, worker.pos, deadline)
    closed = copy(view)
    closed.occupied = view.occupied | {opening}
    home = distance_field(closed, stands(world, worker.id), worker.pos, deadline)
    entries = (set(neighbours(opening)) & blue) - {plan['w']}
    choices = [(reach[q] + 1 + home[q], q) for q in entries & reach.keys() & home.keys()]
    if time.monotonic() >= deadline or not choices:
        return float('inf'), set()
    cost = min(n for n, _ in choices)
    result = cost, {q for n, q in choices if n == cost}
    world.alternate_seal_phases = [(reach[q],1+home[q]) for _,q in choices]
    world.alternate_seal_quote = result
    return result


def ingress_reserve(world, policy, walk, deadline=float('inf')):
    """Use the same clearance window for task admission and ordered ingress."""
    from .rules import station_rings
    reserve = walk + policy.return_buffer
    if enabled(world):
        blue, yellow = station_rings(world.task_side_plan['anchor'])
        if set(world.wall_targets or ()) == yellow:
            from .day_access import gate
            opening = gate(world)
            walls = {u.pos for u in world.ours.values() if u.alive and u.kind == 'wall'}
            # Walking already includes the passage. Budget the observed final
            # seal and a worker yield, not another fixed perimeter traversal.
            reserve += int(opening is not None and opening not in walls)
            if opening is not None and opening not in walls and opening != world.task_side_plan['gate']:
                tour, _ = alternate_seal_tour(world, deadline)
                phases=getattr(world,'alternate_seal_phases',())
                if phases:
                    # Both exterior approaches proceed concurrently. Only
                    # W's seal and post-closure return follow P's ingress.
                    # The phase paths already exclude P's final occupied stand.
                    reserve += min(max(0,approach-walk)+tail-1 for approach,tail in phases)
                else:
                    reserve += max(0, tour - 1)  # Keep the conservative unknown-route fallback.
            roster = getattr(world, 'night_roster', None)
            worker = world.ours.get(roster.w) if roster else None
            pioneer = world.ours.get(rotator(world)) if roster else None
            reserve += int(bool(worker and pioneer and worker.pos in blue and pioneer.pos not in blue))
            reserve += seal_service_steps(world)
    return reserve


def service_diagnostic(world, selected):
    if not enabled(world):return None
    actor=world.ours.get(caretaker(world))
    if not actor:return None
    options=[]
    for point in sorted(stands(world,actor.id)):
        lethal,missed,coverage,damage,_,_=stand_rank(world,actor,point,0)
        options.append(dict(pos=point,occupied=point in world.occupied and point!=actor.pos,
            known_lethal=lethal,urgent_walls_uncovered=missed,known_two_step_damage=damage,front_coverage=-coverage))
    return dict(actor=actor.id,current=actor.pos,selected=selected.get(actor.id),options=options,
        basis='known lethal exposure, wall coverage, known damage, reachable walking cost')
