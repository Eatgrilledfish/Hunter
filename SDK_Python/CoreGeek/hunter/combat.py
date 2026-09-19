"""Bounded target generation; joint damage is scored by the shared arbiter."""
from itertools import combinations
import time

from .arbitration import Candidate
from .navigation import axis_ray, neighbours
from .rays import clear_centre_ray, primitive_step
from .protocol import MOBILE, distance, pos_json
from .base_fire import BasePressure
from .night_roles import operators, weapon_allowed
from .robot_targets import opposing, protected_area, line_clear, eligible, cleanup


def threat_weights(world):
    assets = [u.pos for u in world.stations + world.movers]
    staffed = [g for g in world.weapons if g.attack_range is not None and
        any(i in world.ours and world.ours[i].alive and distance(world.ours[i].pos,g.pos)<=1
            for i in getattr(world,'night_defenders',()))]
    motion = getattr(world,'observed_robot_motion',{})
    weights = {}
    for robot in world.robots.values():
        if not robot.alive:
            continue
        if cleanup(world):
            weights[robot.id] = 1.0 / max(1, robot.health)
            continue
        near = min((distance(robot.pos, p) for p in assets), default=30)
        intent = 0.0 if opposing(world,robot) else 1.0
        pressure = {"smallRobot": 1.0, "middleRobot": 1.2, "largeRobot": 1.6, "bossRobot": 2.0}.get(robot.kind, 1.0)
        # A rear gun must cover enemies reaching the base, even when they
        # bypass other turrets. No assumption about the judge's target order.
        base_cells = [p for base in world.stations for p in (
            base.pos, (base.pos[0]+1, base.pos[1]), (base.pos[0], base.pos[1]-1), (base.pos[0]+1, base.pos[1]-1))]
        base_distance = min((distance(robot.pos, p) for p in base_cells), default=30)
        base_pressure = 2.0 if base_distance <= 3 else 1.4 if base_distance <= 5 else 1.0
        weights[robot.id] = intent * (1.0 + 8.0 / (near + 1)) * pressure * base_pressure * (0.6 if robot.abnormal == "dizzy" else 1.0)
        if staffed and robot.id in motion and base_pressure==1.0 and robot.abnormal!='dizzy':
            dx,dy=motion[robot.id]
            previous=(robot.pos[0]-dx,robot.pos[1]-dy)
            margin=max(g.attack_range-distance(g.pos,robot.pos) for g in staffed)
            previous_margin=max(g.attack_range-distance(g.pos,previous) for g in staffed)
            if margin>=0 and margin<previous_margin:
                # A local priority heuristic, not a robot speed/cooldown rule:
                # observed targets leaving staffed coverage lose future shots.
                weights[robot.id]*=2
    return weights


def rocket_damage(world, impacts):
    return {r.id: sum(20 if p == r.pos else 10 if distance(p, r.pos) == 1 else 0 for p in impacts)
            for r in world.robots.values() if r.alive}


def diverse_rocket_options(world, single, level, weights, baseline, deadline):
    """Retain a best standalone shot plus complementary, bounded allocations.

    Residuals are candidate-generation counterfactuals only. They never update
    the world or assume another gun actually fires; the arbiter checks all locks
    and scores the final combined damage against the original observation.
    """
    sparse = [(p, {k: d for k, d in damage.items() if d}) for p, damage in single]
    health = {r.id: r.health for r in world.robots.values() if r.alive}

    def greedy(residual, group=None):
        impacts, damage = [], {}
        for _ in range(level):
            best = None
            for point, effect in sparse:
                if time.monotonic() >= deadline:
                    return None
                if point in impacts:
                    continue
                gain = sum(weights[k]*min(max(0, health[k]-residual.get(k, 0)-damage.get(k, 0)), d)
                           for k, d in effect.items() if group is None or k in group)
                rank = (-gain, point)
                if best is None or rank < best[0]:
                    best = rank, point, effect
            if best is None:
                return None
            _, point, effect = best
            impacts.append(point)
            for key, amount in effect.items():
                damage[key] = damage.get(key, 0)+amount
        return tuple(sorted(impacts)), damage, 0.1

    proposals, residual = list(baseline[:1]), {}
    # Up to three cooperating guns: preserve alternatives after one or two
    # hypothetical volleys have depleted the dominant group.
    for _ in range(min(3, len(world.weapons))):
        option = greedy(residual)
        if option is None:
            break
        proposals.append(option)
        for key, amount in option[1].items():
            residual[key] = residual.get(key, 0)+amount
    positions = {r.pos: r.id for r in world.robots.values() if r.alive}
    remaining, groups = set(positions), []
    while remaining and time.monotonic() < deadline:
        frontier, group = [min(remaining)], set()
        remaining.remove(frontier[0])
        while frontier:
            x, y = frontier.pop()
            group.add(positions[(x, y)])
            adjacent = {(x+dx, y+dy) for dx in range(-2, 3) for dy in range(-2, 3)} & remaining
            remaining.difference_update(adjacent)
            frontier.extend(sorted(adjacent))
        groups.append(group)
    groups.sort(key=lambda g: (-sum(weights[k]*health[k] for k in g), tuple(sorted(g))))
    for group in groups[:3]:
        option = greedy({}, group)
        if option is not None:
            proposals.append(option)
    proposals.extend(baseline)
    retained, seen = [], set()
    for option in proposals:
        effect = tuple(sorted((k, d) for k, d in option[1].items() if d))
        if effect and effect not in seen:
            seen.add(effect)
            retained.append(option)
        if len(retained) == 8:
            break
    return retained or baseline


def line_damage(world, weapon, target, rules, allow_empty=False):
    if weapon.kind == "gatling":
        # A bullet is consumed by the first robot: occupancy beyond that hit
        # cannot obstruct this shot. Keep the requested endpoint in the command
        # (range/cone/count checks still use it), shorten only the forecast.
        dx, dy = target[0]-weapon.pos[0], target[1]-weapon.pos[1]
        length2 = dx*dx + dy*dy
        centres = []
        for robot in world.robots.values():
            rx, ry = robot.pos[0]-weapon.pos[0], robot.pos[1]-weapon.pos[1]
            along = rx*dx + ry*dy
            if robot.alive and 0 < along <= length2 and dx*ry == dy*rx:
                centres.append((along, robot.id, robot.pos))
        if centres:
            target = min(centres)[2]
    ray = axis_ray(weapon.pos, target)
    if ray is None:
        ray = clear_centre_ray(weapon.pos, target, world.occupied)
        if ray is None:
            return None
    robots = [r for r in world.robots.values() if r.alive and r.pos in ray]
    robots.sort(key=lambda r: (distance(weapon.pos, r.pos), r.id))
    robot_cells = {r.pos for r in robots}
    # User confirmed Gatling bullets stop at walls. Other object/damage rules
    # remain unknown; conservatively avoid firing through those objects too.
    if any(p in world.occupied and p not in robot_cells for p in ray):
        return None
    if not robots:
        return {} if allow_empty else None
    if weapon.kind == "gatling":
        return {robots[0].id: 10}
    energy = rules.rail_energy.get(weapon.level)
    if energy is None:
        return {}  # Legal line, unknown energy: no fabricated damage forecast.
    damage = {}
    for robot in robots:
        amount = min(energy, robot.health)
        damage[robot.id] = amount
        energy -= amount
    return damage


def fire_status(world, clock, rules, response, candidates):
    """Bounded diagnostics: observed range is not a guaranteed clear shot."""
    rows = []
    for gun in world.weapons:
        controllers = [u.id for u in operators(world) if distance(u.pos, gun.pos) <= 1 and weapon_allowed(world, u.id, gun.id)]
        targets = sorted((r for r in world.robots.values() if eligible(world,r) and
                          distance(gun.pos,r.pos) <= (gun.attack_range or 0)), key=lambda r:r.id)
        count = sum(c.actor == gun.id and c.command.get("action") == "attack" for c in candidates)
        fired = response["roleCommandMap"].get(gun.id,{}).get("action") == "attack"
        why = ("fire" if fired else "day" if clock.phases != {"night"} else "no_controller" if not controllers
               else "cooldown" if gun.cooldown else "no_target_in_range" if not targets else
               "arbitration_or_controller_busy" if count else "no_damage_candidate")
        row = {"id":gun.id,"cd":gun.cooldown,"range":gun.attack_range,"controllers":controllers,
               "targets":len(targets),"fired":fired,"why":why,"candidates":count}
        row['ignored_opponent_camp'] = sum(r.alive and opposing(world,r) and not cleanup(world) for r in world.robots.values())
        row['combat_mode'] = 'CLEANUP' if cleanup(world) and targets else 'IDLE_CLEAR' if cleanup(world) else 'DEFEND'
        row['opponent_in_range'] = sum(r.alive and opposing(world,r) and distance(gun.pos,r.pos)<=(gun.attack_range or 0) for r in world.robots.values())
        row['preferred'] = [dict(id=r.id,hp=r.health,distance=distance(gun.pos,r.pos)) for r in sorted(targets,key=lambda r:(r.health,r.id))[:3]]
        row['unknown_camp_targets'] = sum(r.target_team is None for r in targets)
        if why == "no_damage_candidate" and gun.kind != "rocket":
            robot_cells = {r.pos for r in world.robots.values() if r.alive}
            for target in targets[:8]:
                ray = axis_ray(gun.pos,target.pos)
                if ray is None:
                    continue
                blockers = [p for p in ray if p in world.occupied and p not in robot_cells]
                if blockers:
                    point = blockers[0]
                    kinds = sorted({u.kind for u in list(world.ours.values())+list(world.enemies.values()) if point in u.cells})
                    row["sample"] = {"target":target.pos,"blocked_at":point,"kind":kinds or ["neutral"]}
                    row["why"] = "wall_blocks_gatling" if gun.kind == 'gatling' and 'wall' in kinds else "unverified_object_blocking"
                    break
            else:
                row["why"] = "unverified_ray_or_damage"
        rows.append(row)
    return rows


def propose(world, clock, rules, deadline, task_actor=None, *, base_fire_enabled=False,
            rocket_diversity_enabled=True):
    # This observation is published only after this invocation returns a whole
    # fire proposal. An empty completed pool differs from an expired search.
    world.combat_fire_observation = {'round': world.round, 'complete': False, 'effects': {}}
    if clock.phases != {"night"}:
        world.combat_fire_observation['complete'] = True
        return []
    weights = threat_weights(world)
    pressure = BasePressure(world, clock) if base_fire_enabled else None
    result = []
    robots = [r for r in world.robots.values() if eligible(world,r)]
    protected = protected_area(world)
    for weapon in world.weapons:
        if time.monotonic() >= deadline:
            break
        controllers = [u for u in operators(world, task_actor=task_actor) if distance(u.pos, weapon.pos) <= 1 and weapon_allowed(world, u.id, weapon.id)]
        if not controllers or weapon.level not in {1, 2, 3} or weapon.attack_range is None:
            continue
        if weapon.cooldown is not None and weapon.cooldown > 0:
            continue
        if weapon.kind == "rocket" and weapon.cooldown is None:
            continue
        options = []
        focused = []
        if weapon.kind == "rocket":
            cells = sorted({p for r in robots for p in neighbours(r.pos) + [r.pos]
                            if world.inside(p) and distance(weapon.pos, p) <= weapon.attack_range
                            and p not in protected})
            single = [(p, rocket_damage(world, [p])) for p in cells]
            single.sort(key=lambda row: (-sum(weights[k]*min(world.robots[k].health, d) for k, d in row[1].items()), row[0]))
            for impacts in combinations([p for p, _ in single[:12]], weapon.level):
                options.append((impacts, rocket_damage(world, impacts), 0.1))
            if pressure is not None:
                targets = sorted(pressure.targets, key=lambda i: (
                    -pressure.targets[i][0]/pressure.targets[i][1], i))[:4]
                for identity in targets:
                    if time.monotonic() >= deadline:
                        break
                    ranked = sorted(single, key=lambda row: (
                        -row[1].get(identity, 0),
                        -sum(weights[k]*min(world.robots[k].health, d) for k,d in row[1].items()), row[0]))
                    if len(ranked) >= weapon.level and ranked[0][1].get(identity, 0):
                        impacts = tuple(sorted(p for p,_ in ranked[:weapon.level]))
                        focused.append((impacts, rocket_damage(world, impacts), 0.1))
        else:
            single = []
            endpoints = set()
            for robot in robots:
                if distance(weapon.pos, robot.pos) > weapon.attack_range:
                    continue
                endpoints.add(robot.pos)
                step = primitive_step(weapon.pos, robot.pos)
                if weapon.kind == "gatling" and step is not None:
                    sx, sy = step
                    for extra in range(1, weapon.level):
                        endpoint = robot.pos[0]+sx*extra, robot.pos[1]+sy*extra
                        if world.inside(endpoint) and distance(weapon.pos, endpoint) <= weapon.attack_range:
                            endpoints.add(endpoint)
            if weapon.kind == "gatling":
                # Distinct nearby endpoints can fill a required volley when the
                # map edge leaves fewer than level known hitting endpoints. No
                # damage beyond an empty endpoint is assumed.
                endpoints.update(p for p in neighbours(weapon.pos) if world.inside(p)
                                 and distance(weapon.pos, p) <= weapon.attack_range)
            for endpoint in sorted(endpoints):
                if time.monotonic() >= deadline:
                    break
                damage = line_damage(world, weapon, endpoint, rules, allow_empty=weapon.kind == "gatling")
                if damage is not None and line_clear(world, weapon, endpoint, rules, damage):
                    single.append((endpoint, damage))
            single.sort(key=lambda row: (-sum(weights.get(k, 1) * v for k, v in row[1].items()), row[0]))
            for bundle in combinations(single[:12], 1 if weapon.kind == "railgun" else weapon.level):
                impacts = tuple(p for p, _ in bundle)
                if weapon.kind == "gatling":
                    vectors = [(p[0]-weapon.pos[0], p[1]-weapon.pos[1]) for p in impacts]
                    if any(a[0]*b[0]+a[1]*b[1] < 0 for a in vectors for b in vectors):
                        continue
                damage = {}
                for _, d in bundle:
                    for identity, amount in d.items():
                        damage[identity] = damage.get(identity, 0) + amount
                if weapon.kind == "gatling" and not damage:
                    continue
                # Unknown rail energy remains a heuristic preference, not an
                # invented attackPower-to-energy conversion or kill prediction.
                utility = 8.0 if weapon.kind == "railgun" and not damage else 0.1
                options.append((impacts, damage, utility))
        options.sort(key=lambda row: (-sum(weights[k]*min(world.robots[k].health, d) for k, d in row[1].items())-row[2], row[0]))
        if weapon.kind == "gatling":
            # Different legal endpoints can describe the same volley effect.
            # Keep one representative so aliases do not consume the candidate
            # budget needed for another gun's complementary target allocation.
            distinct, seen = [], set()
            for option in options:
                signature = tuple(sorted(option[1].items()))
                if signature not in seen:
                    seen.add(signature)
                    distinct.append(option)
            options = distinct
        retained = options[:8]
        if focused:
            # Reserve bounded candidate diversity before the shared arbiter;
            # never rewrite a selected response outside the transaction.
            retained, seen = [], set()
            for option in focused + options:
                signature = tuple(sorted(option[1].items()))
                if signature not in seen:
                    retained.append(option)
                    seen.add(signature)
                if len(retained) == 8:
                    break
        if weapon.kind == 'rocket' and rocket_diversity_enabled:
            retained = diverse_rocket_options(world, single, weapon.level, weights, retained, deadline)
        for impacts, damage, utility in retained:
            for controller in controllers:
                result.append(Candidate(weapon.id, {"action": "attack", "controllerId": controller.id,
                                                    "targetPos": [pos_json(p) for p in impacts]},
                                        utility, "joint fire candidate; forecasts remain unconfirmed", damage))
    fire_complete = time.monotonic() < deadline
    result.extend(propose_consumables(world, deadline, task_actor))
    from .forage_admission import attack_key
    # Keep damage separate from mutable utility metadata: joint_fire_enabled
    # may later fold Candidate.damage into a score and clear that dictionary.
    effects = {attack_key(c.actor, c.command): dict(c.damage) for c in result
               if c.command.get('action') == 'attack'}
    world.combat_fire_observation = {'round': world.round, 'complete': fire_complete, 'effects': effects}
    return result


def area_targets(world, robots, deadline, forbidden=frozenset()):
    """All bounded 3x3 centres, including empty cells between targets.

    Build inverse coverage in O(9*robots), without scanning attackRange or
    scoring partially accumulated coverage when the budget expires.
    """
    coverage = {}
    for robot in robots:
        if time.monotonic() >= deadline:
            return []
        for p in neighbours(robot.pos) + [robot.pos]:
            if world.inside(p) and p not in forbidden:
                coverage.setdefault(p, set()).add(robot.id)
    seen, result = set(), []
    for p, identities in sorted(coverage.items()):
        signature = frozenset(identities)
        if signature not in seen:
            result.append((p, signature))
            seen.add(signature)
    return result


def propose_consumables(world, deadline, task_actor=None):
    roster = getattr(world, 'night_roster', None)
    actors = [u for u in world.movers if u.id != task_actor
              and (roster is None or u.id in (roster.p, roster.m))
              and not cleanup(world)
              and (u.inventory["Bomb"] or u.inventory["DizzyWeapon"])]
    if not actors:
        return []
    weights = threat_weights(world)
    robots = [r for r in world.robots.values() if eligible(world,r)]
    centres = area_targets(world, robots, deadline, protected_area(world))
    bombs = sorted(centres, key=lambda row: (-sum(weights[i]*min(world.robots[i].health, 100)
                                               for i in sorted(row[1])), row[0]))
    # Different centres that only differ by already dizzy units are equivalent.
    controls, seen = [], set()
    for p, identities in centres:
        targets = frozenset(i for i in identities if world.robots[i].abnormal != "dizzy")
        if targets and targets not in seen:
            controls.append((p, targets))
            seen.add(targets)
    controls.sort(key=lambda row: (-sum(weights[i]*15 for i in sorted(row[1])), row[0]))
    result = []
    for actor in actors:
        if time.monotonic() >= deadline:
            break
        if actor.inventory["Bomb"]:
            for p, identities in bombs[:4]:
                result.append(Candidate(actor.id, {"action": "use", "name": "Bomb", "targetPos": [pos_json(p)]},
                                        -40, "joint bomb coverage competes with personal stock and controller turn",
                                        {i: 100 for i in sorted(identities)}))
        if actor.inventory["DizzyWeapon"]:
            actor_controls=controls
            from .rear_open import enabled as rear_enabled
            if rear_enabled(world) and actor.id==world.night_roster.p:
                # The pioneer keeps control stock for an actual
                # defensive window. A distant dense spawn is not evidence
                # that stunning it now protects the miner or a wall.
                assets=world.movers+world.stations+[
                    u for u in world.ours.values() if u.alive and u.kind=='wall'
                    and getattr(world,'observed_wall_losses',{}).get(u.id,0)>0]
                motion=getattr(world,'observed_robot_motion',{})
                # Use the same labelled straight-motion scenario as exterior
                # defence: waiting for actual contact can miss the last control
                # window. No displacement is invented for a newly seen robot.
                projected={r.id:(r.pos[0]+motion.get(r.id,(0,0))[0],
                                 r.pos[1]+motion.get(r.id,(0,0))[1]) for r in robots}
                immediate={r.id for r in robots if r.attack_power is not None and r.attack_power>0
                    and r.attack_range is not None and any(
                        min(distance(r.pos,q),distance(projected[r.id],q))<=r.attack_range
                        for u in assets for q in u.cells)}
                actor_controls=[row for row in controls if row[1]&immediate]
                if actor.inventory['DizzyWeapon']==1:
                    # The final personal control must cover a survival window,
                    # using the same two-opportunity bound as guard repairs.
                    # Healthy wall pressure cannot spend the only remaining
                    # answer to an exposed mover's lethal pursuers.
                    known=[r for r in robots if r.attack_power is not None
                           and r.attack_range is not None and r.abnormal!='dizzy']
                    critical=set()
                    protected=world.movers+world.stations+[
                        u for u in world.ours.values() if u.alive and u.kind=='wall']
                    for unit in protected:
                        sources=[r for r in known if any(
                            distance(r.pos,q)<=r.attack_range for q in unit.cells)]
                        if 2*sum(r.attack_power for r in sources)>=unit.health:
                            critical.update(r.id for r in sources)
                    actor_controls=[row for row in actor_controls if row[1]&critical]
            for p, identities in actor_controls[:4]:
                result.append(Candidate(actor.id, {"action": "use", "name": "DizzyWeapon", "targetPos": [pos_json(p)]},
                                        -35, "joint future suppression coverage; uncalibrated opportunity value",
                                        suppression=identities))
    if roster:
        # Preserve M's existing personal escape stock only against a robot
        # already able to hit M. Ordinary offensive supplies belong to P.
        miner=world.ours.get(roster.m)
        direct={r.id for r in robots if miner and r.abnormal!='dizzy'
                and r.attack_range is not None and r.attack_power is not None and r.attack_power>0
                and distance(miner.pos,r.pos)<=r.attack_range}
        result=[c for c in result if c.actor!=roster.m or (set(c.damage)|set(c.suppression))&direct]
        world.emergency_consumable_actions={roster.m:[c.command for c in result if c.actor==roster.m]}
    return result
