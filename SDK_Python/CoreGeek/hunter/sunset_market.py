"""Personal liquidation before a final, observed-cash night-stock checkout."""
from dataclasses import dataclass, field
import time

from .arbitration import Candidate
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import MINERALS, WEAPONS, distance, pos_json
from .day_schedule import day_endpoints
from .night_roles import defender_ids
from . import procurement


def moves(actor, route, reason):
    length = route.get(actor.pos)
    if not length:
        return []
    return [Candidate(actor.id, {'action':'move','targetPos':[pos_json(p)]}, 180-i*.01, reason)
            for i,p in enumerate(sorted(p for p in neighbours(actor.pos) if route.get(p,float('inf'))<length)[:4])]


@dataclass
class SunsetMarket:
    day: int | None = None
    started: bool = False
    settled: set = field(default_factory=set)
    pending: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)

    def prepare(self, world, clock, rules, policy, guidance, jobs, excluded, deadline):
        guidance.market_permit = lambda candidate: permits(world, candidate)
        world.pioneer_trade_stands = guidance.operator_stands
        world.sunset_actions = {}
        world.sunset_buyer = None
        self.diagnostic = {'stage':'inactive'}
        for identity, order in list(self.pending.items()):
            actor = world.ours.get(identity)
            feedback = world.raw.get('lastRoundRoleActionResults', {})
            failed = (world.round == order['round']+1 and isinstance(feedback,dict)
                      and feedback.get(identity) is False)
            if failed or (actor and actor.backpack is not None and actor.inventory[order['name']]>order['prior']):
                self.pending.pop(identity)
        if self.day != clock.day:
            self.day = clock.day; self.started = False; self.settled.clear()
        if (not policy.day_schedule_enabled or clock.phases != {'day'} or clock.day is None
                or not world.phase_task_observed or time.monotonic()>=deadline):
            return []
        from .defence_duties import enabled
        if enabled(world):
            self.diagnostic = {'stage':'worker_delivery_schedule', 'buyer':world.night_roster.w}
            return []  # DaySchedule/Procurement prove the worker's full delivery circuit.
        # Without a live pioneer, retain the workers' existing complete
        # sale/purchase/delivery schedule and emergency replacement duties.
        if not any(a.kind=='pioneer' for a in world.movers):
            return []
        # Thirty rounds is a policy window, not a game rule. Long sale/return
        # routes open it earlier; each circuit is checked against today's map.
        pioneers = [a for a in world.movers if a.kind=='pioneer' and a.id not in excluded
                    and a.backpack is not None and a.capacity is not None and len(a.backpack)<a.capacity
                    and world.phase_task_observed and not world.phase_task]
        buyer = pioneers[0] if len(pioneers)==1 else None
        margin = policy.return_buffer+(8 if world.defence_cells else 0)
        home_fields = {}
        def home(actor):
            if actor.id not in home_fields:
                goals = ({guidance.operator_stands[actor.id]} if actor.id in guidance.operator_stands else set())
                if actor.kind=='worker':
                    goals,_ = day_endpoints(world,actor,guidance.operator_stands)
                home_fields[actor.id] = distance_field(world,goals,actor.pos,deadline)
            return home_fields[actor.id]
        def circuits(actor, zone, actions):
            start = distance_field(world,[actor.pos],actor.pos,deadline)
            back = home(actor)
            return sorted((start[q]+actions+back[q],start[q],q,back[q])
                          for q in interaction_cells(world,world.zones.get(zone,()),actor.pos)
                          if q in start and q in back)
        checkout_actions = 1  # Replan each observed purchase; a four-item basket is not mandatory.
        shopping = circuits(buyer,'weaponShop',checkout_actions) if buyer else []
        shopping = [r for r in shopping if r[0]+margin<clock.until_night]
        if not shopping:
            buyer = None
        if (buyer and buyer.id not in self.pending and not self.order(world,buyer,rules,policy,deadline)
                and any(a.kind=='worker' and a.id in defender_ids(world) and a.id not in excluded
                        and self.order(world,a,rules,policy,deadline) for a in world.movers)):
            buyer=None  # A stocked pioneer must release W's personal upgrade/supply checkout.
        if buyer is None:
            # W may finish a short shop stop on its actual return circuit.
            # Never recruit the exterior miner or interrupt construction,
            # traffic, or a carried-voucher delivery to manufacture a buyer.
            options=[]
            for worker in world.movers:
                if (worker.kind!='worker' or worker.id not in defender_ids(world)
                        or worker.id in excluded or (jobs.get(worker.id) and not jobs[worker.id].get('gate'))
                        or worker.id not in guidance.operator_stands
                        or guidance.return_routes.get(worker.id,{}).get('due')
                        or worker.backpack is None or worker.capacity is None
                        or len(worker.backpack)>=worker.capacity
                        or any('UpgradeVoucher' in k and n for k,n in worker.inventory.items())):
                    continue
                paths=[r for r in circuits(worker,'weaponShop',1) if r[0]+margin<clock.until_night]
                if paths:options.append((paths[0][0],worker.id,worker,paths))
            if options:
                _,_,buyer,shopping=min(options,key=lambda o:o[:2])
                checkout_actions=1
        rows = {}; empty_workers = set()
        from .economy import construction_reservations
        reserves = construction_reservations(world,rules,jobs=jobs)
        for actor in world.movers:
            if actor.id in excluded or actor.backpack is None:
                continue
            if (getattr(world,'task_side_plan',None) and actor.id==world.night_roster.m
                    and clock.until_night<=35):
                continue  # Exterior miner cashes out after dawn, not before dusk.
            reserve = reserves.get(actor.id,{})
            # External gate preparation removes M's ordinary job. Its personal
            # stones still fund remaining walls and the next daily gate.
            stone = reserve.get('stone',0)
            if actor.kind=='worker' and actor.id==getattr(world.night_roster,'m',None):
                walls={u.pos for u in world.ours.values() if u.alive and u.kind=='wall'}
                stone=max(stone,len((world.wall_targets or set())-walls),1 if clock.day<10 and getattr(world,'task_side_plan',None) else 0)
            stock={k:max(0,actor.inventory[k]-(stone if k=='stone' else reserve.get(k,0)))
                   for k in sorted(MINERALS) if world.vendor.get(k,0)>0}
            stock={k:n for k,n in stock.items() if n}
            if not stock:
                if (actor.kind=='worker' and not any('UpgradeVoucher' in k and n for k,n in actor.inventory.items())
                        and actor.inventory['stone']>=stone
                        and (not jobs.get(actor.id) or jobs[actor.id].get('gate'))):
                    empty_workers.add(actor.id)
                continue
            paths=circuits(actor,'vendor',len(stock))
            if time.monotonic()>=deadline:
                self.diagnostic={'stage':'route_budget'};return []
            if paths:
                rows[actor.id]=(actor,stock,paths)
        earliest = max((paths[0][0]+margin+4 for _,_,paths in rows.values()),default=0)
        if shopping:
            _,travel,_,back=shopping[0]
            last_sale=max((paths[0][1]+len(stock) for _,stock,paths in rows.values()),default=0)
            earliest=max(earliest,max(travel,last_sale)+checkout_actions+back+margin)
        ready_purchase=bool(buyer and self.order(world,buyer,rules,policy,deadline))
        dawn_sale=bool(getattr(world,'task_side_plan',None) and world.night_roster.m in rows
                       and clock.day>1 and clock.until_night>35)
        if not self.started and clock.until_night>max(30,earliest) and not ready_purchase and not dawn_sale:
            return []
        self.started = True
        self.settled.update(empty_workers)
        self.diagnostic={'stage':'cashout','sellers':{},'buyer':buyer.id if buyer else None,
                         'fallback_worker':bool(buyer and buyer.kind=='worker'),
                         'blocked':{a.id:('reserved_duty' if a.id in excluded else
                             'no_return_stand' if a.id not in guidance.operator_stands else 'route_or_stock')
                             for a in world.movers if a.id in defender_ids(world)} if not buyer else {}}
        result=[];waiting=[]
        def install(actor, choices, stage):
            choices=[c for c in choices if guidance.permit(c)]
            if choices or stage=='wait_sales':
                world.sunset_actions[actor.id]=[c.command for c in choices]
                result.extend(choices)
                return True
            return False
        for identity,(actor,stock,paths) in rows.items():
            if actor.kind=='worker' and jobs.get(identity) and not jobs[identity].get('gate'):
                self.diagnostic['sellers'][identity]='construction';continue
            paths=[r for r in paths if r[0]+margin<clock.until_night]
            if not paths:
                self.diagnostic['sellers'][identity]='return_deadline';continue
            total,length,stand,back=paths[0]
            choices=([Candidate(identity,{'action':'sell','name':name,'num':stock[name]},180,
                                'sell personal surplus before final checkout')]
                     if length==0 and (name:=max(stock,key=lambda k:(stock[k]*world.vendor[k],k))) else
                     moves(actor,distance_field(world,[stand],actor.pos,deadline),'cash out personal surplus before night'))
            if install(actor,choices,'cashout'):
                self.settled.add(identity)
                # Wait only for sales that can finish before the buyer's last safe
                # checkout. Forecasts coordinate time, never spendable gold.
                if buyer and length+len(stock)+shopping[0][3]+checkout_actions+margin<clock.until_night:
                    waiting.append(identity)
                self.diagnostic['sellers'][identity]={'stock':stock,'steps':length+len(stock)}
            else:
                self.diagnostic['sellers'][identity]='duty_precedes_sale'
        # Once a worker has liquidated, do not restart mining before dusk and
        # strand a fresh batch. Builders can still finish their reserved walls.
        for identity in sorted(self.settled-set(rows)-set(excluded)-({buyer.id} if buyer else set())):
            if getattr(world,'task_side_plan',None) and identity==world.night_roster.m:continue
            actor=world.ours.get(identity)
            if (actor and actor.alive and actor.kind=='worker'
                    and not any('UpgradeVoucher' in k and n for k,n in actor.inventory.items())):
                choices=moves(actor,home(actor),'surplus sold: return to assigned evening duty')
                if not choices and actor.pos in home(actor):
                    world.sunset_actions[identity]=[]
                else:
                    install(actor,choices,'home')
        if buyer:
            world.sunset_buyer=buyer.id
            world.pioneer_trade_ids.discard(buyer.id)
            if buyer.id not in world.sunset_actions:
                total,length,stand,back=shopping[0]
                route=distance_field(world,[stand],buyer.pos,deadline)
                if waiting:
                    choices=moves(buyer,route,'designated buyer approaches checkout while personal sales settle')
                    # A real return deadline is never replaced by a wait.
                    if not guidance.return_routes.get(buyer.id,{}).get('due'):
                        install(buyer,choices,'wait_sales')
                        self.diagnostic['stage']='wait_sales'
                        self.diagnostic['waiting']=waiting
                elif buyer.id in self.pending:
                    install(buyer,moves(buyer,home(buyer),'purchase unresolved: return with observed inventory'),'home')
                    self.diagnostic['stage']='purchase_pending'
                else:
                    order=self.order(world,buyer,rules,policy,deadline)
                    if order:
                        name,num,reserve=order
                        choices=([Candidate(buyer.id,{'action':'buy','name':name,'num':num},180,
                                            'final checkout with observed team gold',gold_reserve=reserve)]
                                 if length==0 else moves(buyer,route,'designated buyer night-stock checkout'))
                        install(buyer,choices,'checkout')
                        self.diagnostic.update(stage='checkout',item=name,num=num,gold=world.gold)
                    else:
                        choices=moves(buyer,home(buyer),'night stock ready or unaffordable: return before dusk')
                        if not choices and buyer.pos in home(buyer):world.sunset_actions[buyer.id]=[]
                        else:install(buyer,choices,'home')
                        self.diagnostic['stage']='home'
        if time.monotonic()>=deadline:
            world.sunset_actions={};world.sunset_buyer=None
            self.diagnostic={'stage':'route_budget'}
            return []
        return result

    def order(self, world, buyer, rules, policy, deadline):
        if world.gold is None or buyer.capacity is None or len(buyer.backpack)>=buyer.capacity:
            return None
        # Preserve construction cash; purchases use quotes from this snapshot.
        costs=[r.gold for k in WEAPONS if (r:=rules.build_rule(world,k)) is not None]
        reserve=min(costs,default=0)*max(0,rules.weapon_limit-len(world.weapons))
        reserve += getattr(world,"treasure_reserved_gold",0)
        cash=max(0,world.gold-reserve)
        targets,rank,restricted,_=procurement.upgrade_demand(world,policy,rules=rules)
        actors={a.id:a for a in world.movers if a.backpack is not None}
        fields={}
        def route(actor,target):
            key=actor.id,target['unit'].id
            if key not in fields:
                fields[key]=distance_field(world,interaction_cells(world,[target['unit'].pos],actor.pos),actor.pos,deadline)
            return fields[key]
        targets,_,_=procurement.match_carried_supply(targets,actors,deadline,route)
        stand=[getattr(world,'pioneer_trade_stands',{}).get(buyer.id,buyer.pos)]
        # Night stock belongs to this buyer: only buy for targets reachable
        # from its own assigned stand; never promise an inventory transfer.
        demands=sorted((t for t in targets.values() if (not restricted or t['rank']==rank)
                        and any(distance(p,t['unit'].pos)<=1 for p in stand)),key=lambda t:(t['rank'],t['unit'].id))
        choices=[(t['name'],1) for t in demands]
        walls=[u for u in world.ours.values() if u.alive and u.kind=='wall'
               and any(distance(p,u.pos)<=1 for p in stand)]
        if walls:choices.append(('WallFixer',max(0,2-buyer.inventory['WallFixer'])))
        choices.append(('Medicine',max(0,1-buyer.inventory['Medicine'])))
        # One ranged emergency item after local maintenance stock and upgrades.
        if not buyer.inventory['Bomb'] and not buyer.inventory['DizzyWeapon']:
            choices.extend((name,1) for name in ('Bomb','DizzyWeapon'))
        for name,num in choices:
            price=world.shop.get(name)
            if num and price is not None and price>0 and cash>=price:
                return name,min(num,buyer.capacity-len(buyer.backpack),cash//price),reserve
        return None

    def finalize(self, world, response):
        identity=getattr(world,'sunset_buyer',None)
        cmd=response['roleCommandMap'].get(identity,{})
        if cmd.get('action')=='buy':
            self.pending[identity]={'name':cmd['name'],'prior':world.ours[identity].inventory[cmd['name']],
                                    'round':world.round}


def permits(world, candidate):
    """Apply the same evening commitment to incumbents and later planners."""
    command=candidate.command;identity=candidate.actor
    allowed=getattr(world,'sunset_actions',{})
    buyer=getattr(world,'sunset_buyer',None)
    if command in getattr(world,'treasure_actions',{}).get(identity,()):
        return True
    if command.get('action')=='buy' and buyer and identity!=buyer:
        actor=world.ours.get(identity)
        return bool(command.get('name')=='Medicine' and actor and actor.health<=110)
    if identity not in allowed:
        return True
    if command.get('action')=='use' and command.get('name') in {'Medicine','Bomb','DizzyWeapon','WallFixer'}:
        return True
    if identity!=buyer and command.get('action') in {'build','remove','use'}:
        return True
    return command in allowed[identity]
