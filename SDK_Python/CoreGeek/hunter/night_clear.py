"""Temporary guard gathering after an observed own wave clears.

Use only existing passages, a bounded personal batch and a current safe return.
A new/unknown target recalls the worker; no wall is demolished for this trip.
"""
from dataclasses import dataclass,field
import time
from .arbitration import Candidate
from .navigation import distance_field,interaction_cells,neighbours
from .protocol import MINERALS,distance,pos_json
from .robot_threats import active
from . import defence_duties


@dataclass
class NightClear:
    seen_day: int | None = None
    actor: str | None = None
    collected_before: int = 0
    returning: bool = False
    offered: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)

    def prepare(self,world,clock,rules,policy,guidance,deadline):
        self.offered={};self.diagnostic={'stage':'inactive'}
        if clock.phases!={'night'} or not defence_duties.enabled(world) or not policy.night_foraging_enabled:
            self.actor=None;self.returning=False
            return []
        robots=list(world.robots.values())
        if any(r.alive and r.target_team==world.side for r in robots):self.seen_day=clock.day
        explicit=isinstance(world.raw.get('robot'),dict) and isinstance(world.raw['robot'].get('roles'),list)
        clear=explicit and not any(r.alive and r.target_team in (None,world.side) for r in robots)
        worker=world.ours.get(self.actor or world.night_roster.w)
        if not worker or not worker.alive or worker.backpack is None or worker.capacity is None:
            self.actor=None
            return []
        if worker.id!=world.night_roster.w:
            self.actor=None
            return []
        if not self.actor and (not clear or self.seen_day!=clock.day or len(worker.backpack)>=worker.capacity):return []
        if (worker.id in guidance.survival_actions or worker.id in getattr(world,'return_recovery_actions',{})
                or worker.id in getattr(world,'repair_commands',{}) and world.repair_commands[worker.id]
                or worker.health<=110 or getattr(world,'critical_base_ids',())
                or guidance.observations.get(worker.id,{}).get('upper_per_attack_opportunity',0)>0):
            if self.actor:self.returning=True
            self.diagnostic={'stage':'urgent_duty','actor':worker.id}
            return []
        if not clear:self.returning=True
        threats=active(world)
        if any(r.attack_range is None or r.attack_power is None for r in threats):
            self.returning=bool(self.actor)
            self.diagnostic={'stage':'unknown_route_threat','actor':worker.id}
            return []
        blocked=set()
        for r in threats:
            if r.attack_power<=0:continue
            radius=r.attack_range+1
            blocked.update((x,y) for x in range(max(0,r.pos[0]-radius),min(world.width,r.pos[0]+radius+1))
                           for y in range(max(0,r.pos[1]-radius),min(world.height,r.pos[1]+radius+1)))
        home=distance_field(world,defence_duties.stands(world,worker.id),worker.pos,deadline,blocked)
        back=home.get(worker.pos)
        remaining=min(130-(clock.round-o)%130 for o in clock.offsets)
        stock=sum(worker.inventory[k] for k in MINERALS)
        batch=max(1,min(policy.sell_batch,worker.capacity-len(worker.backpack)))
        if self.actor and (stock-self.collected_before>=policy.sell_batch or len(worker.backpack)>=worker.capacity
                           or back is None or back+policy.return_buffer+2>=remaining):self.returning=True
        if self.returning and back==0:
            self.actor=None;self.returning=False
            self.diagnostic={'stage':'returned','actor':worker.id}
            return []
        wall_rule=rules.build_rule(world,'wall')
        need_stone=bool(clock.day is not None and clock.day<10 and wall_rule
                        and worker.inventory['stone']<wall_rule.items.get('stone',0))
        command=None;stage='return' if self.returning else 'gather'
        if self.returning:
            if back:
                steps=sorted(p for p in neighbours(worker.pos) if home.get(p,float('inf'))<back)
                if steps:command=dict(action='move',targetPos=[pos_json(steps[0])])
        elif back is not None and time.monotonic()<deadline:
            reach=distance_field(world,{worker.pos},worker.pos,deadline,blocked)
            options=[]
            for name in sorted(MINERALS):
                # The guard must retain its own next-day wall material. An
                # optional ore batch cannot assume the other worker's stone.
                if need_stone and name!='stone':continue
                if world.vendor.get(name,0)<=0 and not (need_stone and name=='stone'):continue
                for mine in world.zones.get(name,()):
                    if getattr(world,'batch_mine_owners',{}).get(mine,worker.id)!=worker.id:continue
                    for stand in interaction_cells(world,[mine],worker.pos,blocked):
                        walk=reach.get(stand);ret=home.get(stand)
                        if walk is None or ret is None or walk+batch+ret+policy.return_buffer+2>remaining:continue
                        options.append((-world.vendor.get(name,0)/(walk+1),walk,mine,stand,name))
            if options:
                _,walk,mine,stand,name=min(options)
                if not walk:command=dict(action='collect',targetPos=[pos_json(mine)])
                else:
                    route=distance_field(world,{stand},worker.pos,deadline,blocked)
                    steps=sorted(p for p in neighbours(worker.pos) if route.get(p,float('inf'))<route.get(worker.pos,0))
                    if steps:command=dict(action='move',targetPos=[pos_json(steps[0])])
            elif self.actor:
                self.returning=True
                if back:
                    steps=sorted(p for p in neighbours(worker.pos) if home.get(p,float('inf'))<back)
                    if steps:command=dict(action='move',targetPos=[pos_json(steps[0])]);stage='return'
        self.diagnostic=dict(stage=stage if command else 'no_safe_circuit',actor=worker.id,
                             own_clear=clear,return_steps=back,night_remaining=remaining,
                             own_wall_material_needed=need_stone)
        if not command:return []
        self.offered=dict(actor=worker.id,command=command,stock=stock)
        candidate=Candidate(worker.id,command,1100,'cleared own wave: '+stage+' using current passage and safe return')
        previous=guidance.duty_permit
        def permit(c):
            identity=c.command.get('controllerId') if c.command.get('action')=='attack' else c.actor
            if identity==worker.id:
                return c.command==command or (c.command.get('action')=='use' and c.command.get('name') in {'Medicine','Bomb','DizzyWeapon'})
            return previous(c) if previous else None
        guidance.duty_permit=permit
        world.night_forage_commands.setdefault(worker.id,[]).append(command)
        return [candidate]

    def finalize(self,world,response):
        if self.offered and response['roleCommandMap'].get(self.offered['actor'])==self.offered['command'] and not self.actor:
            self.actor=self.offered['actor'];self.collected_before=self.offered['stock']
