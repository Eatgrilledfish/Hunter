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


def ingress_reserve(world, policy, walk):
    """Use the same clearance window for task admission and ordered ingress."""
    from .rules import station_rings
    reserve = walk + policy.return_buffer
    if enabled(world):
        _, yellow = station_rings(world.task_side_plan['anchor'])
        if set(world.wall_targets or ()) == yellow:
            return max(18, reserve + 8)
    return reserve
