"""One daylight level-one wall transaction, advanced by observed receipts."""
from dataclasses import dataclass, field
import time

from .arbitration import Candidate
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import pos_json, distance
from . import rear_open, defence_duties
from .day_schedule import weighted_field


def damaged(world, wall):
    if not rear_open.enabled(world) or wall.kind != 'wall' or not wall.alive or wall.level != 1:
        return False
    restored = getattr(world, 'wall_restore_observations', {}).get(wall.id)
    if restored and restored['level'] == 1 and wall.health >= restored['hp']:
        return False
    limit = getattr(world, 'wall_health_levels', {}).get(1, {})
    return bool((limit and not limit.get('conflict') and wall.health < limit['hp'])
                or getattr(world, 'observed_wall_losses', {}).get(wall.id, 0) > 0)


def commands(world, identity):
    return getattr(world, 'wall_rebuild_actions', {}).get(identity)


def stone_reserve(world, identity, rules):
    plan=getattr(world,'wall_rebuild_plan',{})
    rule=rules.build_rule(world,'wall')
    if not plan or plan.get('actor')!=identity or not rule:return 0
    wall=next((u for u in world.ours.values() if u.alive and u.kind=='wall' and u.pos==plan['position']),None)
    return rule.items.get('stone',0) if wall and damaged(world,wall) else 0


def upgrade_ready(world, wall):
    # Damage is not a prerequisite to repair: a legal upgrade restores HP.
    return wall.alive and wall.level in (1,2)


@dataclass
class WallRebuild:
    plan: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)
    reconciled_round: int = -1

    def reconcile(self, world, clock, rules, policy, session):
        """Quote-independent target/receipt reconciliation (design §6.1 step 3).

        Runs before funding publication so revoked or newly created plans are
        visible to this frame's grants (E5). Action planning stays in prepare.
        """
        if self.reconciled_round == world.round:
            return
        self.reconciled_round = world.round
        if not rear_open.enabled(world):
            return
        worker = world.ours.get(world.night_roster.w)
        if not worker or not worker.alive or worker.backpack is None:
            return
        p = self.plan
        if p:
            p.setdefault('target_level',2 if p['stage']=='UPGRADE' or any(p.get('paid_bindings',{}).values()) else 1)
        if p and p['actor'] != worker.id:
            # Inventory stays personal. A replacement must independently fund
            # the build before it can take over the same physical obligation.
            p['actor'] = worker.id
            self.pending = {}
        walls = {u.pos:u for u in world.ours.values() if u.alive and u.kind == 'wall'}
        wall = walls.get(p.get('position')) if p else None
        if self.pending and world.round > self.pending['round']:
            issued = self.pending
            feedback = world.raw.get('lastRoundRoleActionResults', {})
            ack = feedback.get(issued['actor']) if isinstance(feedback, dict) and world.round == issued['round']+1 else None
            action = issued['command']['action']
            if action == 'remove' and wall is None and ack is True:
                p['stage'] = 'REBUILD'
            elif action == 'build' and wall and wall.level == 1:
                if ack is True and worker.inventory['stone'] < issued['stone']:
                    p.update(stage='UPGRADE', new_id=wall.id)
                    # Only this transaction may rebind paid vouchers.
                    session.sunset_market.rebind_delivery(world, p['old_id'], wall,
                                                          p.get('paid_bindings', {}))
                    session.wall_restore_observations[wall.id] = dict(round=world.round, level=1, hp=wall.health)
                    world.wall_restore_observations = session.wall_restore_observations
                    if p.get('target_level',1)==1:
                        self.diagnostic=dict(stage='DONE',position=p['position'],wall=wall.id,target_level=1)
                        self.plan={};p={};world.wall_rebuild_plan={}
            elif action == 'use' and issued['command']['name'] == 'WallUpgradeVoucher1' and wall and wall.level >= 2:
                if ack is True and worker.inventory['WallUpgradeVoucher1'] < issued['voucher']:
                    self.diagnostic = dict(stage='DONE', position=p['position'], wall=wall.id)
                    self.plan = {}; p = {}; world.wall_rebuild_plan = p
            if ack is False or ack is True or world.round > issued['round']+1:
                self.pending = {}
        if clock.phases != {'day'}:
            return
        required = set(world.wall_targets or ()) & rear_open.required(world)
        from .wall_policy import upgrade_targets
        from .wall_policy import investment_targets
        direct_upgrade=(set(investment_targets(world)) if worker.inventory['WallUpgradeVoucher1'] else set())
        if p and p['stage']=='SUPPLY' and wall and wall.pos in direct_upgrade:
            # An unopened transaction can yield to direct upgrading. A real
            # hole or completed rebuild still keeps its existing owner.
            self.plan={};p={};world.wall_rebuild_plan={}
            self.diagnostic=dict(stage='direct_upgrade',wall=wall.id,reason='upgrade restores damaged level-one wall')
        if not p:
            options = sorted((u for u in walls.values() if u.pos in required-direct_upgrade and damaged(world,u)),
                             key=lambda u:(distance(worker.pos,u.pos),u.health,u.id))
            if not options:
                world.wall_rebuild_plan = self.plan
                return
            wall=options[0]
            p=self.plan=dict(actor=worker.id,position=wall.pos,old_id=wall.id,stage='SUPPLY',
                created=world.round,last_progress_round=world.round,target_level=1,
                paid_bindings={i:[(uid,level) for uid,level in targets if uid==wall.id]
                    for i,targets in session.sunset_market.checkout_targets.items()})
            if any(p['paid_bindings'].values()):p['target_level']=2
            world.wall_rebuild_plan=p
        wall=walls.get(p['position'])
        if wall and wall.level>=2:
            self.plan={};world.wall_rebuild_plan={}
            self.diagnostic=dict(stage='observed_upgraded',wall=wall.id);return
        if p['stage']=='SUPPLY' and wall and not damaged(world,wall):
            self.plan={};world.wall_rebuild_plan={};return
        if wall is None:p['stage']='REBUILD'
        elif wall.id!=p['old_id']:
            if p.get('target_level',1)==1:
                self.plan={};world.wall_rebuild_plan={}
                self.diagnostic=dict(stage='DONE',position=wall.pos,wall=wall.id,target_level=1);return
            p.update(stage='UPGRADE',new_id=wall.id)
        if wall and p['stage']=='SUPPLY' and p.get('target_level',1)>1:
            p['stage']='UPGRADE'  # An intact upgrade target never needs demolition first.
        snapshot=(worker.pos,tuple(sorted(worker.inventory.items())),wall.id if wall else None,
                  wall.level if wall else None,p['stage'])
        if snapshot!=p.get('observation'):
            old=p.get('observation')
            if not old or snapshot[1:]!=old[1:]:p['last_progress_round']=world.round
            if not old or snapshot[0]!=old[0]:p['last_motion_round']=world.round
            p['observation']=snapshot
        world.wall_rebuild_plan = self.plan

    def prepare(self, world, clock, rules, policy, deadline, session):
        world.wall_rebuild_actions = {}
        world.wall_rebuild_plan = self.plan
        self.diagnostic = dict(stage='idle')
        self.reconcile(world, clock, rules, policy, session)
        if not rear_open.enabled(world):
            return []
        worker = world.ours.get(world.night_roster.w)
        if not worker or not worker.alive or worker.backpack is None:
            self.diagnostic = dict(stage='owner_unavailable', plan=dict(self.plan))
            return []
        p = self.plan
        walls = {u.pos:u for u in world.ours.values() if u.alive and u.kind == 'wall'}
        wall = walls.get(p.get('position')) if p else None
        if clock.phases != {'day'}:
            self.diagnostic = dict(stage='night_pause', plan=dict(p)) if p else self.diagnostic
            return []
        required = set(world.wall_targets or ()) & rear_open.required(world)
        from .wall_policy import upgrade_targets
        from .wall_policy import investment_targets
        direct_upgrade=set(investment_targets(world))
        from .wall_policy import daily_upgrade_targets
        due=daily_upgrade_targets(world)
        held_front=any(u.inventory[f'WallUpgradeVoucher{target.level}'] for u in world.movers
                       if u.backpack is not None for target in due)
        front_gap=bool(direct_upgrade-set(walls))
        if (not p or p['stage']=='SUPPLY' and p.get('target_level',1)==1) and (front_gap or due and held_front):
            # An intact optional side wall has not acquired exclusive work rights.
            if p:p.pop('segment',None)
            return self._blocked(world,worker,'yield_front_upgrade')
        if p and p['stage']=='SUPPLY' and p.get('target_level',1)==1:
            if world.round<p.get('retry_after',0):
                return self._blocked(world,worker,'unstarted_maintenance_yield')
            if world.round-p.get('last_progress_round',world.round)>=12:
                p.update(retry_after=world.round+4,last_progress_round=world.round)
                p.pop('segment',None)
                return self._blocked(world,worker,'unstarted_maintenance_no_progress')
        if not p:
            return []  # reconcile found no damaged required wall to transact.
        if worker.health<220 and worker.inventory['Medicine']:
            return self._offer(world,worker,dict(action='use',name='Medicine'),'heal_first')
        if self.pending or session.sunset_market.purchase_pending(world, worker.id):
            return self._blocked(world,worker,'receipt_unknown',hold=True)
        if any(r.alive and r.target_team in (None,world.side) for r in world.robots.values()):
            return self._blocked(world,worker,'observed_threat_blocks_demolition')
        rule=rules.build_rule(world,'wall')
        if not rule or not rule.items.get('stone'):
            return self._blocked(world,worker,'unknown_build_cost')
        missing_stone=max(0,rule.items['stone']-worker.inventory['stone']) if p['stage']!='UPGRADE' else 0
        paid_elsewhere=sum(level==1 and uid not in {p['old_id'],p.get('new_id')}
            for uid,level in getattr(world,'checkout_targets',{}).get(worker.id,()))
        missing_voucher=p.get('target_level',1)>1 and worker.inventory['WallUpgradeVoucher1']<=paid_elsewhere
        home=distance_field(world,defence_duties.stands(world,worker.id),worker.pos,deadline)
        if time.monotonic()>=deadline or not getattr(home,'complete',True):
            return self._blocked(world,worker,'planner_budget_exhausted',hold=p['stage']=='REBUILD')
        # After opening a hole, close it before any shopping. All other stages
        # may carry a bounded supply trip across days without opening the wall.
        orders=[]
        slots=max(0,(worker.capacity or 0)-len(worker.backpack)-missing_stone)
        from .repair_decision import stock_target
        from .guard_stock import requirements as guard_requirements
        stock=guard_requirements(world,worker,policy)
        if not missing_stone and not missing_voucher:
            # Materials for this transaction are already personal. Optional
            # restocking must not send W away before closing the local wall job.
            stock=[(name,n) for name,n in stock if name=='Medicine' and worker.health<220]
        if p['stage']!='REBUILD':
            from .funding import permits_bundle
            for name,target in stock+([('WallUpgradeVoucher1',paid_elsewhere+1)] if missing_voucher else []):
                price=world.shop.get(name,0)
                qty=min(slots,max(0,target-worker.inventory[name])) if price>0 else 0
                while qty:
                    trial=orders+[dict(action='buy',name=name,num=qty)]
                    quoted=[Candidate(worker.id,c,0,'wall supply') for c in trial]
                    if permits_bundle(world,quoted,sum(world.shop[c['name']]*c['num'] for c in trial)):
                        orders=trial;slots-=qty;break
                    qty-=1
        # Being at the shop is a real opportunity: fund personal treatment and
        # stock first even when today's remaining demolition tour cannot fit.
        if orders and world.near_zone(worker.pos,'weaponShop'):
            needed=home.get(worker.pos,float('inf'))+len(orders)+policy.return_buffer+2
            if needed<=clock.until_night or orders[0]['name']=='Medicine' and worker.health<220:
                return self._offer(world,worker,orders[0],'supply_at_shop')
        service_goals=interaction_cells(world,[p['position']],worker.pos)
        service=weighted_field(world,{q:home[q]+(2 if p['stage']=='UPGRADE' else 3 if p['stage']=='REBUILD' else 5)
                                     for q in service_goals if q in home},worker,deadline)
        if service is None:return self._blocked(world,worker,'planner_budget_exhausted',hold=p['stage']=='REBUILD')
        mines=interaction_cells(world,world.zones.get('stone',()),worker.pos)
        shops=interaction_cells(world,world.zones.get('weaponShop',()),worker.pos)
        groups={}
        if missing_stone:groups['stone']=(mines,missing_stone)
        if orders:groups['shop']=(shops,len(orders)+(1 if worker.health<220 and not worker.inventory['Medicine'] else 0))
        can_finish=not missing_voucher or any(c['name']=='WallUpgradeVoucher1' for c in orders)
        if p['stage']=='REBUILD':can_finish=True
        gaps=required-walls.keys()-{p['position']}
        # Recheck on every demolition attempt, not just when creating a plan.
        if gaps and p['stage']=='SUPPLY':can_finish=False
        def quote(tail):
            sequences=[tuple(groups)]
            if len(groups)==2:sequences.append(tuple(reversed(tuple(groups))))
            options=[]
            for seq in sequences:
                current=tail;routes={}
                for name in reversed(seq):
                    goals,actions=groups[name]
                    current=weighted_field(world,{q:current[q]+actions for q in goals if q in current},worker,deadline)
                    if current is None:break
                    routes[name]=current
                if current is not None and worker.pos in current:
                    options.append((current[worker.pos]+policy.return_buffer,seq,current,routes))
            return min(options,key=lambda row:(row[0],row[1]!=tuple(p.get('segment',())))) if options else None
        full=quote(service) if can_finish else None
        chosen=full if full and full[0]<=clock.until_night else quote(home) if groups else None
        if time.monotonic()>=deadline:
            return self._blocked(world,worker,'planner_budget_exhausted',hold=p['stage']=='REBUILD')
        p['full_required']=full[0] if full else None
        if not chosen or chosen[0]>clock.until_night:
            p.pop('segment',None)
            return self._blocked(world,worker,'daylight_window_insufficient' if chosen or full else
                'missing_personal_voucher' if missing_voucher else 'no_service_route',hold=p['stage']=='REBUILD')
        needed,seq,route,_=chosen
        p.update(segment=list(seq),expected_finish=world.round+needed,phase_deadline=world.round+clock.until_night)
        if seq:
            name=seq[0]
            if name=='stone' and worker.pos in mines:
                if (worker.capacity or 0)<=len(worker.backpack):return self._blocked(world,worker,'capacity')
                rock=min(q for q in world.zones['stone'] if distance(q,worker.pos)<=1)
                return self._offer(world,worker,dict(action='collect',targetPos=[pos_json(rock)]),'acquire_stone')
            if name=='shop' and worker.pos in shops:
                return self._offer(world,worker,orders[0],'acquire_supplies')
        elif worker.pos in service_goals:
            if p['stage']=='REBUILD':action=dict(action='build',name='wall',targetPos=[pos_json(p['position'])])
            elif p['stage']=='UPGRADE':action=dict(action='use',name='WallUpgradeVoucher1',targetPos=[pos_json(p['position'])])
            elif not gaps and not missing_stone and not missing_voucher:
                action=dict(action='remove',targetPos=[pos_json(p['position'])])
            else:return self._blocked(world,worker,'prerequisites_changed')
            return self._offer(world,worker,action,p['stage'])
        steps=sorted(q for q in neighbours(worker.pos) if route.get(q,float('inf'))<route.get(worker.pos,0))
        if not steps:return self._blocked(world,worker,'route_incomplete',hold=p['stage']=='REBUILD')
        return self._offer(world,worker,dict(action='move',targetPos=[pos_json(steps[0])]),
                           'prepare_next_day' if chosen is not full else p['stage'])

    def _blocked(self,world,worker,reason,hold=False):
        self.diagnostic=dict(stage=reason,plan=dict(self.plan),next_action_owner=worker.id,
                             blocked_reason=reason)
        if hold:world.wall_rebuild_actions[worker.id]=[]
        return []

    def _offer(self, world, worker, command, stage):
        world.wall_rebuild_actions[worker.id] = [command]
        self.diagnostic = dict(stage=stage, plan=dict(self.plan), command=command)
        candidate = Candidate(worker.id,command,900,'complete observed wall rebuild transaction')
        if command.get('action') == 'buy':
            # The SUPPLY purchase and its receipt belong to the single market
            # ledger; remove/build/use construction receipts stay here (§6.3).
            market = getattr(world, 'procurement_market', None)
            if market is not None:
                tracked = market.track(world, candidate, 'wall_rebuild_supply', step='supply')
                if tracked is None:
                    return self._blocked(world, worker, 'procurement_work_busy')
                candidate = tracked
        return [candidate]

    def finalize(self, world, response):
        if not self.plan:
            return
        actor = world.ours.get(self.plan['actor'])
        command = response['roleCommandMap'].get(self.plan['actor'])
        if actor and command in (commands(world, actor.id) or ()) and command['action'] in {'remove','build','use'}:
            self.pending = dict(actor=actor.id, round=world.round, command=command,
                                stone=actor.inventory['stone'], voucher=actor.inventory['WallUpgradeVoucher1'])
