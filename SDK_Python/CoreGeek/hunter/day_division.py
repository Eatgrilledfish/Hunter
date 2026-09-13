"""One stone supplier and a cash worker; construction has a real daytime budget.

The tour is a conservative static route, not a prediction of mobile collisions.
Actual builds still pass LayoutGuard, and actual moves use current occupancy.
"""
from collections import Counter
from copy import copy
from dataclasses import dataclass
import time
from .navigation import distance_field, neighbours
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

    def assign(self, world, clock, rules, policy, jobs, deadline):
        world.economy_first = False
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
        fixed_gate = planned_gate(world)
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
