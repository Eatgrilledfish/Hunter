"""One wall restoration policy. Observed losses are evidence, not robot DPS."""
def eligible(world, wall, rules, policy=None, *, service_steps=1, pressure=0):
    restored=getattr(world,'wall_restore_observations',{}).get(wall.id)
    if (restored and restored['level']==wall.level and wall.health is not None
            and wall.health>=restored['hp']):return False
    maximum = rules.max_health.get('wall', {}).get(wall.level)
    if wall.kind != 'wall' or not wall.alive or not maximum or wall.health is None or wall.health >= maximum:
        return False
    policy = policy or getattr(world, 'strategy_policy', None)
    fraction = getattr(policy, 'wall_repair_health_fraction', .5)
    if wall.health <= maximum * fraction:
        return True
    loss = getattr(world, 'observed_wall_losses', {}).get(wall.id, 0)
    return bool(loss > 0 and pressure > 0 and wall.health <= loss * max(1, service_steps))
