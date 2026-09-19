"""One shared, observed roster. Economic workers never become a third guard."""
from dataclasses import dataclass, field

from .protocol import distance
from . import defence_duties


def _observed_unavailable(world, identity):
    """Unknown or malformed own-role observations cannot establish a vacancy."""
    if identity is None:
        return None
    actor = world.ours.get(identity)
    if actor is not None:
        return actor.health == 0 if actor.health is not None else None
    records = world.raw.get('teamOur', {}).get('roles')
    if (not isinstance(records, list) or not records or not world.stations
            or len(records) != len(world.ours)
            or any(w.startswith('teamOur:') for w in world.warnings)
            or any(u.health is None for u in world.ours.values())):
        return None
    return True


@dataclass
class NightRoster:
    w: str | None = None
    m: str | None = None
    p: str | None = None
    substituting: bool = False
    generation: int = 0
    handoff_requested: bool = False
    handoff_task: tuple = ()
    traffic: dict = field(default_factory=dict)
    exit_pending: dict = field(default_factory=dict)
    return_recovery: dict = field(default_factory=dict)

    def prepare(self, world):
        workers = sorted((u for u in world.movers if u.kind == 'worker'), key=lambda u: u.id)
        pioneers = sorted((u for u in world.movers if u.kind == 'pioneer'), key=lambda u: u.id)
        plan = getattr(world, 'task_side_plan', None)
        # Preserve identities across travel, death and revival. Choose the
        # nearest worker duty stand when attaching to a running game.
        if self.w is None and workers:
            goals = set(plan['c_stands']) if defence_duties.enabled(world) else {plan['w']} if plan else set()
            self.w = min(workers, key=lambda u: (min((distance(u.pos,q) for q in goals),default=0), u.id)).id
        if self.m is None:
            self.m = next((u.id for u in workers if u.id != self.w), None)
        if self.p is None and pioneers:
            self.p = pioneers[0].id
        if defence_duties.enabled(world):
            self.traffic = {}; self.exit_pending = {}
        live = {u.id for u in world.movers}
        from .rear_open import enabled as rear_enabled
        rear = rear_enabled(world)
        worker_unavailable = (_observed_unavailable(world, self.w) is True
                              if rear else self.w not in live)
        if defence_duties.enabled(world) and worker_unavailable and self.m in live:
            # Promote the observed surviving worker, including when the dead
            # defender disappears from the complete role snapshot. A revival
            # becomes the exterior worker; distance never swaps a living W.
            self.w, self.m = self.m, self.w
            self.generation += 1
            world.role_handoff = dict(worker=self.w,former_worker=self.m,generation=self.generation)
            self.return_recovery = {}
            self.traffic = {}; self.exit_pending = {}
        # A living P remains the second defender while returning from a task. M is
        # never recalled merely to let P solve another external night task.
        # Actual pioneer death retains the existing emergency second guard.
        self.handoff_requested = False
        self.handoff_task = ()
        unavailable = _observed_unavailable(world, self.p) if rear else self.p not in live
        if unavailable is not None:
            self.substituting = unavailable
        second = self.m if self.substituting else self.p
        if self.traffic:
            traveller = world.ours.get(self.traffic["traveller"])
            blocker = world.ours.get(self.traffic["blocker"])
            fixed_w = self.traffic.get('kind') == 'fixed_w_return'
            invalid_fixed = fixed_w and (not plan or self.traffic.get('common') != plan['w']
                or self.traffic.get('gate') != plan['gate'] or self.traffic.get('c') != plan['c']
                or self.traffic['traveller'] != self.w or self.traffic['blocker'] != second)
            restored_fixed = (fixed_w and plan and traveller and blocker
                              and traveller.pos == plan['w'] and blocker.pos in plan['c_stands'])
            if (not traveller or not traveller.alive or not blocker or not blocker.alive
                    or invalid_fixed or restored_fixed
                    or (not fixed_w and not self.traffic.get('gate_owned') and traveller.pos in self.traffic["goals"])):
                self.traffic = {}
        world.roster_yielding = {self.traffic["blocker"]} if self.traffic else set()
        world.night_roster = self
        world.night_defenders = frozenset(i for i in (self.w, second) if i in live)
        world.night_economists = frozenset(u.id for u in workers if u.id not in world.night_defenders)


def defender_ids(world):
    if not hasattr(world, 'night_defenders'):
        NightRoster().prepare(world)
    return world.night_defenders


def operators(world, include_pioneer=True, task_actor=None, allow_task_control=False):
    allowed = defender_ids(world)
    return [u for u in world.movers if u.id in allowed
            and (include_pioneer or u.kind != 'pioneer')
            and (u.id != task_actor or allow_task_control)]


def permits(world, clock, candidate):
    from .purchase_roles import permitted
    if candidate.command.get('action') == 'buy' and not permitted(world, candidate.actor, candidate.command.get('name', '')):
        return False
    owner=candidate.command.get('controllerId') if candidate.command.get('action')=='attack' else candidate.actor
    claimed=getattr(world,'wall_rebuild_actions',{})
    if owner in claimed:
        return candidate.command in claimed[owner] or (candidate.command.get('action')=='use' and candidate.command.get('name')=='Medicine')
    recovery = getattr(world,'return_recovery_actions',{})
    if candidate.actor in recovery:
        return candidate.command in recovery[candidate.actor] or (candidate.command.get('action')=='use'
            and candidate.command.get('name') in {'Medicine','Bomb','DizzyWeapon'})
    from .sunset_market import permits as market_permits
    if not market_permits(world, candidate):
        return False
    command = candidate.command
    from .day_access import gate as access_gate
    current_gate = access_gate(world)
    if defence_duties.enabled(world) and command.get('action')=='remove':
        from .protocol import pos_json
        if command.get('targetPos') in ([pos_json(current_gate)],
                [pos_json(getattr(world,'clear_exit_plan',None) or current_gate)]):
            return command in getattr(world,'ordered_gate_actions',{}).get(candidate.actor,())
    if defence_duties.enabled(world) and command.get('action')=='build' and command.get('name')=='wall':
        from .protocol import pos_json
        from .rules import station_rings
        _, yellow = station_rings(world.task_side_plan['anchor'])
        if (set(world.wall_targets or ()) == yellow
                and command.get('targetPos')==[pos_json(current_gate)]):
            return command in getattr(world,'ordered_gate_actions',{}).get(candidate.actor,())
    treasure = getattr(world,'treasure_actions',{})
    if candidate.actor in treasure and not (command.get('action')=='use' and command.get('name') in {'Medicine','Bomb','DizzyWeapon'}):
        if command not in treasure[candidate.actor]:return False
    actor = world.ours.get(candidate.actor)
    if (clock.phases == {'night'} and actor and actor.id in getattr(world,'night_economists',())
            and command.get('action') in {'buy','sell'}):
        contract = getattr(world, 'forage_contract', None) or {}
        admitted_trade = (contract.get('actor') == actor.id
                          and contract.get('command') == command)
        if not (admitted_trade or command in getattr(world,'night_resupply_commands',{}).get(actor.id,())):
            return False
        # Admission grants only this observed order. Continue through pending
        # receipt and disabled-economy checks below; arbitration still validates
        # the contract's joint guard service and the actor's actual resources.
    if clock.phases != {'day'} and actor and actor.kind == 'pioneer':
        clear_treasure=(getattr(world,'own_wave_cleared',False)
                        and command in treasure.get(candidate.actor,()))
        if not clear_treasure and command.get('action') in {'acceptTask','collect','sell','buy','summonTreasure','drop'}:
            return False
        if command.get('action') == 'move':
            # Only observed defence/clearance/repair routes can move P at
            # night. A fresh procurement or treasure route cannot bypass C.
            return (clear_treasure or command in getattr(world,'pioneer_defence_moves',()) or
                    command in getattr(world,'repair_commands',{}).get(actor.id,()))
    gate_duty=getattr(world,'gate_worker_duty',None)
    if (gate_duty and gate_duty.get('round')==world.round and command.get('action')=='remove'
            and command.get('targetPos')==[{'x':gate_duty['gate'][0],'y':gate_duty['gate'][1]}]
            and candidate.actor!=gate_duty.get('worker')):
        return False
    pending=getattr(world,'pending_night_purchase',None)
    if (pending and candidate.actor==pending['actor'] and command.get('action')=='buy'
            and command.get('name')==pending['name']):
        return False
    if (clock.phases=={'night'} and not getattr(world,'night_foraging_enabled',True)
            and candidate.actor in getattr(world,'night_economists',())
            and (command.get('action') in ('collect','buy','sell') or
                 command.get('action')=='use' and 'UpgradeVoucher' in command.get('name',''))):
        return False
    if (getattr(world, 'repair_policy_active', False) and command.get('action') == 'use'
            and command.get('name') == 'WallFixer'):
        return command in world.repair_commands.get(candidate.actor, ())
    if command.get('action') == 'attack':
        return weapon_allowed(world, str(command.get('controllerId')), candidate.actor)
    if clock.phases != {'day'} and command.get('action') == 'collect':
        return command in getattr(world, 'night_forage_commands', {}).get(candidate.actor, ())
    return True


def economic_endpoints(world, actor, *, observed_exits=False):
    """Physical exterior day endpoints; never substitute a gun stand for M."""
    from .rules import station_rings
    from .navigation import neighbours
    if len(world.stations) != 1:
        return {actor.pos}
    blue, yellow = station_rings(world.stations[0].pos)
    forbidden = blue | yellow | world.stations[0].cells
    plan = getattr(world, 'task_side_plan', None)
    boundary = {plan['gate']} if plan else yellow
    if observed_exits:
        # A partial first-night wall ring may already include G in front10.
        # Use its other real openings; never project a wall removal. Mobile
        # occupants stay hard obstacles in the actual route/traffic checks.
        from .protocol import MOBILE
        closed = {p for cells in world.zones.values() for p in cells}
        closed.update(p for group in (world.ours,world.enemies,world.robots)
                      for unit in group.values() if unit.blocks and unit.kind not in MOBILE
                      for p in unit.cells)
        boundary = yellow - closed
        if plan and plan['gate'] in boundary:
            boundary = {plan['gate']}
    return {p for gate in boundary for p in neighbours(gate)
            if world.inside(p) and p not in forbidden
            and p not in world.occupied - {actor.pos}}


def weapon_allowed(world, identity, weapon_id):
    if identity not in defender_ids(world) or identity in getattr(world, "roster_yielding", set()):
        return False
    plan = getattr(world, 'task_side_plan', None)
    if not plan:
        return True
    from .rear_open import enabled as rear_enabled
    if rear_enabled(world):
        gun = world.ours.get(weapon_id)
        return bool(identity == defence_duties.rotator(world) and gun
                    and (gun.kind, gun.pos) in plan['slots'])
    gun = world.ours.get(weapon_id)
    roster = world.night_roster
    sites = (plan['a'], plan['b']) if identity == defence_duties.rotator(world) else (plan['c'],)
    return bool(gun and gun.pos in sites)


def fixed_stands(world, deadline, include_pioneer=True, task_actor=None, allow_task_control=False):
    budget = getattr(world, 'duty_budget', None)
    if budget is not None:
        return budget.run('fixed_stands', lambda end: _fixed_stands(
            world, end, include_pioneer, task_actor, allow_task_control), deadline)
    return _fixed_stands(world, deadline, include_pioneer, task_actor, allow_task_control)


def _fixed_stands(world, deadline, include_pioneer=True, task_actor=None, allow_task_control=False):
    """None means legacy geometry; an empty plan never authorizes a new layout."""
    import time
    from .navigation import distance_field
    plan = getattr(world, 'task_side_plan', None)
    if not plan:
        return None
    if world.width * world.height > 41 * 32:
        return {}
    result = {}
    for actor in operators(world, include_pioneer, task_actor, allow_task_control):
        if time.monotonic() >= deadline:
            return {}  # Publish no partially calculated assignment.
        goals = defence_duties.stands(world, actor.id)
        traffic = world.night_roster.traffic
        if traffic and actor.id == traffic['blocker']:
            goals = {traffic['stand']}
        if actor.id == task_actor:
            goals &= {actor.pos}
        goals -= world.occupied - {actor.pos}
        goals -= getattr(world, 'operator_excluded_cells', set())
        field = distance_field(world, [actor.pos], actor.pos, deadline)
        reachable = goals & field.keys()
        if reachable:
            from .guard_risk import maintenance_worker, evidence
            if (maintenance_worker(world,actor) and actor.pos in reachable
                    and not evidence(world,actor,actor.pos)['withdraw']):
                result[actor.id]=actor.pos
            else:
                result[actor.id] = min(reachable, key=lambda p: defence_duties.stand_rank(world, actor, p, field[p]))
    return result if time.monotonic() < deadline else {}


def transit_stands(world, clock, deadline):
    if defence_duties.enabled(world):
        return {}  # Ordered ingress is owned by worker_gate, with observed positions.
    budget = getattr(world, 'duty_budget', None)
    if budget is not None:
        return budget.run('duty_transit', lambda end: _transit_stands(world, clock, end), deadline)
    return _transit_stands(world, clock, deadline)


def _observe_exit(world):
    """A released M must really leave, then P must really regain C.

    Ordinary traffic may finish before P has returned from its yield. Keep
    that narrow restoration obligation independently of the traffic record.
    Gate previews never create it and existing task membership always wins.
    """
    roster = getattr(world, 'night_roster', None)
    if roster is None or not roster.exit_pending:
        return False
    pending = roster.exit_pending
    plan = getattr(world, 'task_side_plan', None)
    m, p = (world.ours.get(pending[k]) for k in ('m', 'p'))
    traffic = roster.traffic
    own_traffic = bool(traffic and not traffic.get('gate_owned')
                       and traffic.get('blocker') == pending['p']
                       and (traffic.get('traveller') == pending['m'] or
                            traffic.get('exit_owned') and traffic.get('traveller') == roster.w))
    invalid = (not plan or len(world.stations) != 1 or pending['gate'] != plan['gate'] or
               (pending['m'], pending['p']) != (roster.m, roster.p) or
               not m or not m.alive or not p or not p.alive or
               traffic and not own_traffic)
    if invalid:
        roster.exit_pending = {}
        if own_traffic:
            world.roster_yielding.discard(traffic['blocker'])
            roster.traffic = {}
        return False
    from .rules import station_rings
    blue, yellow = station_rings(plan['anchor'])
    outside = m.pos not in blue | yellow | world.stations[0].cells
    if own_traffic and traffic.get('traveller') == m.id and pending.get('yield_steps',1) > 1 and not outside:
        # Recheck both observed yield steps before fixed_stands creates this
        # frame's move. A newly unknown source pauses the pending yield; it
        # never means the promised second step already happened.
        threats = [u for u in world.robots.values() if u.alive and u.abnormal != 'dizzy']
        if any(u.attack_power is None or u.attack_range is None for u in threats):
            roster.traffic['stand'] = p.pos
        else:
            unsafe = {q for q in blue if 2*sum(u.attack_power for u in threats
                      if distance(q,u.pos)<=u.attack_range) >= p.health}
            world.navigation_avoided.setdefault(p.pos,set()).update(unsafe-{p.pos})
            roster.traffic['stand'] = pending['stand']
    if outside and own_traffic:
        world.roster_yielding.discard(traffic['blocker'])
        roster.traffic = {}
    # Restore P to C first. Its economic yield cell can itself block W;
    # fixed-W clearance then handles that observed local geometry normally.
    if outside and p.pos in plan['c_stands']:
        roster.exit_pending = {}
        return False
    return True


def _transit_stands(world, clock, deadline):
    """Observed routes for returning P and outbound M; neither adds a guard."""
    import time
    from .navigation import distance_field
    _observe_exit(world)
    plan = getattr(world, 'task_side_plan', None)
    if not plan or world.width * world.height > 41 * 32 or time.monotonic() >= deadline:
        return {}
    defender_ids(world)
    roster = world.night_roster
    fixed_return = _fixed_w_transit(world, clock, deadline)
    if fixed_return is not None:
        return fixed_return
    result = {}
    p = world.ours.get(roster.p)
    m = world.ours.get(roster.m)
    from .rules import station_rings
    blue, yellow = station_rings(plan['anchor'])
    interior = blue | yellow | world.stations[0].cells
    exit_inside = bool(roster.exit_pending and m and m.pos in interior)
    w = world.ours.get(roster.w)
    if exit_inside and w and w.alive and w.pos not in interior:
        # Zero-step return targets alone permit unrelated movement. Keep an
        # exterior W in the actual passage whitelist until M is observed out,
        # including failed yield/exit steps and incomplete route preparation.
        result[w.id] = w.pos
        world.fixed_w_transit_actors = (w.id,)
    if (p and p.alive and p.pos not in plan['c_stands'] and not exit_inside
            and (clock.phases != {'day'} or getattr(world,'task_return_required',False))
            and (not world.phase_task or getattr(world,'task_return_required',False))):
        goals = set(plan['c_stands']) - {plan['w']} - (world.occupied - {p.pos})
        field = distance_field(world, [p.pos], p.pos, deadline)
        available = goals & field.keys()
        if available:
            result[p.id] = min(available, key=lambda q: (field[q], q))
        else:
            yielding = clear_c_access(world, p, set(plan['c_stands']), roster.m, deadline)
            result.update(yielding)
    if m and m.alive and m.id not in defender_ids(world) and m.pos in interior:
        # Missing attack facts do not justify a new night excursion. Medical
        # and existing triage remain available while the exterior route waits.
        threats = [u for u in world.robots.values() if u.alive and u.abnormal != 'dizzy']
        if clock.phases != {'day'} and any(u.attack_range is None or u.attack_power is None for u in threats):
            return result
        blocked = set()
        if clock.phases != {'day'}:
            for x in range(world.width):
                if time.monotonic() >= deadline:
                    return result
                for y in range(world.height):
                    q = (x, y)
                    upper = sum(u.attack_power for u in threats if distance(q, u.pos) <= u.attack_range)
                    if 2 * upper >= m.health:
                        blocked.add(q)
        world.navigation_avoided.setdefault(m.pos, set()).update(blocked - {m.pos})
        goals = economic_endpoints(world,m,observed_exits=clock.phases != {'day'}) - blocked
        field = distance_field(world, [m.pos], m.pos, deadline, extra_blocked=blocked - {m.pos})
        available = goals & field.keys()
        if available:
            result[m.id] = min(available, key=lambda q: (field[q], q))
        else:
            yielding = clear_c_access(world,m,goals,roster.p,deadline)
            result.update(yielding)
            if not yielding:
                # W may occupy the first of P's two safe inner yield steps.
                # Preview at most eight real W retreats, then observe that
                # single move before publishing P's economic exit traffic.
                from copy import copy
                from .navigation import neighbours
                w = world.ours.get(roster.w)
                if (w and w.alive and w.pos in blue and w.pos != plan['w'] and p and p.pos in plan['c_stands']
                        and all(u.attack_power is not None and u.attack_range is not None for u in threats)):
                    for wait in sorted(set(neighbours(w.pos)) & blue - world.occupied):
                        if time.monotonic() >= deadline:
                            return result
                        if wait in world.navigation_avoided.get(w.pos,set()):
                            continue
                        if 2*sum(u.attack_power for u in threats if distance(wait,u.pos)<=u.attack_range) >= w.health:
                            continue
                        preview=copy(world)
                        preview.occupied=(world.occupied-{w.pos})|{wait}
                        preview.night_roster=copy(roster)
                        preview.roster_yielding=set(world.roster_yielding)
                        if clear_c_access(preview,m,goals,roster.p,deadline):
                            result[w.id]=wait
                            break
    pending = roster.exit_pending
    if pending and m and m.alive and p and p.alive:
        # Finish actual M passage before P restores C. W already outside
        # waits for the shared opening; an inner W uses the local plan above.
        from .rules import station_rings
        blue, yellow = station_rings(plan['anchor'])
        inside = blue | yellow | world.stations[0].cells
        w = world.ours.get(roster.w)
        if m.pos in inside:
            if w and w.alive and w.pos != plan['w'] and w.pos not in inside:
                result[w.id] = w.pos
                world.fixed_w_transit_actors = (w.id,)
            result[p.id] = pending['stand']
    if roster.traffic:
        result[roster.traffic['blocker']] = roster.traffic['stand']
    return result


def _fixed_w_transit(world, clock, deadline):
    """One local blocker, at most one W step and two blocker yield steps.

    Preview only the 12 blue cells and the observed open G. Never change a
    wall, move an active task actor, or assume a planned step has succeeded.
    The same traffic stays owned through W arrival and actual C restoration.
    """
    import time
    from .navigation import neighbours
    from .rules import station_rings
    from .task_side_layout import _field, BudgetExpired
    roster = world.night_roster
    plan = world.task_side_plan
    traffic = roster.traffic
    owned = traffic.get('kind') == 'fixed_w_return'
    world.fixed_w_transit_actors = ()
    if not owned and clock.phases != {'night'} and clock.until_night > 20:
        return None  # Ordinary daytime work keeps its existing return budget.
    if not owned and clock.phases == {'day'} and world.phase_task:
        return None  # Do not preempt an active daytime task to start a yield.
    if roster.exit_pending or traffic and not owned or len(world.stations) != 1:
        return None
    w = world.ours.get(roster.w)
    blocker = world.ours.get(traffic['blocker'] if owned else roster.m if roster.substituting else roster.p)
    if not w or not w.alive or not blocker or not blocker.alive:
        return None
    blue, _ = station_rings(plan['anchor'])
    area = blue | {plan['gate']}
    if not owned and (w.pos == plan['w'] or w.pos not in area or blocker.pos not in plan['c_stands']):
        return None
    holds = {w.id:w.pos, blocker.id:blocker.pos}
    threats = [r for r in world.robots.values() if r.alive and r.abnormal != 'dizzy']
    known = all(r.attack_power is not None and r.attack_range is not None for r in threats)
    hazards = {}

    def blocked(actor, positions):
        obstacles = (world.occupied - {w.pos, blocker.pos}) | set(positions)
        obstacles |= world.navigation_avoided.get(actor.pos,set())
        if known:
            if actor.id not in hazards:
                unsafe = set()
                for q in area:
                    if time.monotonic() >= deadline:
                        raise BudgetExpired
                    from .guard_risk import maintenance_worker, evidence
                    unsafe_here=(evidence(world,actor,q)['lethal'] if maintenance_worker(world,actor) else
                        2*sum(r.attack_power for r in threats if distance(q,r.pos)<=r.attack_range)>=actor.health)
                    if unsafe_here:
                        unsafe.add(q)
                hazards[actor.id] = unsafe
            obstacles |= hazards[actor.id]
        return obstacles

    def field(actor, starts, positions):
        return _field(world, starts, blocked(actor,positions), deadline, area)

    def goal(actor, target, other):
        route = field(actor,{target},{other})
        steps = [q for q in neighbours(actor.pos) if q in route
                 and route[q] < route.get(actor.pos,0)]
        return min(steps,key=lambda q:(route[q],q)) if steps else actor.pos

    try:
        if not owned:
            actual = _field(world,{plan['w']},world.occupied-{w.pos},deadline,area)
            if w.pos in actual:
                return None
            relaxed = _field(world,{plan['w']},world.occupied-{w.pos,blocker.pos},deadline,area)
            if w.pos not in relaxed:
                return None  # A static break is not a one-role traffic problem.
        world.fixed_w_transit_actors = (w.id,blocker.id)
        if time.monotonic() >= deadline or not known:
            return holds
        if owned:
            stage = traffic['stage']
            if w.pos == plan['w']:
                traffic['stage'] = stage = 'restore_c'
            if stage == 'restore_c':
                routes = field(blocker,set(plan['c_stands']),{w.pos})
                if blocker.pos not in routes:
                    return holds
                home = field(blocker,{blocker.pos},{w.pos})
                available = set(plan['c_stands']) & home.keys()
                if available:
                    traffic['stand'] = min(available,key=lambda q:(home[q],q))
                    holds[blocker.id] = goal(blocker,traffic['stand'],w.pos)
                return holds
            if stage == 'retreat_w':
                if w.pos != traffic['wait']:
                    holds[w.id] = goal(w,traffic['wait'],blocker.pos)
                    return holds
                traffic['stage'] = stage = 'yield_c'
            if stage == 'yield_c':
                if blocker.pos != traffic['stand']:
                    # Keep the promised W route and C return feasible before
                    # each actual yield step, including changed wall/risks.
                    passage = field(w,{plan['w']},{traffic['stand']})
                    restored = field(blocker,set(plan['c_stands']),{plan['w']})
                    if w.pos not in passage or traffic['stand'] not in restored:
                        return holds
                    holds[blocker.id] = goal(blocker,traffic['stand'],w.pos)
                    return holds
                traffic['stage'] = stage = 'pass_w'
            if stage == 'pass_w':
                holds[w.id] = goal(w,plan['w'],blocker.pos)
            return holds

        # Enumerate at most 9 observed W holds/one-step retreats and 13 local
        # yield endpoints. All preview fields are bounded to those same cells.
        waits = [w.pos] + sorted((set(neighbours(w.pos)) & blue) - world.occupied)
        options = []
        for wait in waits:
            if wait != w.pos and wait in blocked(w,{blocker.pos}):
                continue
            reach = field(blocker,{blocker.pos},{wait})
            for stand in sorted(area & reach.keys()):
                if not 1 <= reach[stand] <= 2 or stand == plan['w']:
                    continue
                passage = field(w,{plan['w']},{stand})
                restored = field(blocker,set(plan['c_stands']),{plan['w']})
                if wait not in passage or stand not in restored:
                    continue
                options.append((stand not in blue, wait != w.pos,
                                int(wait!=w.pos)+reach[stand]+passage[wait]+restored[stand],
                                wait,stand))
        if not options or time.monotonic() >= deadline:
            return holds
        _,_,_,wait,stand = min(options)
        if wait != w.pos:
            holds[w.id] = goal(w,wait,blocker.pos)
        else:
            holds[blocker.id] = goal(blocker,stand,w.pos)
        if time.monotonic() >= deadline:
            return {w.id:w.pos,blocker.id:blocker.pos}
        roster.traffic = dict(kind='fixed_w_return',blocker=blocker.id,traveller=w.id,
            stand=stand,goals=frozenset({plan['w']}),wait=wait,common=plan['w'],
            gate=plan['gate'],c=plan['c'],stage='retreat_w' if wait!=w.pos else 'yield_c')
        world.roster_yielding.add(blocker.id)
        if blocker.id == roster.p and world.phase_task:
            world.task_return_required = True
        return holds
    except BudgetExpired:
        # Do not publish an unfinished new option. Existing traffic waits on
        # actual observations and keeps immediate rescue candidates available.
        return holds if owned or world.fixed_w_transit_actors else None


def clear_c_access(world, traveller, goals, blocker_id, deadline):
    """Move the C operator to another C neighbour only if it opens this route."""
    from copy import copy
    import time
    from .navigation import distance_field, neighbours
    plan = world.task_side_plan
    blocker = world.ours.get(blocker_id)
    if time.monotonic() >= deadline or not blocker or not blocker.alive or blocker.pos not in plan['c_stands']:
        return {}
    if (blocker.kind == 'pioneer' and world.phase_task
            and not getattr(world,'task_return_required',False)
            and getattr(getattr(world,'night_clock',None),'phases',{'day'}) == {'day'}):
        return {}  # Task-neighbourhood movement needs its separate whitelist.
    # Test real stands against the blocked traveller's full route. An ordinary
    # economic exit can need two P steps along the blue floor: the first yield
    # cell can itself occupy the corridor. Gate previews keep the one-step rule.
    from .rules import station_rings
    blue, _ = station_rings(plan['anchor'])
    roster = world.night_roster
    ordinary_exit = (traveller.id == roster.m and blocker.id == roster.p
                     and not roster.handoff_requested and bool(goals)
                     and not set(goals) & set(plan['c_stands']))
    reach = {q:1 for q in neighbours(blocker.pos)}
    if ordinary_exit:
        from .task_side_layout import _field, BudgetExpired
        occupied = (world.occupied | world.navigation_avoided.get(blocker.pos,set()) | {plan['w']}) - {blocker.pos}
        threats = [u for u in world.robots.values() if u.alive and u.abnormal != 'dizzy']
        if any(u.attack_power is None or u.attack_range is None for u in threats):
            return {}
        occupied.update(q for q in blue if 2*sum(u.attack_power for u in threats
                        if distance(q,u.pos)<=u.attack_range) >= blocker.health)
        try:
            reach = _field(world,{blocker.pos},occupied,deadline,blue)
        except BudgetExpired:
            return {}
    choices = sorted(blue - {plan['w']}, key=lambda q: (reach.get(q,float('inf')),q not in plan['c_stands'],q))
    for stand in choices:
        if time.monotonic() >= deadline:
            return {}
        if (stand in world.occupied or not 1 <= reach.get(stand,0) <= (2 if ordinary_exit else 1)
                or stand in world.navigation_avoided.get(blocker.pos, set())):
            continue
        threats = [u for u in world.robots.values() if u.alive and u.abnormal != 'dizzy']
        if any(u.attack_power is None or u.attack_range is None for u in threats):
            continue
        if 2*sum(u.attack_power for u in threats if distance(stand,u.pos)<=u.attack_range) >= blocker.health:
            continue
        opened = copy(world)
        opened.occupied = (world.occupied - {blocker.pos}) | {stand}
        field = distance_field(opened, goals - opened.occupied, traveller.pos, deadline)
        if traveller.pos in field and time.monotonic() < deadline:
            world.roster_yielding.add(blocker.id)
            if blocker.kind == 'pioneer' and world.phase_task:
                world.task_return_required = True
            world.night_roster.traffic = dict(blocker=blocker.id, traveller=traveller.id,
                                              stand=stand, goals=frozenset(plan['c_stands'] if set(goals) & set(plan['c_stands']) else goals))
            roster = world.night_roster
            if (traveller.id == roster.m and blocker.id == roster.p and not roster.handoff_requested
                    and not set(goals) & set(plan['c_stands'])):
                roster.exit_pending = dict(m=traveller.id,p=blocker.id,gate=plan['gate'],
                                           stand=stand,round=world.round,yield_steps=reach[stand])
            return {blocker.id: stand}
    return {}


def admit_task_departure(world, clock, choice, deadline):
    """Only daytime can start a task; night never recalls the economist."""
    _observe_exit(world)
    defender_ids(world)
    roster = world.night_roster
    roster.handoff_requested = False
    roster.handoff_task = ()
    if clock.phases == {'day'}:
        return choice
    return dict(actor=roster.p,selected=None,candidates=[],reason='pioneer night defence; economist stays outside')
