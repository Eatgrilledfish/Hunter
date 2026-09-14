"""Bounded task-side three-rocket geometry, separate from role execution.

Distances describe static paths through the planned gate with W reserved.
They never certify safe night travel or assume that a mobile actor has moved.
Incomplete searches publish no plan. Session ownership freezes selected sites.
"""
from dataclasses import dataclass
from collections import Counter
import time

from .navigation import neighbours
from .protocol import MOBILE, distance
from .rules import station_rings, Clock


class BudgetExpired(Exception):
    pass


def _field(world, starts, blocked, deadline, allowed=None):
    result = {p: 0 for p in starts if p not in blocked and world.inside(p)
              and (allowed is None or p in allowed)}
    queue = list(result)
    for index, p in enumerate(queue):
        if index % 32 == 0 and time.monotonic() >= deadline:
            raise BudgetExpired
        for q in neighbours(p):
            if (q in result or q in blocked or not world.inside(q)
                    or allowed is not None and q not in allowed):
                continue
            result[q] = result[p] + 1
            queue.append(q)
    return result


def templates(anchor, all_gate_sides=False):
    """Eight gun orientations; gate placement may use any non-corner side."""
    for reflected in (False, True):
        for rotations in range(4):
            def transform(p):
                u, v = p
                if reflected:
                    u = 5-u
                for _ in range(rotations):
                    u, v = 5-v, u
                return anchor[0]-2+u, anchor[1]-3+v
            gates = [(i,0) for i in range(1,5)]
            if all_gate_sides:
                gates += [(i,5) for i in range(1,5)]+[(0,i) for i in range(1,5)]+[(5,i) for i in range(1,5)]
            for gate in gates:
                yield dict(a=transform((1, 4)), w=transform((2, 4)),
                           b=transform((3, 4)), c=transform((1, 1)),
                           p=transform((2, 1)), gate=transform(gate))


def _static(world):
    return ({p for cells in world.zones.values() for p in cells}
            | {p for u in (*world.ours.values(), *world.enemies.values())
               if u.kind not in MOBILE and u.health != 0 for p in u.cells})


def select(world, rules, policy, deadline):
    from .battery import interior_usable
    from .director import exposure
    from .wall_policy import front_cells, monster_face
    if len(world.stations) != 1 or rules.weapon_limit != 3:
        return None, 'unknown_region'
    anchor = world.stations[0].pos
    blue, yellow = station_rings(anchor)
    if (any(not world.inside(p) for p in blue | yellow)
            or any(not (rule := rules.build_rule(world, name)) or rule.cells != region
                   for name, region in (('rocket', blue), ('wall', yellow)))):
        return None, 'unknown_region'
    tasks = [t for t in world.tasks if world.task_cells(t)
             and all(type(t.get(k)) is int and t[k] >= 0
                     for k in ('scoreReward', 'goldReward'))]
    # This template family intentionally supports the two advertised points.
    if not tasks or len(tasks) > 2:
        return None, 'unknown_tasks'
    tasks.sort(key=lambda t: tuple(sorted(world.task_cells(t))))
    if any(g.kind != 'rocket' for g in world.weapons):
        return None, 'incompatible_existing_weapons'
    static = _static(world)
    owned_walls = {u.pos: u for u in world.ours.values() if u.kind == 'wall' and u.alive}
    region = blue | yellow | world.stations[0].cells
    try:
        # The closed camp is excluded from exterior task routes. Per-candidate
        # opening joins this graph only through G; two-cell tasks use a union.
        exterior = []
        task_areas = [{q for p in world.task_cells(task) for q in neighbours(p)} for task in tasks]
        for i, starts in enumerate(task_areas):
            # acceptTask has no point ID: an ambiguous shared stand cannot
            # certify access to the particular task used in the ranking.
            starts = starts - set().union(*(a for j,a in enumerate(task_areas) if j != i))
            exterior.append(_field(world, starts, static | region, deadline))
        workers = [u for u in world.movers if u.kind == 'worker']
        pioneers = [u for u in world.movers if u.kind == 'pioneer']
        if len(workers) != 2 or len(pioneers) != 1:
            return None, 'unknown_roles'
        worker_fields = [_field(world, [u.pos], static, deadline) for u in workers]
        # Compare a concrete current economic trip, without predicting mine
        # stock or income. Loaded workers head to an observed buyer; otherwise
        # use the nearest currently profitable mineral interaction stand.
        economic_fields = []
        economic_starts = []
        for actor in workers:
            targets = set(world.zones.get('vendor', ())) if any(
                actor.inventory[k] and world.vendor.get(k, 0) > 0 for k in ('stone','iron','copper')) else set()
            if not targets:
                targets = {p for k in ('stone','iron','copper') if world.vendor.get(k,0)>0
                           for p in world.zones.get(k, ())}
            starts = {q for p in targets for q in neighbours(p)} - static - region
            economic_starts.append(starts)
            economic_fields.append(_field(world, starts, static | region, deadline))
        front = front_cells(world, anchor)
        stats = rules.building_stats.get('rocket', {}).get(1)
        clock = Clock(world.round, rules.round_origin)
        rows = []
        rejected = Counter()
        protect_front = policy.pioneer_rotation_enabled
        facing = monster_face(world,anchor)
        for template in templates(anchor, all_gate_sides=protect_front):
            guns = {template[k] for k in ('a', 'b', 'c')}
            w, p, gate = (template[k] for k in ('w', 'p', 'gate'))
            if protect_front and gate in facing:
                rejected['monster_facing_gate'] += 1
                continue
            wall = owned_walls.get(gate)
            if (wall is not None and wall.level != 1
                    or gate in static and wall is None):
                rejected['protected_or_blocked_gate'] += 1
                continue  # Do not spend an old upgrade or remove another object.
            occupied_guns = {g.pos for g in world.weapons}
            # Preserve every observed gun and every unrelated static blocker.
            blocked = (static - occupied_guns) & blue
            if (not occupied_guns <= guns or blocked & (guns | {w, p})
                    or not interior_usable(blue, blocked, guns)):
                rejected['incompatible_structure'] += 1
                continue
            floor = blue - guns - blocked
            risks = [exposure(world, clock, q) for q in (w,p)]
            if (risks[0]['upper_per_attack_opportunity'] >= min(u.health for u in workers)
                    or risks[1]['upper_per_attack_opportunity'] >= pioneers[0].health):
                rejected['lethal_exposure_upper'] += 1
                continue
            inner = _field(world, [p], guns | blocked | {w}, deadline, floor | {gate})
            if gate not in inner:
                rejected['pioneer_gate_route'] += 1
                continue
            c_stands = {q for q in floor - {w} if distance(q, template['c']) <= 1}
            from_gate = _field(world, [gate], guns | blocked | {w}, deadline, floor | {gate})
            returns = [from_gate[q] for q in c_stands if q in from_gate]
            fallback = _field(world, [w], guns | blocked, deadline, floor | {gate})
            if not returns or gate not in fallback:
                rejected['backup_or_c_return'] += 1
                continue
            outside = set(neighbours(gate)) - region - static
            trips = []
            for field in exterior:
                distances = [field[q] + 1 for q in outside if q in field]
                trips.append(inner[gate] + min(returns) + 2*min(distances) if distances else None)
            if any(t is None for t in trips):
                rejected['task_unreachable'] += 1
                continue
            # A gate operation is performed from an adjacent exterior stand,
            # never by walking onto the future wall. Two operations budget the
            # closing/opening pair, without assuming those actions succeeded.
            gate_trips = []
            for i, actor in enumerate(workers):
                direct = min((worker_fields[i][q] for q in economic_starts[i]
                              if q in worker_fields[i]), default=None)
                via = [worker_fields[i][q]+economic_fields[i][q]
                       for q in outside if q in worker_fields[i] and q in economic_fields[i]]
                if direct is not None and via:
                    gate_trips.append((max(0,min(via)-direct)+2,actor.id))
            gate_detour, gate_worker = min(gate_trips) if gate_trips else (None,None)
            # Whole-camp static evidence only. Runtime repair uses actual
            # cooldown/occupancy and may return to a different C neighbour.
            from_p = _field(world,[p],guns|blocked|{w},deadline,floor)
            to_c = _field(world,c_stands,guns|blocked|{w},deadline,floor)
            wall_access = []
            for wall_pos in sorted(yellow):
                stands = set(neighbours(wall_pos)) & floor
                costs = [from_p[q]+1+to_c[q] for q in stands if q in from_p and q in to_c]
                wall_access.append(dict(pos=wall_pos,worker_stationary=distance(w,wall_pos)<=1,
                    pioneer_rounds=min(costs) if costs else None, outside_only=not bool(stands)))
            coverage = sum(bool(row['pos'] in front and (row['worker_stationary'] or
                row['pioneer_rounds'] is not None and stats and stats.cooldown is not None
                and row['pioneer_rounds']<=stats.cooldown)) for row in wall_access)
            coverage_range = sum(distance(g,r.pos)<=stats.attack_range for g in guns
                for r in world.robots.values() if r.alive) if stats and type(stats.attack_range) is int else 0
            builder = [f[q] for f in worker_fields for q in floor if q in f]
            if not builder:
                rejected['no_builder_access'] += 1
                continue
            row = dict(template, slots=tuple(('rocket', template[k]) for k in ('a', 'b', 'c')),
                       anchor=anchor,
                       task_round_trips=tuple(trips), c_stands=tuple(sorted(c_stands)),
                       worker_gate_steps=max(0,fallback[gate]-1), construction_travel=min(builder),
                       candidate_count=128 if protect_front else 32,
                       gate_detour_rounds=gate_detour, suggested_gate_worker=gate_worker,
                       exposure_upper=sum(r['upper_per_attack_opportunity'] for r in risks),
                       exposure_unknown=any(r['unknown_robot_damage'] for r in risks),
                       initial_range_coverage=coverage_range, front_repair_coverage=coverage,
                       wall_access=tuple(wall_access))
            rows.append(row)
        if time.monotonic() >= deadline:
            raise BudgetExpired
        if not rows:
            world.task_layout_rejections = dict(rejected)
            return None, 'no_template'
        # Choose ONE primary task against the same complete candidate family.
        # Comparing each row's own preferred task would let a short low-value
        # task beat a longer high-value task merely by changing the comparator.
        primary = min(range(len(tasks)), key=lambda i: (
            -(tasks[i]['scoreReward'] + policy.task_gold_weight*tasks[i]['goldReward']) /
            (1 + min(r['task_round_trips'][i] for r in rows)), i))
        winner = min(rows, key=lambda r: (
            r['task_round_trips'][primary],
            tuple(t for i,t in enumerate(r['task_round_trips']) if i != primary),
            (r['gate_detour_rounds'] is None,r['gate_detour_rounds'] or 0),
            r['worker_gate_steps'],r['exposure_unknown'],r['exposure_upper'],
            -r['initial_range_coverage'],-r['front_repair_coverage'],r['construction_travel'],
            tuple(r[k] for k in ('a','w','b','c','p','gate'))))
        winner['primary_task'] = tuple(sorted(world.task_cells(tasks[primary])))
        winner['feasible_candidates'] = len(rows)
        winner['rejected_candidates'] = dict(rejected)
        if time.monotonic() >= deadline:
            raise BudgetExpired
        return winner, 'selected'
    except BudgetExpired:
        return None, 'budget_exhausted'


@dataclass
class TaskSideLayout:
    plan: dict | None = None
    status: str = 'disabled'

    def prepare(self, world, rules, policy, deadline):
        world.task_side_plan = None
        world.task_layout_rejections = {}
        if not policy.task_side_layout_enabled:
            self.status = 'disabled'
            return
        if self.plan and (len(world.stations) != 1 or world.stations[0].pos != self.plan['anchor']):
            self.plan = None
        if self.plan is None:
            self.plan, self.status = select(world, rules, policy, deadline)
        if self.plan:
            # Unexpected buildings never cause replacement of observed weapons.
            if any((u.kind, u.pos) not in self.plan['slots'] for u in world.weapons):
                self.status = 'incompatible_existing_weapons'
                return
            world.task_side_plan = self.plan


def apply(world, rules, policy):
    plan = getattr(world, 'task_side_plan', None)
    if not policy or not policy.task_side_layout_enabled or not plan:
        return False
    blue, yellow = station_rings(plan['anchor'])
    world.build_interior = blue
    world.wall_targets = yellow
    world.firing_ports = frozenset()
    world.battery_plan = dict(mode='task_side_3r', enclosure=True, slots=plan['slots'],
                              ports=(), wall_goal=20, direction_source='own_task_routes')
    return True
