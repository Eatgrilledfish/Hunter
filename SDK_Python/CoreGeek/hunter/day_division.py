"""One stone supplier and a cash worker; construction has a real daytime budget.

The tour is a conservative static route, not a prediction of mobile collisions.
Actual builds still pass LayoutGuard, and actual moves use current occupancy.
"""
from collections import Counter
from copy import copy, deepcopy
from dataclasses import dataclass, field
import time
from .navigation import distance_field, neighbours, interaction_cells
from .protocol import distance, pos_json
from .layout import LayoutGuard
from .arbitration import Candidate
from . import battery, defence_duties
from .wall_policy import planned_gate
from .night_roles import defender_ids, economic_endpoints


@dataclass
class DayDivision:
    day: int | None = None
    supplier: str | None = None
    gate: tuple | None = None
    building: bool = False
    helper_batches: dict = field(default_factory=dict)
    helper_clearance: dict = field(default_factory=dict)

    @staticmethod
    def reconcile_assistance(world, jobs):
        reserved = getattr(world, 'helper_wall_targets', ())
        if reserved and not any(j.get('helper') for j in jobs.values()):
            supplier = world.wall_assistance.get('supplier')
            if supplier in jobs:
                jobs[supplier]['stock_target'] += len(reserved)
            world.helper_wall_targets = frozenset()
            world.batch_mine_owners = {}
            world.wall_assistance.update(active=False,reason='helper has prior movement or dawn trade duty')

    def assign(self, world, clock, rules, policy, jobs, deadline):
        world.economy_first = False
        world.helper_wall_targets = frozenset()
        world.wall_assistance = {'active': False}
        world.helper_clearance_commands = {}
        if (not policy.economy_first_enabled or not policy.day_schedule_enabled
                or not policy.construction_commitment_enabled or not world.build_interior
                or world.firing_ports or len(world.weapons) != rules.weapon_limit
                or clock.phases != {'day'}):
            return jobs
        workers = [u for u in world.movers if u.kind == 'worker' and u.backpack is not None]
        if len(workers)!=2:
            return jobs
        world.economy_first = True
        world.timed_economy = True
        if clock.day != self.day:
            self.day = clock.day
            self.supplier = self.gate = None
            self.building = False
            self.helper_batches.clear()
            self.helper_clearance.clear()
        from .day_access import gate as access_gate
        fixed_gate = access_gate(world)
        enclosing = getattr(world, "wall_stage", None) != "front10"
        if fixed_gate is not None:
            self.gate = fixed_gate if enclosing else None
        missing = set(battery.missing_walls(world, rules))
        if not workers or not missing:
            return {i:j for i,j in jobs.items() if j['name'] != 'wall'}
        if defence_duties.enabled(world) and world.night_roster.w in {u.id for u in workers}:
            self.supplier = world.night_roster.w
        if self.supplier not in {u.id for u in workers}:
            choices = []
            for u in workers:
                field = distance_field(world, [u.pos], u.pos, deadline)
                travel = min((field[p] for m in world.zones.get('stone', ())
                              for p in neighbours(m) if p in field), default=world.width*world.height)
                choices.append((not (u.inventory['stone'] >= len(missing)), travel, u.id))
            self.supplier = min(choices)[-1]
        if not defence_duties.enabled(world) and len(missing)==1 and not world.ours[self.supplier].inventory['stone']:
            carriers = [u for u in workers if u.inventory['stone']]
            if carriers:
                self.supplier = min(carriers,key=lambda u:u.id).id
        actor = world.ours[self.supplier]
        topology = copy(world)
        topology.occupied = world.occupied - {u.pos for u in world.movers}
        if defence_duties.enabled(world) and getattr(world,'task_side_plan',None):
            # Match the caretaker's construction budget: its return tour
            # cannot consume the pioneer's reserved rotation position.
            topology.occupied |= {world.task_side_plan['w']}
        ring = rules.build_rule(world, 'wall').cells
        outside = {p for q in ring for p in neighbours(q) if world.inside(p) and p not in ring|world.build_interior}
        enclosing = getattr(world, "wall_stage", None) != "front10"
        if not enclosing:
            self.gate = None
        if enclosing and fixed_gate is None and self.gate not in missing:
            # Choose an exit connected to every free internal cell after the
            # other walls are built. A yellow corner behind a gun is not a door.
            options = []
            for gate in sorted(missing):
                closed = copy(topology)
                closed.occupied = topology.occupied | (missing-{gate})
                reachable = distance_field(closed, outside, actor.pos, deadline)
                free_inside = world.build_interior - closed.occupied
                missed = len(free_inside - reachable.keys())
                options.append((missed, distance(actor.pos,gate), gate))
                if time.monotonic() >= deadline:
                    break
            self.gate = min(options)[-1] if options else min(missing)
        result = {i:j for i,j in jobs.items() if j['name'] != 'wall'}
        tour = self.tour(topology, actor, missing, ring, deadline, world.occupied)
        guard=LayoutGuard(world,deadline)
        legal=[]
        for target in sorted(missing,key=lambda p:(distance(actor.pos,p),p)):
            if target in world.occupied:continue
            preview=Candidate(actor.id,{'action':'build','name':'wall','targetPos':[pos_json(target)]},0,'day wall job')
            if guard.check([preview])[0]:
                legal.append(target);break
        job = {'name':'wall', 'target':legal[0] if legal else (self.gate if self.gate is not None else min(missing)),
               'items':Counter(rules.build_rule(world,'wall').items), 'stock_target':len(missing),
               'economy_first':True, 'gate':enclosing and missing == {self.gate}, 'supplier':self.supplier,
               'construction_steps':tour['steps'] if tour else None,
               'construction_entry':tour['entry'] if tour else None,
               'construction_tail':tour['tail'] if tour else None,
               'construction_endpoint':tour['endpoint'] if tour else None,
               'construction_end':tour['end'] if tour else None}
        if enclosing and missing == {self.gate}:
            self.building=False
        if tour is None or clock.until_night <= tour['steps'] + 3:
            self.building = True
        if getattr(world, 'wall_stage', None) == 'morning19' and len(missing)>1:
            self.building = True
        # Once started, finish non-gate walls without another mining/cash detour.
        job['defer_build'] = bool(tour and not self.building)
        result[self.supplier] = job
        # Personal inventory cannot be transferred. Use another worker's held
        # stone on remaining walls instead of selling it while the supplier
        # makes a redundant mining trip. Only the primary supplier mines a deficit.
        other = next(u for u in workers if u.id != self.supplier)
        if defence_duties.enabled(world):
            deficit = max(0, len(missing)-actor.inventory['stone'])
            rescue = bool(getattr(world, 'critical_base_ids', ()))
            material_steps = 0
            if deficit and tour:
                start = distance_field(topology, {actor.pos}, actor.pos, deadline)
                entry = distance_field(topology, {tour['entry']}, actor.pos, deadline)
                material_steps = min((start[p]+entry[p]+tour['tail']+deficit
                    for mine in world.zones.get('stone', ())
                    for p in interaction_cells(topology,[mine],actor.pos) if p in start and p in entry),
                    default=float('inf'))
            late = tour is None or max(tour['steps'],material_steps)+policy.return_buffer >= clock.until_night
            if late or rescue or (deficit and other.inventory['stone']) or other.id in self.helper_batches:
                helper = self.assistance(world,clock,policy,other,missing,ring,job,guard,deadline)
                if helper:
                    clearance = self.helper_clearance
                    if (clearance.get('worker') == actor.id and actor.pos == clearance.get('position')
                            and clearance.get('last_round') == world.round-1):
                        home = distance_field(world,defence_duties.stands(world,actor.id),actor.pos,deadline)
                        if home.get(actor.pos,float('inf'))+policy.return_buffer+1 < clock.until_night:
                            world.helper_clearance_commands = {actor.id: []}
                            clearance['last_round'] = world.round
                        else:
                            self.helper_clearance.clear()
                    result[other.id] = helper
                    job['stock_target'] -= len(helper['helper_targets'])
                    world.helper_wall_targets = frozenset(helper['helper_targets'])
                    remaining = missing-world.helper_wall_targets
                    supplier_tour=self.tour(topology,actor,remaining,ring,deadline,world.occupied) if remaining else None
                    for name in ('steps','entry','tail','endpoint','end'):
                        job['construction_'+name]=supplier_tour[name] if supplier_tour else None
                    if job['target'] in world.helper_wall_targets:
                        targets = remaining-{self.gate} or remaining
                        if targets:
                            job['target'] = min(targets,key=lambda p:(distance(actor.pos,p),p))
                            job['gate'] = remaining == {self.gate}
                        else:
                            result.pop(self.supplier,None)
                    if helper.get('helper_collect'):
                        world.batch_mine_owners = {helper['helper_mine']:other.id}
                    world.wall_assistance = dict(active=True,worker=other.id,target=helper['target'],
                        supplier=self.supplier,deficit=deficit,late=late,critical=rescue,
                        steps=helper['construction_steps'])
                elif not getattr(world,'ordered_ingress_due',False):
                    self.clear_helper_corridor(world,clock,policy,actor,other,missing,ring,job,deadline)
            return result
        share = min(other.inventory['stone'], max(0,len(missing)-1))
        if share and not defence_duties.enabled(world):
            options = []
            for target in sorted(missing,key=lambda p:(distance(other.pos,p),p)):
                if target == job['target'] or target == self.gate or target in world.occupied:
                    continue
                preview = Candidate(other.id,{'action':'build','name':'wall','targetPos':[pos_json(target)]},0,'use held wall material')
                if guard.check([preview])[0]:
                    options.append(target);break
            if options:
                # Keep the conservative full-project bound, but derive it
                # from this carrier's position and duty. Copying the supplier's
                # entry/tail can route M back to W's interior destination.
                other_tour = self.tour(topology, other, missing, ring, deadline, world.occupied)
                result[other.id] = dict(job, target=options[0], stock_target=share, gate=False,
                    construction_steps=other_tour['steps'] if other_tour else None,
                    construction_entry=other_tour['entry'] if other_tour else None,
                    construction_tail=other_tour['tail'] if other_tour else None,
                    construction_endpoint=other_tour['endpoint'] if other_tour else None,
                    construction_end=other_tour['end'] if other_tour else None,
                    defer_build=bool(other_tour and job['defer_build']
                                     and clock.until_night > other_tour['steps'] + 3))
                job['stock_target'] -= share
        return result

    def clear_helper_corridor(self, world, clock, policy, worker, helper, missing, ring, job, deadline):
        """Move the blocking worker first; reserve no build on imagined occupancy."""
        self.helper_clearance.clear()
        if (not policy.pioneer_rotation_enabled or not helper.inventory['stone']
                or any(r.alive for r in world.robots.values())):
            return
        blocked = ring | world.build_interior | world.stations[0].cells
        if helper.pos in blocked:
            return
        best = None
        # Only an adjacent, currently empty step can start this transaction.
        for point in sorted(neighbours(worker.pos)):
            if (not world.inside(point) or point in world.occupied
                    or point in world.navigation_avoided.get(worker.pos,set())):
                continue
            if time.monotonic() >= deadline:
                return
            preview = copy(world)
            preview.occupied = (world.occupied-{worker.pos})|{point}
            trial = deepcopy(self)
            planned = trial.assistance(preview,clock,policy,helper,missing,ring,job,
                                       LayoutGuard(preview,deadline),deadline)
            if not planned or planned.get('helper_collect'):
                continue
            home = distance_field(preview,defence_duties.stands(world,worker.id),point,deadline)
            required = planned['construction_steps']+home.get(point,float('inf'))+policy.return_buffer+1
            if required >= clock.until_night or time.monotonic() >= deadline:
                continue
            rank = (-len(planned['helper_targets']),required,point)
            if best is None or rank < best[0]:best = (rank,point,planned)
        if best is not None:
            _,point,planned = best
            self.helper_clearance = dict(worker=worker.id,position=point,last_round=world.round)
            world.helper_clearance_commands = {
                worker.id:[dict(action='move',targetPos=[pos_json(point)])], helper.id:[]}
            world.wall_assistance = dict(active=False,reason='await observed worker corridor clearance',
                worker=helper.id,supplier=worker.id,clearance=list(point),steps=planned['construction_steps'])
            return

    def assistance(self, world, clock, policy, actor, missing, ring, job, guard, deadline):
        """Finish the observed ore batch, then spend personal stone in one tour.

        Ore remainder is unknown. Only disappearance, capacity, danger or the
        actual wall-work deadline can interrupt an already started batch.
        """
        blocked = ring | world.build_interior | world.stations[0].cells
        start = distance_field(world, {actor.pos}, actor.pos, deadline, extra_blocked=blocked)
        options = []
        batch = self.helper_batches.get(actor.id)
        if batch and batch.get('last_round') not in (world.round-1,world.round):
            self.helper_batches.pop(actor.id,None)
            batch = None
        if batch and batch.get('mine') not in world.zones.get('stone', ()):
            batch['phase'] = 'build'
        if batch and batch['phase'] == 'build' and not actor.inventory['stone']:
            self.helper_batches.pop(actor.id, None)
            batch = None
        supplier = world.ours.get(job.get('supplier'))
        # A target without personally held material is not an exclusive build
        # commitment. Keep W's last stone for sealing an enclosing perimeter.
        gate_stone = int(self.gate in missing)
        exclusive = {job['target']} if supplier and supplier.inventory['stone'] > gate_stone else set()
        if batch:exclusive-=set(batch.get('targets',()))
        for target in sorted(missing-{self.gate}-exclusive):
            if time.monotonic() >= deadline:
                break
            if target in world.occupied:
                continue
            preview = Candidate(actor.id,dict(action='build',name='wall',targetPos=[pos_json(target)]),0,'exterior assistance')
            if not guard.check([preview])[0]:
                continue
            goals = interaction_cells(world,[target],actor.pos)-blocked
            work = distance_field(world,goals,actor.pos,deadline,extra_blocked=blocked)
            if actor.inventory['stone'] and (not batch or batch['phase'] == 'build'):
                required = work.get(actor.pos, float('inf'))+1
                mine = None
            else:
                mines = [batch['mine']] if batch and batch['phase'] == 'harvest' else world.zones.get('stone',())
                mining = [(start[p]+1+work[p]+1,m) for m in mines
                          for p in interaction_cells(world,[m],actor.pos) if p in start and p in work]
                if not mining:
                    continue
                required,mine = min(mining)
            if required+2 <= clock.until_night:
                facing = target in getattr(world,'monster_front_walls',())
                options.append((not facing,required,target,mine,goals))
        if not options:
            return None
        _,required,target,mine,goals = min(options,key=lambda x:x[:3])
        if batch is None:
            batch = self.helper_batches[actor.id] = dict(mine=mine, phase='harvest' if mine is not None else 'build')
        # Budget the complete funded batch from the next mining position.
        # All future walls stay blocked; each build and every exterior walk
        # is counted. A mine's unknown remainder never becomes owned stock.
        view = copy(world)
        view.occupied = world.occupied | blocked
        candidate_targets = {o[2] for o in options}
        count = max(1, actor.inventory['stone'])
        point = actor.pos
        mining_steps = 0
        if batch['phase'] == 'harvest' and mine is not None:
            stands = interaction_cells(world, [mine], actor.pos) & start.keys()
            if not stands:
                return None
            point = min(stands, key=lambda p: (start[p], p))
            mining_steps = start[point] + 1
        total = mining_steps
        timed_targets = []
        # Reserve one additional stone's work before admitting that collection.
        for _ in range(min(len(candidate_targets), count + (batch['phase'] == 'harvest'))):
            reach = distance_field(view, {point}, point, deadline)
            choices = [(reach[p], q, p) for q in candidate_targets
                       for p in interaction_cells(world, [q], point)-blocked if p in reach]
            if not choices or time.monotonic() >= deadline:
                break
            steps, q, point = min(choices)
            total += steps + 1
            timed_targets.append((q, total))
            candidate_targets.remove(q)
        required = total if timed_targets else float('inf')
        danger = any(r.alive and r.attack_range is not None and distance(actor.pos,r.pos)<=r.attack_range
                     for r in world.robots.values())
        stop = ('danger' if danger else 'capacity' if actor.capacity is not None and len(actor.backpack)>=actor.capacity
                else 'construction deadline' if required+3>=clock.until_night else None)
        if stop and actor.inventory['stone']:
            batch['phase'] = 'build'
        collect = batch['phase'] == 'harvest' and mine is not None and not danger
        targets = [q for q, _ in timed_targets[:count]]
        batch['targets']=list(targets)
        if not targets:
            return None
        target = targets[0]
        goals = interaction_cells(world, [target], actor.pos)-blocked
        batch['last_round'] = world.round
        batch['reason'] = stop or ('current mine still present' if collect else 'mine batch complete')
        return dict(job,target=target,stock_target=len(targets),gate=False,economy_first=False,
                    helper=True,helper_mine=mine,helper_collect=collect,helper_targets=targets,
                    helper_batch_reason=batch['reason'],helper_goals=sorted(goals),
                    construction_steps=required,construction_endpoint='exterior',
                    defer_build=collect)

    def tour(self, world, actor, missing, ring, deadline, occupied=()):
        """Budget all remaining build actions, outside walk, entry and closure.

        Future wall cells are already blocked for routing. This prevents an
        optimistic shortcut through a wall that this very tour will construct.
        A failed/time-limited route never creates a positive mining budget.
        """
        topology = copy(world)
        topology.occupied = world.occupied | (missing-{self.gate})
        remaining = missing-{self.gate}
        point = actor.pos
        total = 0
        first = None
        while remaining:
            field = distance_field(topology, [point], point, deadline)
            options = []
            outside_required = {q for q in remaining if not any(
                p in world.build_interior and p not in topology.occupied for p in neighbours(q))}
            for target in remaining:
                # The builder may currently stand on a missing wall cell.
                # Its tour first walks to a work stand before issuing build;
                # treating its own position as an immovable blocker deadlocks
                # the next-stone budget while the worker has no stone yet.
                if first is None and target in occupied and target != actor.pos:continue
                for p in neighbours(target):
                    if p not in field or p in ring:continue
                    if outside_required:
                        if field[p] and target not in outside_required:continue
                    elif p not in world.build_interior:continue
                    covered=sum(distance(p,q)<=1 for q in remaining)
                    options.append((field[p],-covered,target,p))
            if not options or time.monotonic() >= deadline:
                return None
            length, _, target, stand = min(options)
            if first is None:
                first = (target, stand, length)
            total += length+1
            remaining.remove(target)
            point = stand
        field = distance_field(topology, [point], point, deadline)
        # M finishes outside. This budget never authorizes sealing: the gate
        # state machine still checks personal stone and both actual guards.
        exterior = actor.id not in defender_ids(world)
        plan = getattr(world, 'task_side_plan', None)
        if exterior:
            goals = economic_endpoints(topology, actor)
            if self.gate is not None:
                goals &= set(neighbours(self.gate))
            positioning = 0
        elif plan:
            roster = world.night_roster
            goals = defence_duties.stands(world, actor.id)
            positioning = 0
        else:
            goals = set(world.build_interior)
            positioning = 2  # Legacy geometry has no fixed final gun stand.
        entries = [p for p in goals if p in field]
        if not entries or time.monotonic() >= deadline:
            return None
        entry = min(entries,key=lambda p:(field[p],p))
        total += field[entry] + positioning
        if (exterior or defence_duties.enabled(world)) and self.gate in missing:
            total += 1  # M's separate exterior build action, not a third guard.
        if first is None:
            first = (self.gate, entry, field[entry])
        return {'target':first[0], 'entry':first[1], 'steps':total, 'tail':total-first[2],
                'endpoint':'exterior' if exterior else 'defence', 'end':entry}
