"""Personal wall repairs with observed cooldowns and real return paths."""
from dataclasses import dataclass, field
from collections import Counter
import time

from .arbitration import Candidate
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import distance, pos_json
from .night_roles import defender_ids
from . import defence_duties
from .robot_threats import active as active_robots


def damaged_walls(world, rules):
    return [u for u in world.ours.values() if u.alive and u.kind == 'wall'
            and (maximum := rules.max_health.get('wall', {}).get(u.level))
            and u.health * 10 < maximum * 3]


def pressure(world, wall):
    """Observed two-opportunity bound, not a forecast of the robot's target."""
    threats = [u for u in active_robots(world)
               if u.target_team in (None, world.side)]
    if any(u.attack_power is None or u.attack_range is None for u in threats):
        return None
    return 2 * sum(u.attack_power for u in threats if distance(u.pos, wall.pos) <= u.attack_range)


@dataclass
class RepairPlan:
    active: dict = field(default_factory=dict)
    offered: dict = field(default_factory=dict)
    service: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)

    def prepare(self, world, clock, rules, policy, deadline, task=None):
        self.offered = {}
        self.service = {}
        self.diagnostic = {}
        world.repair_commands = {}
        world.day_repair_steps = {}
        world.repair_service = self.service
        plan = getattr(world, 'task_side_plan', None)
        world.repair_policy_active = bool(policy.repair_plan_enabled and plan)
        if world.repair_policy_active and defence_duties.enabled(world) and clock.phases == {'day'}:
            self.active={}
            actor=world.ours.get(defence_duties.caretaker(world))
            if actor and actor.alive and actor.backpack is not None and actor.inventory['WallFixer']:
                reachable=distance_field(world,{actor.pos},actor.pos,deadline)
                options=[(w.health,reachable[p],w.id,p,w) for w in damaged_walls(world,rules)
                         for p in interaction_cells(world,[w.pos],actor.pos) if p in reachable]
                if options and time.monotonic()<deadline:
                    _,length,_,stand,wall=min(options,key=lambda x:x[:4])
                    from .day_schedule import DaySchedule
                    choices=([Candidate(actor.id,dict(action='use',name='WallFixer',targetPos=[pos_json(wall.pos)]),
                                        260,'daytime personal wall repair')] if not length else
                             DaySchedule.moves(actor,distance_field(world,{stand},actor.pos,deadline),
                                               'repair damaged wall before harvesting'))
                    world.repair_commands[actor.id]=[c.command for c in choices]
                    world.day_repair_steps[actor.id]=length+1
                    self.diagnostic[actor.id]=dict(phase='DAY_REPAIR',wall=wall.id,remaining_actions=length+1,
                                                   reason='personal repair route before daytime harvest')
                    return choices
            return []
        if not world.repair_policy_active or clock.phases != {'night'}:
            self.active = {}
            return []
        defender_ids(world)
        roster = world.night_roster
        for identity in set(self.active) - {roster.w, roster.p}:
            self.active.pop(identity, None)
        c = next((g for g in world.weapons if g.pos == plan['c']), None)
        maintenance_mode = defence_duties.enabled(world)
        walls = ([u for u in world.ours.values() if u.alive and u.kind=='wall']
                 if maintenance_mode else damaged_walls(world, rules))
        threats = [u for u in active_robots(world)
                   if u.target_team in (None,world.side)]
        if any(u.attack_range is None or u.attack_power is None for u in threats):
            self.diagnostic={'status':'UNKNOWN_ROBOT_ATTACK'}
            return []
        # Accumulate each observed robot's local range once. Scanning every
        # map cell against every robot exhausted the repair slice in large waves.
        exposure=Counter()
        for robot in threats:
            radius=robot.attack_range
            for x in range(max(0,robot.pos[0]-radius),min(world.width,robot.pos[0]+radius+1)):
                for y in range(max(0,robot.pos[1]-radius),min(world.height,robot.pos[1]+radius+1)):
                    exposure[x,y]+=2*robot.attack_power
                if time.monotonic()>=deadline:break
            if time.monotonic()>=deadline:break
        result = []
        for identity in ((roster.w,) if defence_duties.enabled(world) else (roster.w, roster.p)):
            if time.monotonic() >= deadline:
                break
            actor = world.ours.get(identity)
            expected_kind = 'worker' if identity == roster.w else 'pioneer'
            if (not actor or not actor.alive or actor.kind != expected_kind
                    or identity not in defender_ids(world)):
                self.active.pop(identity, None)
                continue
            if actor.abnormal == 'dizzy':
                continue
            if identity in world.roster_yielding or roster.handoff_requested or roster.traffic or roster.exit_pending:
                continue
            is_task = bool(world.phase_task and identity == roster.p)
            if is_task and (not task or task.answer is not None or
                            not any(distance(actor.pos, q) <= 1 for q in task.cells) or
                            actor.pos not in plan['c_stands']):
                continue
            blocked = {plan['w']} if identity == defence_duties.caretaker(world) else set()
            if world.width * world.height > 41 * 32:
                continue
            blocked.update(q for q,damage in exposure.items() if damage>=actor.health)
            if time.monotonic() >= deadline:
                break
            world.navigation_avoided.setdefault(actor.pos, set()).update(blocked - {actor.pos})
            current = self.active.get(identity)
            if identity == defence_duties.rotator(world):
                self.active.pop(identity, None)
                current = None
            guns = ([g for g in world.weapons if g.pos in (plan['a'], plan['b'])]
                    if identity == defence_duties.rotator(world) else ([c] if c else []))
            gun_count = 2 if identity == defence_duties.rotator(world) else 1
            served = tuple(g.id for g in sorted(guns, key=lambda g: g.id))
            served_weapon = served if identity == defence_duties.rotator(world) else (served[0] if served else None)
            window = (min(g.cooldown for g in guns)
                      if len(guns) == gun_count and all(g.cooldown is not None for g in guns)
                      else None)
            continuing = self._continuing(actor, current, world, served_weapon)
            known_service = bool(len(guns) == gun_count and actor.backpack is not None
                                 and all(g.kind == 'rocket' and g.level in (1, 2, 3)
                                         and g.attack_range is not None and g.attack_range > 0
                                         and g.cooldown is not None for g in guns))
            service_context = dict(served_weapon=served_weapon, observed_cooldown=window,
                                   continuing_selected_repair=continuing,
                                   known_service=known_service)
            if current and current['round'] < world.round:
                feedback = world.raw.get('lastRoundRoleActionResults', {})
                failed = isinstance(feedback, dict) and feedback.get(identity) is False
                previous = current.get('command', {})
                target = previous.get('targetPos', [])
                move_not_observed = (previous.get('action') == 'move' and target
                                     and pos_json(actor.pos) != target[0])
                if (current['phase'] == 'USE_PENDING' or failed or move_not_observed or
                        world.round != current['round'] + 1):
                    current['phase'] = 'RETURN_C'
            if current and not continuing:
                current['phase'] = 'RETURN_C'
            c_stands = {q for q in plan['c_stands'] if c and distance(q, c.pos) <= 1}
            c_stands -= {plan['w']} | getattr(world, 'operator_excluded_cells', set())
            if current and current['phase'] == 'RETURN_C':
                home = distance_field(world, c_stands, actor.pos, deadline)
                if time.monotonic() >= deadline:
                    break
                result.extend(self._return(actor, home, current, world, service_context))
                continue
            if not actor.inventory['WallFixer'] and not (maintenance_mode and any(
                    actor.inventory[name] for name in ('WallUpgradeVoucher1','WallUpgradeVoucher2'))):
                if current:
                    current['phase'] = 'RETURN_C'
                    home = distance_field(world, c_stands, actor.pos, deadline)
                    if time.monotonic() >= deadline:
                        break
                    result.extend(self._return(actor, home, current, world, service_context))
                continue
            self.diagnostic[identity]=dict(phase='WAIT',wall=None,remaining_actions=None,
                observed_cooldown=window,reason='no damaged wall with a safe service route',stock=actor.inventory['WallFixer'])
            if identity == defence_duties.rotator(world) and actor.pos != plan['w']:
                continue
            if identity == defence_duties.caretaker(world) and not current and actor.pos not in c_stands:
                continue  # An unrelated excursion cannot become a repair commitment.
            outgoing = distance_field(world, [actor.pos], actor.pos, deadline)
            home = distance_field(world, c_stands, actor.pos, deadline) if identity == defence_duties.caretaker(world) else {}
            options = []
            for wall in walls:
                if current and wall.id != current['wall']:
                    continue
                maximum=rules.max_health.get('wall',{}).get(wall.level)
                wall_pressure = exposure[wall.pos]
                item = None
                if maintenance_mode and wall.level in (1,2) and actor.inventory[f'WallUpgradeVoucher{wall.level}']:
                    from .procurement import upgrade_allowed
                    if upgrade_allowed(world,wall,policy,rules):item=f'WallUpgradeVoucher{wall.level}'
                if item is None and actor.inventory['WallFixer'] and maximum and wall.health < maximum:
                    # A full-health restoration must recover meaningful HP.
                    # Keep early cooldown maintenance at <=70%, or act sooner
                    # when observed pressure threatens the next service window.
                    if (not maintenance_mode or wall.health*10<=maximum*7
                            or wall.health<=wall_pressure*(max(1,window or 1)+1)):
                        item='WallFixer'
                if item is None:
                    continue
                emergency = wall_pressure >= wall.health
                stands = interaction_cells(world, [wall.pos], actor.pos)
                if identity == defence_duties.rotator(world) or is_task:
                    stands &= {actor.pos}
                stands -= {plan['w']} if identity == defence_duties.caretaker(world) else set()
                for stand in stands & outgoing.keys():
                    back = home.get(stand) if identity == defence_duties.caretaker(world) else 0
                    if back is None:
                        continue
                    actions = outgoing[stand] + 1 + back
                    delayed = window is None or actions > window
                    no_target = (known_service and not any(distance(g.pos,r.pos)<=g.attack_range+1
                        for g in guns for r in threats))
                    maintenance = defence_duties.enabled(world) and (no_target or stand==actor.pos
                        or wall_pressure*actions>=wall.health*2)
                    # Use an observed idle cooldown to prevent damage, not
                    # only after the wall has fallen below the day threshold.
                    # Light damage does not justify abandoning a ready gun.
                    if maintenance_mode and maximum and wall.health*10>=maximum*3 and delayed and not emergency and not no_target:
                        continue
                    if delayed and not emergency and not maintenance:
                        continue
                    # No repair detour enters an observed lethal exposure.
                    if any(u.attack_range is None or u.attack_power is None for u in threats):
                        continue
                    if 2*sum(u.attack_power for u in threats if distance(u.pos,stand)<=u.attack_range) >= actor.health:
                        continue
                    options.append((delayed, wall.pos not in getattr(world,'monster_front_walls',()),
                                    actions, wall.health, wall.id, stand, wall, item))
            if time.monotonic() >= deadline:
                break
            if not options:
                if current:
                    current['phase'] = 'RETURN_C'
                    result.extend(self._return(actor, home, current, world, service_context))
                continue
            delayed, _, actions, _, _, stand, wall, item = min(options, key=lambda r:r[:6])
            why = ('maintenance of damaged wall permits delayed fire' if delayed and defence_duties.enabled(world)
                   else 'observed wall pressure permits delayed fire' if delayed else 'fits observed remaining cooldown')
            if stand == actor.pos:
                command = {'action':'use', 'name':item, 'targetPos':[pos_json(wall.pos)]}
                offered = self._offer(actor, command, wall.id, world, 'USE_PENDING', actions, why,
                                      service_context)
                result.append(offered)
            else:
                route = distance_field(world, [stand], actor.pos, deadline)
                if time.monotonic() >= deadline:
                    break
                result.extend(self._moves(actor, route, wall.id, world, 'GO_REPAIR', actions, why,
                                          service_context))
        if time.monotonic() >= deadline:
            # A partial distance field or a previous actor's offer is not a
            # completed frame proof. Keep the old selected commitment only.
            self.offered.clear()
            self.service.clear()
            world.repair_commands.clear()
            self.diagnostic = {'status': 'BUDGET_EXHAUSTED'}
            return []
        return result

    @staticmethod
    def _continuing(actor, current, world, served_weapon):
        if (not current or current.get('round') != world.round - 1
                or current.get('actor_kind') != actor.kind
                or current.get('served_weapon') != served_weapon
                or not current.get('repair_origin')):
            return False
        positions = {current['observed_position']}
        command = current.get('command', {})
        if command.get('action') == 'move':
            positions.update((p['x'], p['y']) for p in command.get('targetPos', []))
        return actor.pos in positions

    def _return(self, actor, home, current, world, context):
        actions = home.get(actor.pos)
        if actions == 0:
            self.active.pop(actor.id, None)
            self.diagnostic[actor.id] = dict(phase='AT_C', wall=current['wall'],
                                            remaining_actions=0, observed_cooldown=context['observed_cooldown'],
                                            delayed_fire=False, reason='arrival observed at C')
            return []
        if actions is None:
            self.diagnostic[actor.id] = dict(phase='RETURN_C', wall=current['wall'],
                                            remaining_actions=None, reason='no observed return route')
            return []
        window = context['observed_cooldown']
        why = ('return fits observed remaining cooldown' if window is not None and actions <= window
               else 'return exceeds observed cooldown' if window is not None
               else 'return cooldown unknown')
        return self._moves(actor, home, current['wall'], world, 'RETURN_C', actions, why, context)

    def _offer(self, actor, command, wall, world, phase, actions, why, context):
        current = self.active.get(actor.id)
        origin = phase != 'RETURN_C' or bool(current and current.get('repair_origin'))
        state = dict(wall=wall, phase=phase, round=world.round, command=command,
                     observed_position=actor.pos, actor_kind=actor.kind,
                     served_weapon=context['served_weapon'], repair_origin=origin)
        self.offered.setdefault(actor.id, []).append(state)
        world.repair_commands.setdefault(actor.id, []).append(command)
        window = context['observed_cooldown']
        delayed = window is None or actions > window
        self.diagnostic[actor.id] = dict(phase=phase, wall=wall, remaining_actions=actions, reason=why,
                                        observed_cooldown=window, delayed_fire=delayed)
        if (context['known_service'] and
                (phase != 'RETURN_C' or context['continuing_selected_repair'])):
            service = self.service.setdefault(actor.id, dict(
                round=world.round, observed_position=actor.pos, phase=phase, wall=wall,
                served_weapon=context['served_weapon'], observed_cooldown=window,
                remaining_actions=actions, delayed_fire=delayed, allowed_commands=[],
                continuing_selected_repair=context['continuing_selected_repair']))
            service['allowed_commands'].append(command)
        return Candidate(actor.id, command, 90, 'personal wall repair: '+phase)

    def _moves(self, actor, route, wall, world, phase, actions, why, context):
        length = route.get(actor.pos)
        if not length:
            return []
        steps = [q for q in sorted(neighbours(actor.pos)) if q in route and route[q] < length][:4]
        return [self._offer(actor, {'action':'move','targetPos':[pos_json(q)]}, wall, world, phase, actions, why,
                            context)
                for q in steps]

    def finalize(self, world, response):
        for identity, offers in self.offered.items():
            for state in offers:
                if response['roleCommandMap'].get(identity) == state['command']:
                    self.active[identity] = dict(state)
                    self.diagnostic[identity]["selected"] = True
                    break
        self.offered = {}
