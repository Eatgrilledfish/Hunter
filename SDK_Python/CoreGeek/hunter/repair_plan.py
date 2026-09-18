"""Personal wall repairs with observed cooldowns and real return paths."""
from dataclasses import dataclass, field
from collections import Counter
import time

from .arbitration import Candidate
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import distance, pos_json
from .night_roles import defender_ids
from . import defence_duties, repair_decision
from .robot_threats import active as active_robots


def damaged_walls(world, rules):
    return [u for u in world.ours.values() if repair_decision.eligible(world, u, rules)]


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
    night_usage: dict = field(default_factory=dict)
    usage_observed_round: int | None = None
    loss_samples: dict = field(default_factory=dict)
    unserved_stock: dict = field(default_factory=dict)

    def publish_demand(self, world, clock, rules, policy, deadline):
        """Price the same personal stock before any supplier claims the worker."""
        from .rear_open import enabled
        world.day_repair_demand=0
        if not enabled(world):return
        actor=world.ours.get(world.night_roster.w)
        pending=self.active.get(actor.id) if actor else None
        if (pending and pending.get('phase')=='USE_PENDING'
                and pending['command'].get('name')=='WallFixer' and actor.backpack is not None
                and world.round==pending['round']+1 and self.usage_observed_round!=world.round
                and actor.inventory['WallFixer']<pending['inventory_before']):
            day=pending.get('day',clock.day)
            self.night_usage[day]=self.night_usage.get(day,0)+1
            self.usage_observed_round=world.round
        previous_day=(clock.day or 0)-1
        world.caretaker_repair_target=repair_decision.carry_target(clock,policy,
            self.night_usage.get(previous_day,0),len(self.unserved_stock.get(previous_day,set())))
        if clock.phases!={'day'} or not actor or not actor.alive or actor.backpack is None:return
        reachable=distance_field(world,{actor.pos},actor.pos,deadline)
        home=distance_field(world,defence_duties.stands(world,actor.id),actor.pos,deadline)
        from .procurement import upgrade_allowed
        from .wall_service import use_permitted
        from .day_maintenance import owner
        walls=[u for u in damaged_walls(world,rules) if owner(world,u) in (None,actor.id)]
        for wall in world.ours.values():
            if (wall.alive and wall.kind=='wall' and wall.level==1 and actor.inventory['WallUpgradeVoucher1']
                    and wall.health is not None and rules.health_limit(world,wall)
                    and wall.health<rules.health_limit(world,wall) and upgrade_allowed(world,wall,policy,rules)
                    and use_permitted(world,Candidate(actor.id,dict(action='use',name='WallUpgradeVoucher1',
                        targetPos=[pos_json(wall.pos)]),260,'direct restoration'))):walls.append(wall)
        options=[(w.health,reachable[p],w.id,p,w) for w in walls
                 for p in interaction_cells(world,[w.pos],actor.pos) if p in reachable and p in home
                 and reachable[p]+1+home[p]+policy.return_buffer<=clock.until_night]
        if time.monotonic()>=deadline:return
        from .procurement import upgrade_allowed
        from .wall_service import use_permitted
        supplies=actor.inventory.copy();items={}
        for _,_,identity,_,wall in sorted(options,key=lambda x:x[:4]):
            if identity in items:continue
            coupon=f'WallUpgradeVoucher{wall.level}'
            candidate=Candidate(actor.id,dict(action='use',name=coupon,targetPos=[pos_json(wall.pos)]),260,'paid restoration')
            item='WallFixer'
            if (wall.level in (1,2) and supplies[coupon]>0 and upgrade_allowed(world,wall,policy,rules)
                    and use_permitted(world,candidate)):
                item=coupon;supplies[coupon]-=1
            items[identity]=item
        # Never spend a pack on a daylight level-one wall. Another damaged
        # wall may have consumed the one available coupon during matching.
        options=[row for row in options if row[4].level!=1 or items[row[2]]!='WallFixer']
        live={row[2] for row in options};items={k:v for k,v in items.items() if k in live}
        world.day_repair_demand=sum(name=='WallFixer' for name in items.values())
        world.day_repair_quote=(options,items)

    def prepare(self, world, clock, rules, policy, deadline, task=None):
        self.offered = {}
        self.service = {}
        self.diagnostic = {}
        world.repair_commands = {}
        world.repair_holds = {}
        world.day_repair_steps = {}
        world.day_repair_demand = getattr(world,'day_repair_demand',0)
        world.day_repair_urgent = False
        world.repair_service = self.service
        self.loss_samples={i:r for i,r in self.loss_samples.items()
                           if world.round-r['round']<=6 and i in world.ours
                           and world.ours[i].alive and world.ours[i].level==r['level']}
        for identity,loss in getattr(world,'observed_wall_losses',{}).items():
            if loss>0 and identity in world.ours:
                self.loss_samples[identity]=dict(loss=loss,round=world.round,level=world.ours[identity].level)
        plan = getattr(world, 'task_side_plan', None)
        from .rear_open import enabled as rear_enabled
        if rear_enabled(world):
            actor=world.ours.get(world.night_roster.w)
            pending=self.active.get(actor.id) if actor else None
            if (pending and pending.get('phase')=='USE_PENDING'
                    and pending['command'].get('name')=='WallFixer'
                    and actor.backpack is not None and world.round==pending['round']+1
                    and self.usage_observed_round!=world.round
                    and actor.inventory['WallFixer']<pending['inventory_before']):
                day=pending.get('day',clock.day)
                self.night_usage[day]=self.night_usage.get(day,0)+1
                self.usage_observed_round=world.round
            previous_day=(clock.day or 0)-1
            world.caretaker_repair_target=repair_decision.carry_target(clock,policy,
                self.night_usage.get(previous_day,0),len(self.unserved_stock.get(previous_day,set())))
        world.repair_policy_active = bool(policy.repair_plan_enabled and plan)
        if world.repair_policy_active and defence_duties.enabled(world) and clock.phases == {'day'}:
            self.active={}
            actor=world.ours.get(defence_duties.caretaker(world))
            if actor and actor.alive and actor.backpack is not None:
                if not hasattr(world,'day_repair_quote'):
                    self.publish_demand(world,clock,rules,policy,deadline)
                options,items=getattr(world,'day_repair_quote',([],{}))
                # An upgrade restores full health under the official rules.
                # Allocate each personally held voucher once, with the same
                # paid-target restrictions as the final use validator.
                from .procurement import upgrade_allowed
                from .wall_service import use_permitted
                world.day_repair_demand=sum(item=='WallFixer' for item in items.values())
                from .day_maintenance import owner
                upgrading={r.get('target_id') for r in getattr(world,'funding_plan',())
                           if r['purpose']=='wall_upgrade' and r['granted']>=r['cost']}
                funded=[row for row in options if actor.inventory[items[row[2]]]>0
                        and owner(world,row[4]) in (None,actor.id)
                        and not (items[row[2]]=='WallFixer' and row[2] in upgrading)]
                if funded and time.monotonic()<deadline:
                    _,length,_,stand,wall=min(funded,key=lambda x:x[:4])
                    item=items[wall.id]
                    from .day_schedule import DaySchedule
                    choices=([Candidate(actor.id,dict(action='use',name=item,targetPos=[pos_json(wall.pos)]),
                                        260,'daytime personal wall repair')] if not length else
                             DaySchedule.moves(actor,distance_field(world,{stand},actor.pos,deadline),
                                               'repair damaged wall before harvesting'))
                    world.repair_commands[actor.id]=[c.command for c in choices]
                    world.maintenance_targets=dict(getattr(world,'maintenance_targets',{}))
                    world.maintenance_targets[wall.id]=actor.id
                    world.day_repair_steps[actor.id]=length+1
                    world.day_repair_urgent=(wall.health<=rules.health_limit(world,wall)*policy.wall_repair_health_fraction
                                            or pressure(world,wall) not in (None,0))
                    self.diagnostic[actor.id]=dict(phase='DAY_REPAIR',wall=wall.id,remaining_actions=length+1,
                                                   item=item,reason='personal restoration before daytime harvest')
                    return choices
            return []
        if not world.repair_policy_active or clock.phases != {'night'}:
            self.active = {}
            return []
        from .rear_open import enabled as rear_enabled
        if rear_enabled(world):
            return self._rear_service(world, clock, rules, policy, deadline)
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
        for identity in (roster.w, roster.p):
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
            if maintenance_mode and identity==defence_duties.rotator(world):
                physical=active_robots(world)
                if any(r.attack_range is None or r.attack_power is None for r in physical):continue
                for r in physical:
                    if r.attack_power<=0:continue
                    radius=r.attack_range+1
                    blocked.update((x,y) for x in range(max(0,r.pos[0]-radius),min(world.width,r.pos[0]+radius+1))
                                   for y in range(max(0,r.pos[1]-radius),min(world.height,r.pos[1]+radius+1)))
            if time.monotonic() >= deadline:
                break
            routing=world
            if maintenance_mode and identity==defence_duties.rotator(world):
                from copy import copy
                routing=copy(world)
                routing.navigation_avoided={p:set(cells) for p,cells in world.navigation_avoided.items()}
            routing.navigation_avoided.setdefault(actor.pos, set()).update(blocked - {actor.pos})
            current = self.active.get(identity)
            if identity == defence_duties.rotator(world) and not maintenance_mode:
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
            if maintenance_mode and identity == defence_duties.rotator(world):
                c_stands = {plan['w']}
            if current and current['phase'] == 'RETURN_C':
                home = distance_field(routing, c_stands, actor.pos, deadline)
                if time.monotonic() >= deadline:
                    break
                result.extend(self._return(actor, home, current, world, service_context))
                continue
            if not actor.inventory['WallFixer'] and not (maintenance_mode and any(
                    actor.inventory[name] for name in ('WallUpgradeVoucher1','WallUpgradeVoucher2'))):
                unmet = [wall for wall in walls if repair_decision.eligible(world, wall, rules, policy)]
                if unmet:
                    wall = min(unmet, key=lambda w:(w.pos not in getattr(world,'monster_front_walls',()),w.health,w.id))
                    self.diagnostic[identity] = dict(phase='NO_STOCK',wall=wall.id,stock=0,
                        reason='observed repair demand lacks a personally carried repair item',
                        observed_cooldown=window)
                if current:
                    current['phase'] = 'RETURN_C'
                    home = distance_field(routing, c_stands, actor.pos, deadline)
                    if time.monotonic() >= deadline:
                        break
                    result.extend(self._return(actor, home, current, world, service_context))
                continue
            from .guard_risk import evidence as guard_evidence
            self.diagnostic[identity]=dict(phase='WAIT',wall=None,remaining_actions=None,
                risk=guard_evidence(world,actor,actor.pos),
                observed_cooldown=window,reason='no damaged wall with a safe service route',stock=actor.inventory['WallFixer'])
            if identity == defence_duties.rotator(world) and actor.pos != plan['w'] and not current:
                continue
            if identity == defence_duties.caretaker(world) and not current and actor.pos not in c_stands:
                continue  # An unrelated excursion cannot become a repair commitment.
            outgoing = distance_field(routing, [actor.pos], actor.pos, deadline)
            home = distance_field(routing, c_stands, actor.pos, deadline) if maintenance_mode or identity == defence_duties.caretaker(world) else {}
            options = []
            for wall in walls:
                if current and wall.id != current['wall']:
                    continue
                maximum=rules.health_limit(world,wall)
                wall_pressure = exposure[wall.pos]
                upgrade_item = None
                if maintenance_mode and wall.level in (1,2) and actor.inventory[f'WallUpgradeVoucher{wall.level}']:
                    from .procurement import upgrade_allowed
                    if upgrade_allowed(world,wall,policy,rules):upgrade_item=f'WallUpgradeVoucher{wall.level}'
                if upgrade_item is None and not actor.inventory['WallFixer']:
                    continue
                emergency = wall_pressure >= wall.health
                stands = interaction_cells(routing, [wall.pos], actor.pos)
                if identity == defence_duties.rotator(world) and not maintenance_mode or is_task:
                    stands &= {actor.pos}
                stands -= {plan['w']} if identity == defence_duties.caretaker(world) else set()
                for stand in stands & outgoing.keys():
                    item=upgrade_item
                    # Cooldown bounds forgone firing opportunities, not time
                    # until a worker can apply the pack. Judge each real stand
                    # using its outbound path plus the use action; returning
                    # to the gun happens after the wall has been restored.
                    if item is None and repair_decision.eligible(world,wall,rules,policy,
                            service_steps=outgoing[stand]+1,pressure=wall_pressure):
                        item='WallFixer'
                    if item is None:continue
                    back = home.get(stand) if maintenance_mode or identity == defence_duties.caretaker(world) else 0
                    if back is None:
                        continue
                    actions = outgoing[stand] + 1 + back
                    delayed = window is None or actions > window
                    if maintenance_mode and identity==defence_duties.rotator(world):
                        own_remaining=any(r.alive and r.target_team in (None,world.side) for r in world.robots.values())
                        observed=isinstance(world.raw.get('robot'),dict) and isinstance(world.raw['robot'].get('roles'),list)
                        remaining=min(130-(clock.round-o)%130 for o in clock.offsets)
                        if delayed and (own_remaining or not observed):continue
                        if not current and actions+policy.return_buffer>remaining:continue
                    no_target = (known_service and not any(distance(g.pos,r.pos)<=g.attack_range+1
                        for g in guns for r in threats))
                    maintenance = defence_duties.enabled(world) and (no_target or stand==actor.pos
                        or wall_pressure*actions>=wall.health*2)
                    # Use an observed idle cooldown to prevent damage, not
                    # only after the wall has fallen below the day threshold.
                    # Light damage does not justify abandoning a ready gun.
                    if maintenance_mode and maximum and wall.health > maximum*policy.wall_repair_health_fraction and delayed and not emergency and not no_target:
                        continue
                    if delayed and not emergency and not maintenance:
                        continue
                    # No repair detour enters an observed lethal exposure.
                    if any(u.attack_range is None or u.attack_power is None for u in threats):
                        continue
                    if exposure[stand]>=actor.health:
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
                route = distance_field(routing, [stand], actor.pos, deadline)
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

    def _rear_service(self, world, clock, rules, policy, deadline):
        """The worker services walls, with no fictional cannon cooldown debt."""
        from .rear_open import required
        from .guard_risk import evidence
        from copy import copy
        actor = world.ours.get(world.night_roster.w)
        if not actor or not actor.alive or actor.backpack is None or actor.abnormal == 'dizzy':
            return []
        personal=evidence(world,actor,actor.pos)
        if personal['withdraw']:
            self.active.pop(actor.id,None)
            self.diagnostic[actor.id]=dict(phase='PERSONAL_RESCUE',stock=actor.inventory['WallFixer'],
                reason='recent personal hit may be lethal again',risk=personal)
            return []
        previous = self.active.get(actor.id)
        receipt = None
        if previous and previous['phase'] == 'USE_PENDING' and world.round > previous['round']:
            target = world.ours.get(previous['wall'])
            item = previous['command']['name']
            feedback = world.raw.get('lastRoundRoleActionResults')
            receipt = dict(round=world.round, wall=previous['wall'], item=item,
                consumed=actor.inventory[item] < previous['inventory_before'],
                same_target=bool(target and target.alive and target.pos == previous['target_position']),
                hp=target.health if target else None,
                feedback=feedback.get(actor.id) if isinstance(feedback, dict) else None)
        # Every observation can change the most urgent reachable wall. Only
        # finalized offers record pending uses; failed movement never advances.
        self.active.pop(actor.id, None)
        home = defence_duties.stands(world, actor.id)
        view = copy(world)
        risks={q:evidence(world,actor,q) for q in home}
        lethal = {q for q in home if risks[q]['lethal']}
        view.navigation_avoided = {p:set(c) for p,c in world.navigation_avoided.items()}
        view.navigation_avoided.setdefault(actor.pos,set()).update(lethal | {world.task_side_plan['w']})
        reach = distance_field(view, {actor.pos}, actor.pos, deadline)
        candidates = []
        rejected=Counter()
        for wall in world.ours.values():
            if not wall.alive or wall.kind != 'wall' or wall.pos not in required(world):
                continue
            maximum=rules.health_limit(world,wall)
            if maximum and wall.health is not None and wall.health>=maximum:continue
            hit = pressure(world, wall)
            service_cells=interaction_cells(world,[wall.pos],actor.pos)&home
            safe_cells={p for p in service_cells if not risks[p]['lethal']}
            if not safe_cells:rejected['NO_SAFE_SERVICE']+=1
            elif not safe_cells & reach.keys():
                rejected['NO_SERVICE_ROUTE' if reach.complete else 'ROUTE_BUDGET_EXHAUSTED']+=1
            for stand in safe_cells & reach.keys():
                steps = reach[stand]+1
                ready=repair_decision.eligible(world,wall,rules,policy,service_steps=steps,pressure=hit or 0)
                soon=repair_decision.eligible(world,wall,rules,policy,
                    service_steps=steps+policy.return_buffer,pressure=hit or 0)
                if not ready and not soon:
                    continue
                item = 'WallFixer' if actor.inventory['WallFixer'] else None
                if wall.level in (1,2) and actor.inventory[f'WallUpgradeVoucher{wall.level}']:
                    from .procurement import upgrade_allowed
                    from .wall_service import use_permitted
                    coupon=f'WallUpgradeVoucher{wall.level}'
                    use=Candidate(actor.id,dict(action='use',name=coupon,targetPos=[pos_json(wall.pos)]),0,'repair coupon')
                    if upgrade_allowed(world,wall,policy,rules) and use_permitted(world,use):item=coupon
                    else:rejected['COUPON_RESERVED']+=1
                if item:
                    loss=self.loss_samples.get(wall.id,{}).get('loss',0) if hit else 0
                    window=(wall.health+loss-1)//loss if loss else None
                    if window is not None and steps>1 and steps>=window:
                        rejected['ARRIVAL_TOO_LATE']+=1
                        continue
                    slack=window-steps if window is not None else float('inf')
                    prior_command=(previous or {}).get('command') or {}
                    continuing=bool(previous and previous.get('wall')==wall.id
                        and previous.get('target_position')==wall.pos and previous.get('target_level',wall.level)==wall.level
                        and previous.get('round')==world.round-1
                        and (prior_command.get('action')=='move'
                             and prior_command.get('targetPos')==[pos_json(actor.pos)]
                             or previous.get('phase')=='HOLD_REPAIR' and previous.get('stand')==actor.pos))
                    # Finish an available use instead of leaving a repairable
                    # adjacent wall. Otherwise retain an observed successful
                    # approach unless another wall has its last service turn.
                    last_window=window is not None and window<=steps+1
                    candidates.append((not ready,steps!=1,not last_window,not continuing,slack,wall.pos not in world.monster_front_walls,
                                       steps,wall.health,wall.id,stand,item,wall))
                elif not item:
                    rejected['NO_STOCK']+=1
                    loss=self.loss_samples.get(wall.id,{}).get('loss',0) if hit else 0
                    if not loss or steps*loss<wall.health:
                        self.unserved_stock.setdefault(clock.day,set()).add(wall.id)
        self.diagnostic[actor.id] = dict(phase='HOLD_FRONT', stock=actor.inventory['WallFixer'],
            minimum=2, target=getattr(world,'caretaker_repair_target',max(2,policy.caretaker_repair_target)), receipt=receipt,
            reason='NO_SERVICE_REQUIRED',blocked=dict(rejected),risk=dict(worker_hp=actor.health,
                min_two_hit_upper=min((r['two_opportunity_upper'] for r in risks.values()
                                      if r['two_opportunity_upper'] is not None),default=None),
                unknown_cells=sum(r['unknown_attack'] for r in risks.values()),
                actual_recent_loss=personal['actual_recent_loss'],withdraw=personal['withdraw'],
                basis=personal['basis']))
        if not candidates or time.monotonic() >= deadline:
            self.diagnostic[actor.id]['reason']=('BUDGET_EXHAUSTED' if time.monotonic()>=deadline else
                rejected.most_common(1)[0][0] if rejected else 'NO_SERVICE_REQUIRED')
            return []
        preposition,_,_,_,slack,_,steps,_,_,stand,item,wall=min(candidates,key=lambda row:row[:10])
        if preposition and stand==actor.pos:
            # No invented wait command. Keep this safe service position while
            # the freshly observed damage/pressure still justifies staging;
            # medicine and control consumables remain legal via guidance.
            state=dict(wall=wall.id,phase='HOLD_REPAIR',round=world.round,command=None,
                inventory_before=actor.inventory[item],target_position=wall.pos,target_level=wall.level,
                stand=stand,day=clock.day)
            self.offered.setdefault(actor.id,[]).append(state)
            world.repair_holds[actor.id]=dict(wall=wall.id,stand=stand)
            self.diagnostic[actor.id].update(phase='HOLD_REPAIR',wall=wall.id,remaining_actions=1,
                preposition=True,reason='safe service position reached; wait for fresh repair threshold')
            return []
        route=distance_field(view,{stand},actor.pos,deadline)
        commands=([dict(action='use',name=item,targetPos=[pos_json(wall.pos)])] if actor.pos==stand else
                  [dict(action='move',targetPos=[pos_json(q)]) for q in sorted(neighbours(actor.pos))
                   if route.get(q,float('inf')) < route.get(actor.pos,0)][:4])
        if not commands or time.monotonic() >= deadline:
            return []
        result=[]
        for command in commands:
            phase='USE_PENDING' if command['action']=='use' else 'MOVE_TO_REPAIR'
            state=dict(wall=wall.id,phase=phase,round=world.round,command=command,
                inventory_before=actor.inventory[item],target_position=wall.pos,target_level=wall.level,day=clock.day)
            self.offered.setdefault(actor.id,[]).append(state)
            world.repair_commands.setdefault(actor.id,[]).append(command)
            result.append(Candidate(actor.id,command,90,'front maintenance: '+phase))
        self.diagnostic[actor.id].update(phase=phase,wall=wall.id,remaining_actions=steps,
            preposition=preposition,observed_window_slack=None if slack==float('inf') else slack,
            reason='actual wall service without cannon duty')
        return result

    def finalize(self, world, response):
        for identity, offers in self.offered.items():
            for state in offers:
                command=response['roleCommandMap'].get(identity)
                stationary_control=(state.get('phase')=='HOLD_REPAIR' and command
                    and command.get('action')=='use' and command.get('name') in {'Medicine','Bomb','DizzyWeapon'})
                if command == state['command'] or stationary_control:
                    self.active[identity] = dict(state)
                    self.diagnostic[identity]["selected"] = True
                    break
        self.offered = {}
