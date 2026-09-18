"""One daylight level-one wall transaction, advanced by observed receipts."""
from dataclasses import dataclass, field
import time

from .arbitration import Candidate
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import pos_json, distance
from . import rear_open, defence_duties


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


@dataclass
class WallRebuild:
    plan: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)

    def prepare(self, world, clock, rules, policy, deadline, session):
        world.wall_rebuild_actions = {}
        world.wall_rebuild_plan = self.plan
        self.diagnostic = dict(stage='idle')
        if not rear_open.enabled(world):
            return []
        worker = world.ours.get(world.night_roster.w)
        if not worker or not worker.alive or worker.backpack is None:
            self.diagnostic = dict(stage='owner_unavailable', plan=dict(self.plan))
            return []
        p = self.plan
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
                    for owner, targets in session.sunset_market.checkout_targets.items():
                        session.sunset_market.checkout_targets[owner] = [
                            (wall.id if uid == p['old_id'] else uid, level) for uid,level in targets]
                    for owner, targets in p.get('paid_bindings',{}).items():
                        saved=session.sunset_market.checkout_targets.setdefault(owner,[])
                        for _,level in targets:
                            if (wall.id,level) not in saved:saved.append((wall.id,level))
                    session.wall_restore_observations[wall.id] = dict(round=world.round, level=1, hp=wall.health)
                    world.wall_restore_observations = session.wall_restore_observations
            elif action == 'use' and issued['command']['name'] == 'WallUpgradeVoucher1' and wall and wall.level >= 2:
                if ack is True and worker.inventory['WallUpgradeVoucher1'] < issued['voucher']:
                    self.diagnostic = dict(stage='DONE', position=p['position'], wall=wall.id)
                    self.plan = {}; p = {}; world.wall_rebuild_plan = p
            if ack is False or ack is True or world.round > issued['round']+1:
                self.pending = {}
        if clock.phases != {'day'}:
            self.diagnostic = dict(stage='night_pause', plan=dict(p)) if p else self.diagnostic
            return []
        # Never open a second gap. Existing mandatory construction goes first.
        required = set(world.wall_targets or ()) & rear_open.required(world)
        if not p:
            if required - walls.keys():
                return []
            options = sorted((u for u in walls.values() if u.pos in required and damaged(world,u)),
                             key=lambda u:(distance(worker.pos,u.pos),u.health,u.id))
            if not options:
                return []
            wall = options[0]
            p = self.plan = dict(actor=worker.id, position=wall.pos, old_id=wall.id,
                                stage='SUPPLY', created=world.round,
                                paid_bindings={i:[(uid,level) for uid,level in targets if uid==wall.id]
                                    for i,targets in session.sunset_market.checkout_targets.items()})
            world.wall_rebuild_plan = p
        wall = walls.get(p['position'])
        if wall and wall.level >= 2:
            self.plan = {}; world.wall_rebuild_plan = {}
            self.diagnostic = dict(stage='observed_upgraded', wall=wall.id)
            return []
        if p['stage'] == 'SUPPLY' and wall and not damaged(world,wall):
            self.plan = {}; world.wall_rebuild_plan = {}
            return []
        if wall is None:
            p['stage'] = 'REBUILD'  # A real externally destroyed wall also needs closure.
        elif wall.id != p['old_id'] and p['stage'] != 'UPGRADE':
            # An independent builder restored this coordinate; never remove it.
            p.update(stage='UPGRADE', new_id=wall.id)
        if self.pending:
            self.diagnostic = dict(stage='pending_receipt', plan=dict(p))
            world.wall_rebuild_actions[worker.id] = []
            return []
        if worker.health < 220 and worker.inventory['Medicine']:
            return self._offer(world, worker, dict(action='use',name='Medicine'), 'heal_first')
        if any(r.alive and r.target_team in (None,world.side) for r in world.robots.values()):
            self.diagnostic = dict(stage='observed_threat_blocks_demolition', plan=dict(p))
            return []
        start = distance_field(world,{worker.pos},worker.pos,deadline)
        home = distance_field(world,defence_duties.stands(world,worker.id),worker.pos,deadline)
        stands = interaction_cells(world,[p['position']],worker.pos)
        options = [(start[q]+home[q],q) for q in stands & start.keys() & home.keys()]
        if not options:
            self.diagnostic = dict(stage='no_service_route', plan=dict(p))
            return []
        _, stand = min(options)
        service = distance_field(world,{stand},worker.pos,deadline)
        needed_steps = start[stand]+home[stand]+policy.return_buffer+5
        material = worker.inventory['stone'] > 0
        paid_elsewhere=sum(level==1 and uid not in {p['old_id'],p.get('new_id')}
            for uid,level in getattr(world,'checkout_targets',{}).get(worker.id,()))
        voucher = worker.inventory['WallUpgradeVoucher1'] > paid_elsewhere
        targets, action = {stand}, None
        if not material and p['stage'] != 'UPGRADE':
            goals = interaction_cells(world,world.zones.get('stone',()),worker.pos)
            options = [(start[q]+1+service[q]+home[stand]+policy.return_buffer+5,q)
                       for q in goals & start.keys() & service.keys()]
            if not options:
                self.diagnostic = dict(stage='personal_stone_unavailable', plan=dict(p)); return []
            needed_steps, goal = min(options); targets = {goal}
            if worker.pos == goal:
                rock = min(q for q in world.zones['stone'] if distance(q,worker.pos)<=1)
                action = dict(action='collect',targetPos=[pos_json(rock)])
        elif not voucher and p['stage'] != 'REBUILD':
            price = world.shop.get('WallUpgradeVoucher1')
            reserve = getattr(world,'treasure_reserved_gold',0)
            from .funding import permits_bundle
            quote=Candidate(worker.id,dict(action='buy',name='WallUpgradeVoucher1',num=1),0,'rebuild funding quote')
            if not price or world.gold is None or world.gold < price+reserve or not permits_bundle(world,[quote],price):
                self.diagnostic = dict(stage='upgrade_funding_wait', plan=dict(p), required=price)
                return []
            shops = interaction_cells(world,world.zones.get('weaponShop',()),worker.pos)
            options = [(start[q]+1+service[q]+home[stand]+policy.return_buffer+5,q)
                       for q in shops & start.keys() & service.keys()]
            if not options:
                self.diagnostic = dict(stage='upgrade_shop_unreachable', plan=dict(p)); return []
            needed_steps, goal = min(options); targets = {goal}
            if worker.pos == goal:
                action = dict(action='buy',name='WallUpgradeVoucher1',num=1)
        elif worker.pos == stand:
            if p['stage'] == 'REBUILD':
                action = dict(action='build',name='wall',targetPos=[pos_json(p['position'])])
            elif p['stage'] == 'UPGRADE':
                action = dict(action='use',name='WallUpgradeVoucher1',targetPos=[pos_json(p['position'])])
            elif wall and wall.id == p['old_id'] and damaged(world,wall):
                action = dict(action='remove',targetPos=[pos_json(p['position'])])
        if needed_steps > clock.until_night and p['stage'] == 'SUPPLY':
            self.diagnostic = dict(stage='daylight_window_insufficient', required=needed_steps, plan=dict(p))
            return []
        if action is None:
            route = distance_field(world,targets,worker.pos,deadline)
            steps = sorted(q for q in neighbours(worker.pos) if route.get(q,float('inf')) < route.get(worker.pos,0))
            if not steps or time.monotonic() >= deadline:
                return []
            action = dict(action='move',targetPos=[pos_json(steps[0])])
        # Demolition is never granted on a speculative future receipt.
        return self._offer(world,worker,action,p['stage'])

    def _offer(self, world, worker, command, stage):
        world.wall_rebuild_actions[worker.id] = [command]
        self.diagnostic = dict(stage=stage, plan=dict(self.plan), command=command)
        return [Candidate(worker.id,command,900,'complete observed wall rebuild transaction')]

    def finalize(self, world, response):
        if not self.plan:
            return
        actor = world.ours.get(self.plan['actor'])
        command = response['roleCommandMap'].get(self.plan['actor'])
        if actor and command in (commands(world, actor.id) or ()) and command['action'] in {'remove','build','use','buy'}:
            self.pending = dict(actor=actor.id, round=world.round, command=command,
                                stone=actor.inventory['stone'], voucher=actor.inventory['WallUpgradeVoucher1'])
