"""Shared purchasing responsibilities; bags and receipts remain personal."""

ATTACK_ITEMS = ('DizzyWeapon', 'Bomb')


def attack_targets(world):
    clock = getattr(world, 'strategy_clock', None)
    enabled = clock is not None and clock.day is not None and clock.day >= 5
    return dict(DizzyWeapon=2 if enabled else 0, Bomb=0)


def quantity_permitted(world, actor, name, count=1):
    if not permitted(world, actor, name):
        return False
    unit = world.ours.get(actor)
    if unit is None or unit.backpack is None:
        return True  # The protocol validator handles missing inventory evidence.
    if name == 'WallFixer':
        from .repair_decision import stock_target
        return unit.inventory[name]+count <= stock_target(world, None)
    if name in ATTACK_ITEMS:
        return (unit.inventory[name]+count <= attack_targets(world)[name]
                and count <= getattr(world, 'stun_purchase_remaining', 2))
    return True


def stun_ready(world, actor):
    return world.round >= getattr(world, 'stun_next_rounds', {}).get(actor, 0)


def owner(world, name):
    roster = getattr(world, 'night_roster', None)
    if roster is None:
        return None
    if name in ATTACK_ITEMS or name.endswith('SummonOrder'):
        return roster.p
    if name == 'WallFixer' or 'UpgradeVoucher' in name:
        return roster.w
    return None  # Medicine and task supplies retain their personal owners.


def permitted(world, actor, name):
    if name in ATTACK_ITEMS and attack_targets(world)[name] == 0:
        return False
    roster = getattr(world, 'night_roster', None)
    if roster is None:
        return True
    assigned = owner(world, name)
    if name in ATTACK_ITEMS or name.endswith('SummonOrder') or name == 'WallFixer' or 'UpgradeVoucher' in name:
        return assigned is not None and actor == assigned
    return True


def managed(world, actor):
    roster = getattr(world, 'night_roster', None)
    return roster is not None and actor in (roster.w, roster.p)


def publish(world, clock, policy):
    """One daily list with actual carrier, observed stock and current grants."""
    from .guard_stock import requirements
    rows = {}
    roster = world.night_roster
    for identity in (roster.w, roster.p):
        actor = world.ours.get(identity)
        if not actor or actor.backpack is None:
            continue
        for name, target in requirements(world, actor, policy):
            rows[identity, name] = dict(owner=identity, item=name, target=target,
                owned=actor.inventory[name], missing=max(0, target-actor.inventory[name]),
                granted=0, planned=0)
    for grant in getattr(world, 'funding_plan', ()):
        actor = world.ours.get(grant['owner'])
        if not actor or actor.backpack is None:
            continue
        remaining = grant.get('granted', 0)
        for name, count in grant['items'].items():
            row = rows.setdefault((actor.id, name), dict(owner=actor.id, item=name,
                target=actor.inventory[name], owned=actor.inventory[name], missing=0,
                granted=0, planned=0))
            row['planned'] += count
            allocated = min(count*world.shop.get(name, 0), remaining)
            row['granted'] += allocated
            remaining -= allocated
            row['missing'] = max(row['missing'], row['planned'])
            row['target'] = max(row['target'], row['owned']+row['missing'])
    world.daily_purchase_plan = dict(day=clock.day, round=world.round, items=list(rows.values()))
