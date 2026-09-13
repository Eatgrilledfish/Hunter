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
