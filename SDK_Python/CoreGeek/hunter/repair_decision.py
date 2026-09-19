"""One wall restoration policy. Observed losses are evidence, not robot DPS."""
def eligible(world, wall, rules, policy=None, *, service_steps=1, pressure=0):
    from .rear_open import enabled
    clock=getattr(world,'strategy_clock',None)
    if enabled(world) and clock and clock.phases=={'day'} and wall.kind=='wall' and wall.level==1:
        return False  # Upgrade targets restore HP directly; other level-one walls use the rebuild transaction.
    restored=getattr(world,'wall_restore_observations',{}).get(wall.id)
    if (restored and restored['level']==wall.level and wall.health is not None
            and wall.health>=restored['hp']):return False
    maximum = rules.health_limit(world, wall)
    if wall.kind != 'wall' or not wall.alive or not maximum or wall.health is None or wall.health >= maximum:
        return False
    policy = policy or getattr(world, 'strategy_policy', None)
    fraction = getattr(policy, 'wall_repair_health_fraction', .3)
    if wall.health <= maximum * fraction:
        return True
    if premaintenance(world, wall, rules, policy):
        return True
    loss = getattr(world, 'observed_wall_losses', {}).get(wall.id, 0)
    return bool(loss > 0 and pressure > 0 and wall.health <= loss * max(1, service_steps))


def premaintenance(world, wall, rules, policy=None):
    """Daylight maintenance is separate from the emergency-use threshold."""
    from .rear_open import enabled
    clock=getattr(world,'strategy_clock',None)
    if not enabled(world) or not clock or clock.phases!={'day'}:
        return False
    maximum=rules.health_limit(world,wall) if wall.kind=='wall' else None
    if not wall.alive or not maximum or wall.health is None or wall.health>=maximum:
        return False
    history=getattr(world,'wall_pressure',{}).get(wall.pos,{})
    damage=history.get('damage',0)
    # Half-health front walls have no reliable next-wave service window.
    # This is a maintenance policy, never an invented robot damage forecast.
    return bool((wall.pos in getattr(world,'monster_front_walls',set())
                 and wall.health<=maximum*.5)
                or damage>0 and wall.health<=damage)


def weapons_maxed(world):
    """Only observed completion of all three weapon slots unlocks bulk stock."""
    return len(world.weapons) == 3 and all(u.alive and u.level == 3 for u in world.weapons)


def carry_target(clock, policy, used=0, unserved=0, *, world=None):
    return 26 if world is not None and weapons_maxed(world) else 3


def stock_target(world, policy):
    # A stock ceiling, not an instruction to spend before today's other orders.
    return 26 if weapons_maxed(world) else 3


def purchase_floor(world, policy):
    return 0 if weapons_maxed(world) else 3
