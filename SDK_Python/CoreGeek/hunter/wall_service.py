"""Position-based reconstruction obligations, separate from observed units."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PendingWall:
    pos: tuple
    level: int = 1
    kind: str = 'wall'
    alive: bool = False
    health: None = None

    @property
    def id(self):
        return f'pending-wall:{self.pos[0]},{self.pos[1]}'


@dataclass
class WallService:
    sites: dict = field(default_factory=dict)

    def prepare(self, world):
        from .wall_policy import upgrade_targets
        walls = {u.pos:u for u in world.ours.values() if u.alive and u.kind=='wall'}
        self.sites = {p:self.sites.get(p,{}) for p in upgrade_targets(world)}
        for p, record in self.sites.items():
            wall = walls.get(p)
            identity = wall.id if wall else None
            previous = record.get('id')
            if previous and identity!=previous:
                record['reconstruction'] = True
            if identity is None and (getattr(world,'strategy_day',0) or 0)>1:
                record['reconstruction'] = True
            if identity and identity!=previous:
                record['generation'] = record.get('generation',0)+1
            record.update(id=identity,level=wall.level if wall else None,
                hp=wall.health if wall else None, builder=None, build_after=None,
                state='complete' if wall and wall.level==3 else 'upgrade' if wall else 'await_material')
            if wall and wall.level>=2:record.pop('reserved_owner',None)
        world.wall_service = self.sites

    def assign(self, world, clock, jobs):
        from .day_access import gate as access_gate
        gate = access_gate(world)
        roster = world.night_roster
        worker = world.ours.get(roster.w)
        # Gate supervision can take W's movement out of ordinary build_jobs.
        # Reprove that concrete seal, rather than treating ownership transfer
        # as either cancellation or an unconditional persistent reservation.
        jobs = dict(jobs)
        if (gate in self.sites and self.sites[gate]['id'] is None
                and roster.w not in jobs and worker and worker.alive
                and worker.backpack is not None and worker.inventory['stone']
                and getattr(world,'worker_close_requested',False)):
            import time
            from .navigation import distance_field, neighbours
            from .rules import station_rings
            blue, _ = station_rings(world.task_side_plan['anchor'])
            goals = (set(neighbours(gate)) & blue)-{world.task_side_plan['w']}
            route = distance_field(world,goals,worker.pos,time.monotonic()+.01)
            walk = route.get(worker.pos)
            if walk is not None:
                jobs[worker.id] = dict(name='wall',helper=True,helper_targets=[gate],
                                      construction_steps=walk+1,gate_handoff=True)
        helpers = set(getattr(world,'helper_wall_targets',()))
        for identity, job in jobs.items():
            actor = world.ours.get(identity)
            if not actor or actor.backpack is None or job.get('name')!='wall':continue
            targets = set(job.get('helper_targets',())) if job.get('helper') else (
                set(world.wall_targets or ())-{u.pos for u in world.ours.values() if u.alive and u.kind=='wall'}-helpers)
            steps = job.get('construction_steps')
            funded = actor.inventory['stone']>=len(targets)
            fits = steps is not None and steps+1<clock.until_night
            for p in targets & self.sites.keys():
                if self.sites[p]['id'] is not None:continue
                from .day_access import gate as access_gate
                # The active access gap is held for guard ingress. Do not
                # promise its construction at the worker's first arrival.
                closure_steps=(max(steps or 0,clock.until_night-18)
                    if p==gate and not getattr(world,'worker_close_requested',False) else steps)
                self.sites[p].update(builder=identity,
                    build_after=world.round+closure_steps+1 if funded and fits else None,
                    state='await_build' if funded and fits else 'await_material')
        available={u.id:u.inventory['WallUpgradeVoucher1'] for u in world.movers if u.backpack is not None}
        roster=world.night_roster
        for p,record in sorted(self.sites.items()):
            previous_owner=record.pop('reserved_owner',None)
            if not record.get('reconstruction') or record.get('level',1) in (2,3):continue
            if record['id'] is None and record['build_after'] is None:continue
            carriers=[i for i,n in available.items() if n and i in (roster.p,roster.w,roster.m)]
            if carriers:
                owner=min(carriers,key=lambda i:(i!=previous_owner,i!=roster.p,i))
                available[owner]-=1
                record['reserved_owner']=owner


def pending_targets(world, *, scheduled=False):
    from .wall_policy import upgrade_targets
    observed = {u.pos for u in world.ours.values() if u.alive and u.kind=='wall'}
    sites = getattr(world,'wall_service',{})
    return [PendingWall(p) for p in sorted(upgrade_targets(world)-observed)
            if not scheduled or sites.get(p,{}).get('build_after') is not None]


def service_key(world, unit):
    record = getattr(world,'wall_service',{}).get(unit.pos,{})
    return (not record.get('reconstruction',False),unit.id)


def handoff_margin(world):
    """Reserve service/receipt/return slack beyond the existing ingress budget."""
    if (getattr(world,'strategy_day',0) or 0)<=1:return 0
    from .wall_policy import upgrade_targets
    walls={u.pos for u in world.ours.values() if u.alive and u.kind=='wall'}
    return 6 if upgrade_targets(world)-walls else 0


def use_permitted(world, candidate):
    command=candidate.command
    if command.get('action')!='use' or command.get('name')!='WallUpgradeVoucher1':return True
    actor=world.ours.get(candidate.actor)
    points=command.get('targetPos',[])
    if not actor or actor.backpack is None or len(points)!=1:return True
    point=(points[0].get('x'),points[0].get('y'))
    reservations=[p for p,r in getattr(world,'wall_service',{}).items()
                  if r.get('reserved_owner')==actor.id and r.get('level') not in (2,3)]
    if point in reservations or actor.inventory['WallUpgradeVoucher1']>len(reservations):return True
    wall=next((u for u in world.ours.values() if u.alive and u.kind=='wall' and u.pos==point),None)
    loss=getattr(world,'observed_wall_losses',{}).get(wall.id,0) if wall else 0
    return bool(wall and loss>0 and wall.health is not None and wall.health<=loss*2)


def build_permitted(world, candidate):
    command=candidate.command
    if command.get('action')!='build' or command.get('name')!='wall':return True
    if getattr(world,'wall_stage',None)=='front10':return True
    roster=getattr(world,'night_roster',None)
    if not roster or candidate.actor!=roster.w:return True
    from .day_access import gate as access_gate
    gate=access_gate(world)
    if gate is None:return True
    if any(u.alive and u.kind=='wall' and u.pos==gate for u in world.ours.values()):return True
    points=command.get('targetPos',[])
    point=(points[0].get('x'),points[0].get('y')) if len(points)==1 else None
    actor=world.ours.get(candidate.actor)
    front_gap = (point in getattr(world,'monster_front_walls',())
                 and not any(u.alive and u.kind=='wall' and u.pos==point for u in world.ours.values()))
    # Saving the last stone for the door cannot seal a ring with a front
    # breach. Prefer closing that exposed gap; other side work still reserves
    # the personal gate stone.
    return bool(point==gate or actor and actor.backpack is not None and
                (actor.inventory['stone']>1 or actor.inventory['stone'] and front_gap))
