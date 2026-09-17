"""Two forward Gatlings and one rocket, with persistent outward firing ports.

Enemy-base direction is a placement preference, not an assertion about spawn AI.
The legal yellow mask remains unchanged; wall_targets is a construction policy.
Existing weapons are retained, including incompatible older batteries.
"""
from collections import Counter
from functools import lru_cache

from .navigation import neighbours
from .protocol import MOBILE, WEAPONS, distance
from .rules import station_rings


def interior_usable(blue, blocked, guns):
    floor = set(blue)-set(blocked)-set(guns)
    if not floor or any(len(set(neighbours(g)) & floor) < 2 for g in guns):
        return False
    seen = {min(floor)}
    queue = list(seen)
    for point in queue:
        for p in neighbours(point):
            if p in floor and p not in seen:
                seen.add(p)
                queue.append(p)
    if seen != floor:
        return False
    states = {0}
    for p in floor:
        states |= {s | (1 << i) for s in list(states) for i,g in enumerate(guns)
                   if distance(p,g) <= 1 and not s & (1 << i)}
    return (1 << len(guns))-1 in states


@lru_cache(maxsize=128)
def _slots(anchor, direction, existing, blocked):
    blue, _ = station_rings(anchor)
    x,y = anchor
    corners = {(x-1,y-2),(x+2,y-2),(x-1,y+1),(x+2,y+1)}
    counts = Counter(name for name,p in existing)
    names = (['gatling']*max(0,2-counts['gatling']) + ['rocket']*max(0,1-counts['rocket']))[:max(0,3-len(existing))]
    dx,dy = direction
    # Projection chooses the facing edge; squared distance breaks corner ties.
    target = (x+dx,y+dy)
    order = sorted(blue-set(blocked)-{p for _,p in existing}, key=lambda p:(
        p not in corners, (p[0]-target[0])**2+(p[1]-target[1])**2,p))

    def complete(chosen, index):
        if index == len(names):
            return chosen if interior_usable(blue, blocked, [p for _,p in chosen]) else None
        name = names[index]
        candidates = order if name == 'gatling' else sorted(order,key=lambda p:(
            p not in corners,-((p[0]-target[0])**2+(p[1]-target[1])**2),p))
        for p in candidates:
            if p not in {q for _,q in chosen}:
                result = complete(chosen+((name,p),),index+1)
                if result is not None:
                    return result
        return None
    return complete(existing,0)


def prepare(world, rules, policy):
    _prepare_battery(world, rules, policy)
    from .wall_policy import prepare as prepare_walls
    prepare_walls(world, rules, policy)


def _prepare_battery(world, rules, policy):
    world.battery_plan = None
    world.wall_targets = None
    world.firing_ports = frozenset()
    from .task_side_layout import apply as apply_task_layout
    if apply_task_layout(world, rules, policy):
        return
    if policy is None or not policy.forward_battery_enabled or len(world.stations) != 1 or rules.weapon_limit != 3:
        return
    base = world.stations[0]
    blue,yellow = station_rings(base.pos)
    wall_rule = rules.build_rule(world,'wall')
    if not wall_rule or wall_rule.cells != yellow or any(not world.inside(p) for p in yellow):
        return
    if any(not (r := rules.build_rule(world,name)) or r.cells != blue for name in ('gatling','rocket')):
        return
    enemies = sorted((u for u in world.enemies.values() if u.kind == 'station'),key=lambda u:u.id)
    enemy = enemies[0].pos if len(enemies)==1 else ((world.width-1)/2,(world.height-1)/2)
    direction = (enemy[0]-base.pos[0],enemy[1]-base.pos[1])
    if direction == (0,0):
        return
    existing = tuple(sorted((u.kind,u.pos) for u in world.weapons))
    # Occupying workers must not shift future gun sites each turn. Construction
    # still checks real occupancy before issuing any action.
    blocked = frozenset(p for p in blue if any(p in cells for cells in world.zones.values()) or
        any(p in u.cells and u.kind not in MOBILE | WEAPONS and u.health != 0
            for u in (*world.ours.values(),*world.enemies.values())))
    slots = _slots(base.pos,direction,existing,blocked)
    if slots is None:
        return  # Do not force an unstaffable layout into an existing formation.
    ports = set()
    x,y = base.pos
    corners = {(x-1,y-2),(x+2,y-2),(x-1,y+1),(x+2,y+1)}
    for name,point in slots:
        if name != 'gatling' or point not in corners:
            continue
        # Default face ports preserve the yellow corner and offer wider lanes,
        # but CAN be entered diagonally around the gun on the eight-way map.
        # The optional corner port has only the gun as its blue neighbour.
        sx = -1 if point[0] == x-1 else 1
        sy = -1 if point[1] == y-2 else 1
        if policy.corner_battery_ports_enabled:
            ports.add((point[0]+sx,point[1]+sy))
        else:
            ports.update(((point[0]+sx,point[1]),(point[0],point[1]+sy)))
    world.build_interior = blue
    world.wall_targets = frozenset(yellow-ports)
    world.firing_ports = frozenset(ports)
    world.battery_plan = {'mode':'forward_2g1r_corner' if policy.corner_battery_ports_enabled else 'forward_2g1r',
        'enclosure':policy.corner_battery_ports_enabled,'direction_source':'enemy_base' if len(enemies)==1 else 'map_centre',
        'slots':slots,'ports':tuple(sorted(ports)),'wall_goal':len(world.wall_targets),
        'observed_mix':dict(Counter(u.kind for u in world.weapons))}


def cells(world, name, legal):
    if name == 'wall' and world.wall_targets is not None:
        return set(legal) & world.wall_targets
    if name in WEAPONS and world.battery_plan is not None:
        return set(legal) & {p for n,p in world.battery_plan['slots'] if n == name}
    return set(legal)


def missing_walls(world, rules):
    rule = rules.build_rule(world,'wall')
    if not rule:
        return set()
    return cells(world,'wall',rule.cells)-{u.pos for u in world.ours.values()
                                         if u.kind=='wall' and u.health != 0}


def construction_cells(world, name, legal):
    """Keep G in material demand but build it only in the sealing window.

    First-day front10 is deliberately unchanged, including a front-facing G.
    """
    result = cells(world, name, legal)
    if name == 'wall' and getattr(world, 'wall_stage', None) != 'front10':
        from .day_access import gate as access_gate
        gate = access_gate(world)
        if gate not in world.seal_cells:
            result.discard(gate)
    return result


def has_exit(world):
    """Actual fixed-structure reachability, ignoring mobile traffic only here."""
    if len(world.stations) != 1:
        return True
    blue,yellow = station_rings(world.stations[0].pos)
    region = blue | yellow | world.stations[0].cells
    blocked = {p for cells in world.zones.values() for p in cells}
    blocked.update(p for u in (*world.ours.values(),*world.enemies.values())
                   if u.kind not in MOBILE and u.health != 0 for p in u.cells)
    seen = set(blue)-blocked
    queue = list(seen)
    for point in queue:
        for p in neighbours(point):
            if p in blocked or p in seen or not world.inside(p):
                continue
            if p not in region:
                return True
            seen.add(p)
            queue.append(p)
    return False
