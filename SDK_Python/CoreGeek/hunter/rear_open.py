"""Half-game rear opening contract; geometry never asserts combat safety."""
import time

from .navigation import neighbours, distance_field, interaction_cells
from .protocol import MOBILE, distance
from .rules import station_rings


MODE = 'rear_open_three_rockets'


def enabled(world):
    return (getattr(world, 'task_side_plan', None) or {}).get('layout_mode') == MODE


def openings(world):
    return set(world.task_side_plan['permanent_openings']) if enabled(world) else set()


def required(world):
    return set(world.task_side_plan['required_wall_cells'])


def select(world, rules, deadline):
    from .wall_policy import monster_direction, front_cells
    if len(world.stations) != 1 or rules.weapon_limit != 3:
        return None, 'unknown_region'
    anchor = world.stations[0].pos
    blue, yellow = station_rings(anchor)
    if (any(not world.inside(p) for p in blue | yellow)
            or any(not (r := rules.build_rule(world, name)) or r.cells != cells
                   for name, cells in (('rocket', blue), ('wall', yellow)))):
        return None, 'unknown_region'
    direction = monster_direction(world, anchor)
    def transform(x, y):
        return ((anchor[0]+x, anchor[1]+y) if direction == 1 else
                (anchor[0]+1-x, anchor[1]-1-y))
    rear = {transform(-2, y) for y in range(-3, 3)}
    walls = yellow - rear
    service = {transform(2, y) for y in range(-2, 2)}
    static = {p for cells in world.zones.values() for p in cells}
    static.update(p for u in (*world.ours.values(), *world.enemies.values())
                  if u.kind not in MOBILE and u.blocks for p in u.cells)
    gun_sites = {g.pos for g in world.weapons}
    # Existing rear walls, foreign objects and incompatible weapons cannot be
    # wished away when adopting an in-progress game.
    if rear & static or any(g.kind != 'rocket' for g in world.weapons):
        return None, 'incompatible_existing_structure'
    rows = []
    for low in (-2, -1):
        guns = tuple(transform(-1, y) for y in range(low, low+3))
        p = transform(-2, low+1)
        if not gun_sites <= set(guns) or (set(guns) | service) & (static-gun_sites):
            continue
        from copy import copy
        view = copy(world)
        view.occupied = static | set(guns) | walls
        # A complete topology proof includes the permanently open yellow side.
        reach = distance_field(view, {p}, p, deadline)
        if not service <= reach.keys():
            continue
        blocked_p = copy(view)
        blocked_p.occupied = view.occupied | {p}
        exits = {q for r in rear for q in neighbours(r)
                 if world.inside(q) and q not in blue | yellow}
        out = distance_field(blocked_p, exits, next(iter(service)), deadline)
        if not service <= out.keys():
            continue
        trips = []
        for targets in [world.task_cells(t) for t in world.tasks] + [
                world.zones.get('weaponShop', ()), world.zones.get('vendor', ())]:
            if not targets:
                continue
            goals = interaction_cells(view, targets, p)
            lengths = [reach[q] for q in goals if q in reach]
            if not lengths:
                break
            trips.append(min(lengths))
        else:
            if all(u.pos in distance_field(view, {p} if u.kind == 'pioneer' else service,
                                          u.pos, deadline) for u in world.movers):
                risk = sum(r.attack_power for r in world.robots.values() if r.alive
                           and r.attack_power is not None and r.attack_range is not None
                           and distance(p, r.pos) <= r.attack_range)
                rows.append((risk, sum(trips), low, dict(
                    layout_mode=MODE, layout_revision=1, anchor=anchor,
                    a=guns[0], b=guns[1], c=guns[2], w=p, p=p,
                    gate=transform(-2, 1 if low == -2 else -2),
                    slots=tuple(('rocket', g) for g in guns),
                    c_stands=tuple(sorted(service)),
                    permanent_openings=tuple(sorted(rear)),
                    required_wall_cells=tuple(sorted(walls)),
                    task_round_trips=tuple(2*n for n in trips[:len(world.tasks)]),
                    worker_gate_steps=0, construction_travel=0,
                    candidate_count=2, first_day_wall_cells=tuple(sorted(front_cells(world, anchor))))))
        if time.monotonic() >= deadline:
            return None, 'budget_exhausted'
    if not rows:
        return None, 'incompatible_or_unreachable'
    return min(rows, key=lambda row: row[:3])[3], 'rear_open_selected'
