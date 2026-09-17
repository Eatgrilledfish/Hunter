"""Continuous guard economy after an observed own wave clears.

Use existing passages and revalidate threats and personal stock every frame.
A new/unknown target recalls the worker. A closed ring may open its ordinary
level-one door after proving a useful route and personal rebuilding material.
"""
from dataclasses import dataclass,field
import time
from .arbitration import Candidate
from .navigation import distance_field,interaction_cells,neighbours
from .protocol import MINERALS,pos_json
from .robot_threats import active
from . import defence_duties


@dataclass
class NightClear:
    seen_day: int | None = None
    actor: str | None = None
    returning: bool = False
    offered: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)

    @staticmethod
    def material_reserve(world,clock,rules):
        """Carry the staged next-day construction material through night sales."""
        rule=rules.build_rule(world,'wall')
        if not rule or clock.day is None or clock.day>=10:return 0
        cost=rule.items.get('stone',0)
        from . import rear_open
        if rear_open.enabled(world):
            built={u.pos for u in world.ours.values() if u.alive and u.kind=='wall'}
            return len(rear_open.required(world)-built)*cost
        count=1
        if getattr(world,'wall_stage',None)=='front10':
            # The first-stage half ring expands tomorrow. Its missing walls
            # are known construction goals, not disposable mineral revenue.
            from .rules import station_rings
            plan=getattr(world,'task_side_plan',None)
            if plan:
                ring=station_rings(plan['anchor'])[1]
                built={u.pos for u in world.ours.values() if u.alive and u.kind=='wall'}
                count=max(count,len(ring-built))
        return count*cost

    def prepare(self,world,clock,rules,policy,guidance,deadline):
        self.offered={};self.diagnostic={'stage':'inactive'}
        if self.pending:
            buyer=world.ours.get(self.pending['actor'])
            feedback=world.raw.get('lastRoundRoleActionResults',{})
            result=feedback.get(self.pending['actor']) if isinstance(feedback,dict) and world.round==self.pending['round']+1 else None
            if (buyer and buyer.backpack is not None and buyer.inventory[self.pending['name']]>self.pending['before']
                    or result is False or isinstance(result,dict) and result.get('success') is False):
                self.pending={}
        if clock.phases!={'night'} or not defence_duties.enabled(world) or not policy.night_foraging_enabled:
            self.actor=None;self.returning=False
            return []
        robots=list(world.robots.values())
        if any(r.alive and r.target_team==world.side for r in robots):self.seen_day=clock.day
        explicit=isinstance(world.raw.get('robot'),dict) and isinstance(world.raw['robot'].get('roles'),list)
        clear=explicit and not any(r.alive and r.target_team in (None,world.side) for r in robots)
        world.own_wave_cleared=clear and self.seen_day==clock.day
        worker=world.ours.get(self.actor or world.night_roster.w)
        if not worker or not worker.alive or worker.backpack is None or worker.capacity is None:
            self.actor=None
            return []
        if worker.id!=world.night_roster.w:
            self.actor=None
            return []
        if not self.actor and (not clear or self.seen_day!=clock.day):return []
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
        # Dawn is a handoff to the ordinary daytime schedule, not another
        # wave. Keep the real night Clock for action legality. Final night has
        # no following workday and retains its actual match deadline.
        horizon=remaining+(70 if clear and clock.day is not None and clock.day<10 else 0)
        if self.actor and (back is None or back+policy.return_buffer+2>=horizon):self.returning=True
        if self.returning and back==0:
            self.actor=None;self.returning=False
            self.diagnostic={'stage':'returned','actor':worker.id}
            return []
        wall_rule=rules.build_rule(world,'wall')
        material_reserve=self.material_reserve(world,clock,rules)
        need_stone=worker.inventory['stone']<material_reserve
        command=None;stage='return' if self.returning else 'gather'
        if self.returning:
            if back:
                steps=sorted(p for p in neighbours(worker.pos) if home.get(p,float('inf'))<back)
                if steps:command=dict(action='move',targetPos=[pos_json(steps[0])])
        elif back is not None and time.monotonic()<deadline:
            reach=distance_field(world,{worker.pos},worker.pos,deadline,blocked)
            if not self.pending:
                command=self._investment(world,clock,rules,policy,worker,reach,home,blocked,horizon,deadline)
                if command:stage='invest'
            sale={name:worker.inventory[name] for name in MINERALS
                  if world.vendor.get(name,0)>0 and worker.inventory[name]}
            if 'stone' in sale:
                keep=material_reserve
                sale['stone']=max(0,sale['stone']-keep)
                if not sale['stone']:sale.pop('stone')
            vendors=interaction_cells(world,world.zones.get('vendor',()),worker.pos,blocked)
            if not command and sale and not self.pending:
                command=self._funding_sale(world,rules,policy,worker,sale,reach,home,blocked,horizon,deadline)
                if command:stage='fund_investment'
            if not command and sale and (sum(sale.values())>=policy.sell_batch or len(worker.backpack)>=worker.capacity
                         or worker.pos in vendors):
                choices=sorted((reach[p],p) for p in vendors & reach.keys() & home.keys()
                               if reach[p]+len(sale)+home[p]+policy.return_buffer+2<=horizon)
                if choices:
                    walk,stand=choices[0]
                    stage='sell'
                    if not walk:
                        name=max(sale,key=lambda n:(sale[n]*world.vendor[n],n))
                        command=dict(action='sell',name=name,num=sale[name])
                    else:
                        route=distance_field(world,{stand},worker.pos,deadline,blocked)
                        steps=sorted(p for p in neighbours(worker.pos) if route.get(p,float('inf'))<route.get(worker.pos,0))
                        if steps:command=dict(action='move',targetPos=[pos_json(steps[0])])
            options=[]
            for name in (() if command or len(worker.backpack)>=worker.capacity else sorted(MINERALS)):
                # The guard must retain its own next-day wall material. An
                # optional ore batch cannot assume the other worker's stone.
                if need_stone and name!='stone':continue
                if world.vendor.get(name,0)<=0 and not (need_stone and name=='stone'):continue
                for mine in world.zones.get(name,()):
                    if getattr(world,'batch_mine_owners',{}).get(mine,worker.id)!=worker.id:continue
                    for stand in interaction_cells(world,[mine],worker.pos,blocked):
                        walk=reach.get(stand);ret=home.get(stand)
                        # An in-progress trip owes only its next collection,
                        # never a fresh full batch on every frame.
                        if walk is None or ret is None or walk+1+ret+policy.return_buffer+2>horizon:continue
                        options.append((-world.vendor.get(name,0)/(walk+1),walk,mine,stand,name))
            if options:
                _,walk,mine,stand,name=min(options)
                if not walk:command=dict(action='collect',targetPos=[pos_json(mine)])
                else:
                    route=distance_field(world,{stand},worker.pos,deadline,blocked)
                    steps=sorted(p for p in neighbours(worker.pos) if route.get(p,float('inf'))<route.get(worker.pos,0))
                    if steps:command=dict(action='move',targetPos=[pos_json(steps[0])])
            elif not command:
                command=self._passage(world,clock,rules,policy,worker,reach,blocked,horizon,deadline)
                if command:
                    stage='open_passage'
                    if command['action']=='remove':
                        world.ordered_gate_actions.setdefault(worker.id,[]).append(command)
                elif self.actor:
                    self.returning=True
                    if back:
                        steps=sorted(p for p in neighbours(worker.pos) if home.get(p,float('inf'))<back)
                        if steps:command=dict(action='move',targetPos=[pos_json(steps[0])]);stage='return'
        self.diagnostic=dict(stage=stage if command else
                             'budget_exhausted' if time.monotonic()>=deadline or not getattr(home,'complete',True)
                             else 'no_safe_circuit',actor=worker.id,
                             own_clear=clear,return_steps=back,night_remaining=remaining,
                             economic_remaining=horizon,own_wall_material_needed=need_stone)
        if not command:return []
        self.offered=dict(actor=worker.id,command=command)
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

    @staticmethod
    def _investment(world,clock,rules,policy,worker,reach,home,blocked,horizon,deadline):
        """One funded purchase/application with its whole carried tour and return."""
        from copy import copy
        from . import supply_basket
        if not policy.upgrade_commitment_enabled or world.gold is None:return None
        view=copy(world)
        view.occupied=world.occupied|blocked
        view.upgrade_checkout_actor=worker.id
        data=supply_basket.basket(view,worker,rules,policy,deadline)
        if data is None or time.monotonic()>=deadline:return None
        def move(goals):
            route=distance_field(view,goals,worker.pos,deadline)
            steps=sorted(p for p in neighbours(worker.pos) if route.get(p,float('inf'))<route.get(worker.pos,0))
            return dict(action='move',targetPos=[pos_json(steps[0])]) if steps else None
        held=[r for r in data['held'] if not r.get('pending') and r['unit'] is not None]
        tour=supply_basket.use_tour(view,worker,held,worker.pos,home,deadline)
        restock_here=(world.near_zone(worker.pos,'weaponShop')
                      and any(e['name']=='WallFixer' for e in data['planned']))
        if held and not restock_here and tour is not None and tour+policy.return_buffer<=horizon:
            from .wall_service import service_key
            ready=sorted((r for r in held if r['level']==r['unit'].level),
                         key=lambda r:(r['rank'],service_key(world,r['unit'])))
            for r in ready:
                from .wall_service import use_permitted
                quoted_use=Candidate(worker.id,dict(action='use',name=r['name'],
                    targetPos=[pos_json(r['unit'].pos)]),0,'quoted held delivery')
                if not use_permitted(world,quoted_use):continue
                goals=interaction_cells(view,[r['unit'].pos],worker.pos)&reach.keys()
                if not goals:continue
                if worker.pos in goals:
                    return dict(action='use',name=r['name'],targetPos=[pos_json(r['unit'].pos)])
                return move(goals)
        wall=rules.build_rule(world,'wall')
        material_space=max(0,NightClear.material_reserve(world,clock,rules)-worker.inventory['stone'])
        if len(worker.backpack)+material_space>=worker.capacity:return None
        shops=interaction_cells(view,world.zones.get('weaponShop',()),worker.pos)&reach.keys()&home.keys()
        # Every purchase is independently affordable; optional additions cannot
        # hold a minimum useful order hostage to a larger shopping basket.
        for entry in data['planned']:
            if entry.get('pending'):continue
            if entry['unit'] is not None and entry['level']!=entry['unit'].level:continue
            name=entry['name']
            if not 0<world.shop.get(name,0)<=world.gold-data['reserve']:continue
            options=[]
            for shop in shops:
                use=supply_basket.use_tour(view,worker,held+[entry],shop,home,deadline)
                if use is not None and reach[shop]+1+use+policy.return_buffer<=horizon:
                    options.append((reach[shop]+use,shop))
            if time.monotonic()>=deadline:return None
            if options:
                _,shop=min(options)
                if worker.pos==shop and name in data.get('guard_authorizations',{}):
                    world.essential_guard_stock=dict(getattr(world,'essential_guard_stock',{}))
                    world.essential_guard_stock[worker.id,name]=dict(data['guard_authorizations'][name])
                if worker.pos==shop and name=='WallFixer':
                    # Only the selected, route-validated purchase publishes its
                    # quote to the real arbitration world; copy(view) is private.
                    proof=data.get('repair_authorization',{})
                    if proof.get('round')==world.round and proof.get('count',0)>0:
                        world.essential_repair_stock=dict(getattr(world,'essential_repair_stock',{}))
                        world.essential_repair_stock[worker.id]=dict(proof,price=world.shop[name],
                            actor=worker.id,purpose='night_clear_repair',count=1)
                return dict(action='buy',name=name,num=1) if worker.pos==shop else move({shop})
        return None

    @staticmethod
    def _funding_sale(world,rules,policy,worker,stock,reach,home,blocked,horizon,deadline):
        """Quote sale -> minimum necessary purchase -> use -> duty before selling.

        Projected proceeds prove the trip only; purchase still requires observed cash.
        """
        from copy import copy
        from . import supply_basket
        if world.gold is None or time.monotonic()>=deadline:return None
        value=sum(world.vendor[name]*count for name,count in stock.items())
        view=copy(world);view.occupied=world.occupied|blocked;view.upgrade_checkout_actor=worker.id
        data=supply_basket.basket(view,worker,rules,policy,deadline,cash=world.gold+value)
        if data is None or time.monotonic()>=deadline:return None
        needs=[r for r in data['planned'] if not r.get('pending')
               and (r['unit'] is not None and r['level']==r['unit'].level
                    or r['name'] in {'WallFixer','Medicine'})
               and world.gold<data['reserve']+world.shop.get(r['name'],0)<=world.gold+value]
        if not needs:return None
        vendors=interaction_cells(view,world.zones.get('vendor',()),worker.pos)&reach.keys()
        shops=interaction_cells(view,world.zones.get('weaponShop',()),worker.pos)&reach.keys()&home.keys()
        held=[r for r in data['held'] if not r.get('pending') and r['unit'] is not None]
        choices=[]
        fields={}
        before_blocked=(view.occupied|view.navigation_avoided.get(worker.pos,set()))-{worker.pos}
        for shop in sorted(shops):
            onward=distance_field(view,{shop},worker.pos,deadline)
            if time.monotonic()>=deadline:return None
            # The sale route and post-purchase tour often search the same
            # graph from this shop. Reuse only a complete field with exactly
            # the same obstacles, including actor-specific avoidance.
            after_blocked=((view.occupied-{worker.pos})|view.navigation_avoided.get(shop,set()))-{shop}
            if before_blocked==after_blocked:fields[shop]=onward
            for entry in needs:
                use=supply_basket.use_tour(view,worker,held+[entry],shop,home,deadline,fields)
                if use is None:continue
                for vendor in vendors&onward.keys():
                    required=reach[vendor]+len(stock)+onward[vendor]+1+use+policy.return_buffer
                    if required<=horizon:choices.append((required,reach[vendor],vendor))
                if time.monotonic()>=deadline:return None
        if not choices:return None
        _,_,vendor=min(choices)
        if worker.pos==vendor:
            name=max(stock,key=lambda n:(stock[n]*world.vendor[n],n))
            return dict(action='sell',name=name,num=stock[name])
        route=distance_field(view,{vendor},worker.pos,deadline)
        steps=sorted(p for p in neighbours(worker.pos) if route.get(p,float('inf'))<route.get(worker.pos,0))
        if steps and time.monotonic()<deadline:return dict(action='move',targetPos=[pos_json(steps[0])])
        return None

    @staticmethod
    def _passage(world,clock,rules,policy,worker,reach,blocked,horizon,deadline):
        """Preview one ordinary door; only its real removal is authorized."""
        from copy import copy
        from .rules import station_rings
        from .wall_policy import monster_face
        plan=world.task_side_plan
        _,yellow=station_rings(plan['anchor'])
        walls={u.pos:u for u in world.ours.values() if u.alive and u.kind=='wall'}
        gate=getattr(world,'clear_exit_plan',None) or plan['gate'];door=walls.get(gate)
        build=rules.build_rule(world,'wall')
        if (not yellow<=walls.keys() or not door or door.level!=1
                or gate in monster_face(world,plan['anchor'])
                or build is None or world.gold is None or world.gold<build.gold
                or any(worker.inventory[k]<n for k,n in build.items.items())):
            return None
        inside=interaction_cells(world,[gate],worker.pos,blocked)&reach.keys()
        if not inside:return None
        preview=copy(world);preview.occupied=world.occupied-{gate}
        opened=distance_field(preview,{worker.pos},worker.pos,deadline,blocked)
        home=distance_field(preview,defence_duties.stands(world,worker.id),worker.pos,deadline,blocked)
        goals=set()
        if len(worker.backpack)<worker.capacity:
            for name in MINERALS:
                if world.vendor.get(name,0)<=0:continue
                mines=[p for p in world.zones.get(name,())
                       if getattr(world,'batch_mine_owners',{}).get(p,worker.id)==worker.id]
                goals.update(interaction_cells(preview,mines,worker.pos,blocked))
        if any(worker.inventory[k]>(build.items.get(k,0)) and world.vendor.get(k,0)>0 for k in MINERALS):
            goals.update(interaction_cells(preview,world.zones.get('vendor',()),worker.pos,blocked))
        useful=[p for p in goals&opened.keys()&home.keys()
                if p not in reach and opened[p]+home[p]+3+policy.return_buffer<=horizon]
        if not useful or time.monotonic()>=deadline:return None
        if worker.pos in inside:
            return dict(action='remove',targetPos=[pos_json(gate)])
        route=distance_field(world,inside,worker.pos,deadline,blocked)
        steps=sorted(p for p in neighbours(worker.pos) if route.get(p,float('inf'))<route.get(worker.pos,0))
        if steps and time.monotonic()<deadline:return dict(action='move',targetPos=[pos_json(steps[0])])
        return None

    def finalize(self,world,response):
        if self.offered and response['roleCommandMap'].get(self.offered['actor'])==self.offered['command']:
            self.actor=self.offered['actor']
            command=self.offered['command']
            if command['action']=='buy':
                self.pending=dict(actor=self.actor,name=command['name'],before=world.ours[self.actor].inventory[command['name']],round=world.round)
