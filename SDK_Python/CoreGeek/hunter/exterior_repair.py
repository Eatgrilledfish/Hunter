"""Personal exterior service for walls with no usable interior repair tile."""
from dataclasses import dataclass, field
from copy import copy
import time
import math
from .arbitration import Candidate
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import distance, pos_json
from .robot_threats import active
from .rules import station_rings
from . import repair_decision
from .repair_plan import pressure
from .repair_supply import weapon_reserve


@dataclass
class ExteriorRepair:
    pending: dict = field(default_factory=dict)
    offered: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)
    commitment: dict = field(default_factory=dict)

    def prepare(self,world,clock,rules,policy,guidance,deadline):
        self.offered={};self.diagnostic={'stage':'inactive'}
        if self.commitment and world.round>=self.commitment['until']:
            self.commitment={}
        for identity,order in list(self.pending.items()):
            actor=world.ours.get(identity)
            arrived=actor and actor.backpack is not None and actor.inventory['WallFixer']>order['prior']
            feedback=world.raw.get('lastRoundRoleActionResults',{})
            failed=(world.round==order['round']+1 and isinstance(feedback,dict) and feedback.get(identity) is False)
            if arrived or failed:self.pending.pop(identity)
        plan=getattr(world,'task_side_plan',None)
        if (clock.phases!={'night'} or not plan or not policy.repair_plan_enabled
                or not policy.pioneer_rotation_enabled):return []
        actor=world.ours.get(world.night_roster.m)
        from .medical import needs_treatment
        if (not actor or not actor.alive or actor.kind!='worker' or actor.id in world.night_defenders
                or actor.backpack is None or actor.capacity is None or actor.health<=110
                or actor.id in guidance.survival_actions or actor.abnormal=='dizzy'):
            return []
        if policy.medical_supply_enabled and needs_treatment(world,actor,clock):
            self.diagnostic=dict(stage='TREATMENT_FIRST',actor=actor.id)
            return []
        blue,yellow=station_rings(plan['anchor'])
        interior=blue|yellow|world.stations[0].cells
        if actor.pos in interior:return []
        static=world.occupied-{u.pos for u in world.movers}
        walls=[u for u in world.ours.values() if u.alive and u.kind=='wall' and u.pos in yellow
               and not (set(neighbours(u.pos))&blue-static)]
        if not walls:return []
        self.diagnostic=dict(stage='NO_DEMAND',actor=actor.id,stock=actor.inventory['WallFixer'])
        threats=active(world)
        if any(r.attack_range is None or r.attack_power is None for r in threats):
            self.diagnostic['stage']='UNKNOWN_THREAT';return []
        blocked=set(interior)
        for r in threats:
            if r.attack_power<=0:continue
            if distance(actor.pos,r.pos)<=r.attack_range:
                self.diagnostic['stage']='ESCAPE_FIRST';return []
            blocked.update((x,y) for x in range(max(0,r.pos[0]-r.attack_range),min(world.width,r.pos[0]+r.attack_range+1))
                           for y in range(max(0,r.pos[1]-r.attack_range),min(world.height,r.pos[1]+r.attack_range+1)))
        view=copy(world);view.occupied=world.occupied|blocked
        reach=distance_field(view,{actor.pos},actor.pos,deadline)
        left=min(130-(clock.round-o)%130 for o in clock.offsets)
        jobs=[];windows={}
        for wall in walls:
            maximum=rules.health_limit(world,wall)
            loss=getattr(world,'observed_wall_losses',{}).get(wall.id,0)
            hit=pressure(world,wall) or 0
            threshold=maximum*policy.wall_repair_health_fraction if maximum else None
            continuing=(self.commitment.get('actor')==actor.id and self.commitment.get('target')==wall.id
                        and wall.health<=self.commitment['health'] and hit>0)
            if threshold is not None and loss>0 and hit>0:
                # This is a revisable deadline from observed loss, never an
                # assumed robot cooldown. If the predicted decline does not
                # occur, the wait expires rather than turning M into a guard.
                windows[wall.id]=world.round+max(1,math.ceil((wall.health-threshold)/loss))
            elif continuing:windows[wall.id]=self.commitment['until']
            stands=interaction_cells(view,[wall.pos],actor.pos)&reach.keys()
            if stands:
                nearest=min(reach[p] for p in stands)
                stands={p for p in stands if reach[p]==nearest}
            for stand in stands:
                approaching=(threshold is not None and loss>0 and hit>0
                             and wall.health<=threshold+loss*(reach[stand]+1))
                if (reach[stand]+1<=left and (approaching or continuing or
                        repair_decision.eligible(world,wall,rules,policy,service_steps=reach[stand]+1,pressure=hit))):
                    jobs.append((wall.health,reach[stand],wall.id,stand,wall))
        if time.monotonic()>=deadline:return []
        command=None;target=None;stage=None
        if actor.inventory['WallFixer'] and jobs:
            _,walk,_,stand,target=min(jobs,key=lambda j:j[:4])
            if not walk and repair_decision.eligible(world,target,rules,policy,pressure=pressure(world,target) or 0):
                command=dict(action='use',name='WallFixer',targetPos=[pos_json(target.pos)])
            elif not walk:stage='EXTERIOR_REPAIR_WAIT'
            else:command=self.move(view,actor,{stand},deadline)
            stage=stage or 'EXTERIOR_REPAIR'
        elif not actor.inventory['WallFixer'] and actor.id not in self.pending and len(actor.backpack)<actor.capacity:
            reserve=weapon_reserve(world,rules,deadline)
            price=world.shop.get('WallFixer')
            if reserve is not None and price is not None and price>0 and world.gold is not None and world.gold>=reserve+price:
                shops=interaction_cells(view,world.zones.get('weaponShop',()),actor.pos)&reach.keys()
                choices=[]
                for _,_,_,stand,wall in jobs:
                    service=distance_field(view,{stand},actor.pos,deadline)
                    choices.extend((reach[p]+service[p]+2,reach[p],p,wall.id,wall) for p in shops&service.keys()
                                   if reach[p]+service[p]+2<=left)
                # Stock one personal spare when already at a shop, before a
                # corner falls below the threshold. No speculative long trip.
                if not choices and actor.pos in shops and left>=2:
                    choices=[(1,0,actor.pos,'',None)]
                if choices and time.monotonic()<deadline:
                    _,walk,stand,_,target=min(choices,key=lambda j:j[:4])
                    command=dict(action='buy',name='WallFixer',num=1) if not walk else self.move(view,actor,{stand},deadline)
                    stage='EXTERIOR_REPAIR_BUY'
        if (not command and stage!='EXTERIOR_REPAIR_WAIT') or time.monotonic()>=deadline:
            demand=any(repair_decision.eligible(world,w,rules,policy) for w in walls)
            self.diagnostic['stage']=('PENDING_PURCHASE' if actor.id in self.pending else
                'NO_STOCK' if jobs and not actor.inventory['WallFixer'] else
                'NO_SAFE_ROUTE' if demand else 'NO_DEMAND')
            return []
        self.diagnostic=dict(stage=stage,actor=actor.id,target=target.id if target else None,stock=actor.inventory['WallFixer'])
        if command:
            world.repair_commands.setdefault(actor.id,[]).append(command)
            if not hasattr(world,'night_resupply_commands'):world.night_resupply_commands={}
            world.night_resupply_commands.setdefault(actor.id,[]).append(command)
        prior=guidance.duty_permit
        def permit(candidate):
            if candidate.actor==actor.id:
                return candidate.command==command or (candidate.command.get('action')=='use' and candidate.command.get('name')=='Medicine')
            return prior(candidate) if prior else None
        guidance.duty_permit=permit
        self.offered=dict(actor=actor.id,command=command,round=world.round,prior=actor.inventory['WallFixer'],
            target=target.id if target else None,health=target.health if target else None,
            until=windows.get(target.id,world.round+1) if target else world.round+1)
        self.diagnostic['review_by']=self.offered['until']
        return [Candidate(actor.id,command,1105,'personal exterior corner service')] if command else []

    @staticmethod
    def move(world,actor,goals,deadline):
        route=distance_field(world,goals,actor.pos,deadline)
        steps=sorted(p for p in neighbours(actor.pos) if route.get(p,float('inf'))<route.get(actor.pos,0))
        return dict(action='move',targetPos=[pos_json(steps[0])]) if steps else None

    def finalize(self,world,response):
        order=self.offered
        if order and response['roleCommandMap'].get(order['actor'])==order['command']:
            command=order['command'] or {}
            if command.get('action')=='buy':self.pending[order['actor']]=dict(order)
            self.commitment={} if command.get('action')=='use' else dict(order)
