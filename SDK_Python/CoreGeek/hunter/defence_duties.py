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
    threats = [r for r in active(world) if r.target_team in (None, world.side)]
    damage = sum(2*r.attack_power for r in threats
                 if r.attack_power is not None and r.attack_range is not None
                 and distance(position, r.pos) <= r.attack_range)
    front = getattr(world, 'monster_front_walls', set())
    coverage = sum(distance(position, p) <= 1 for p in front)
    return (damage >= actor.health, -coverage, damage, walk, position)


def seal_service_steps(world):
    """A carried front-wall coupon needs a real post-seal use and return."""
    if not enabled(world):return 0
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


def ingress_reserve(world, policy, walk):
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
        lethal,coverage,damage,_,_=stand_rank(world,actor,point,0)
        options.append(dict(pos=point,occupied=point in world.occupied and point!=actor.pos,
            known_lethal=lethal,known_two_step_damage=damage,front_coverage=-coverage))
    return dict(actor=actor.id,current=actor.pos,selected=selected.get(actor.id),options=options,
        basis='known lethal exposure, wall coverage, known damage, reachable walking cost')
