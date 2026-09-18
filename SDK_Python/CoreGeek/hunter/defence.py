"""Optional observed-wave resupply; no invented future waves or shared bags."""
from collections import Counter
from dataclasses import dataclass, field
import time

from .arbitration import Candidate
from .combat import area_targets, threat_weights
from .navigation import route
from .protocol import distance, pos_json

ITEMS = ('Bomb', 'DizzyWeapon')


@dataclass
class DefenceProcurement:
    spent: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    offered: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)

    def reconcile(self, world, clock):
        feedback = world.raw.get('lastRoundRoleActionResults', {})
        feedback = feedback if isinstance(feedback, dict) else {}
        for identity, order in list(self.pending.items()):
            actor = world.ours.get(identity)
            failed = world.round == order['round']+1 and feedback.get(identity) is False
            covered = actor is not None and actor.backpack is not None and actor.inventory[order['name']] > order['prior_count']
            if failed:
                self.spent[order['day']] = max(0, self.spent.get(order['day'], 0)-order['price'])
            if failed or covered:
                self.pending.pop(identity)
        # At most ten match days; retained unknown purchases do not disappear
        # merely because a new day or a skipped observation arrives.
        self.spent = dict(sorted(self.spent.items())[-10:])

    def candidates(self, world, clock, policy, deadline, task_actor=None, selected=(), guidance=None):
        self.offered = {}
        self.diagnostic = {'status':'disabled', 'pending_purchases':len(self.pending),
                           'spent_this_day':self.spent.get(clock.day, 0)}
        if not policy.defence_procurement_enabled:
            return []
        self.diagnostic['status'] = 'no_eligible_window'
        if clock.phases != {'night'} or clock.day is None or not 1 <= clock.day <= 10 or not world.stations:
            return []
        turns = min(130-(clock.round-origin)%130 for origin in clock.offsets)
        if turns < 2 or world.gold is None or not world.zones.get('weaponShop'):
            return []
        if guidance and guidance.base_critical:
            self.diagnostic['status'] = 'immediate_survival_precedes_future_purchase'
            return []
        roster=getattr(world,'night_roster',None)
        actors = [a for a in world.movers if a.id != task_actor and (roster is None or a.id==roster.w)]
        self.diagnostic['status'] = 'inventory_or_purchase_unresolved'
        if self.pending or any(a.backpack is None for a in actors):
            return []
        if any(a.inventory[name] for a in actors for name in ITEMS):
            self.diagnostic['status'] = 'use_existing_personal_stock_first'
            return []
        budget = min(max(0, world.gold-policy.reserve_gold),
                     max(0, policy.defence_gold_limit-self.spent.get(clock.day, 0)))
        self.diagnostic.update(status='no_positive_resupply_option', available_budget=budget, turns_before_day=turns)
        damage, suppressed, busy = Counter(), set(), set()
        for candidate in selected:
            damage.update(candidate.damage)
            suppressed.update(candidate.suppression)
            if candidate.command['action'] in {'attack', 'use', 'submitAnswer', 'acceptTask'}:
                busy.add(candidate.command.get('controllerId', candidate.actor))
        threats = [r for r in world.robots.values() if r.alive and r.abnormal != 'dizzy'
                   and r.id not in suppressed and damage[r.id] < r.health
                   and r.target_team in (None, world.side)
                   and any(distance(r.pos, base.pos) <= 6 for base in world.stations)]
        centres, weights = area_targets(world, threats, deadline), threat_weights(world)
        options = []
        for actor in actors:
            if time.monotonic() >= deadline:
                break
            if actor.id in busy or actor.capacity is None or len(actor.backpack) >= actor.capacity:
                continue
            length, steps = route(world, actor, world.zones['weaponShop'], deadline)
            if length is None or length > 3 or length+2 > turns:
                continue
            for name in ITEMS:
                price = world.shop.get(name)
                if price is None or price > budget:
                    continue
                benefits = [(sum(weights[i]*(min(world.robots[i].health-damage[i], 100) if name == 'Bomb' else 15)
                                 for i in sorted(ids)), p) for p, ids in centres]
                benefit = max((value for value, _ in benefits), default=0)
                # Buying must not create stock that the existing use policy
                # would refuse even in this same residual-threat snapshot.
                if benefit <= (40 if name == 'Bomb' else 35):
                    continue
                # Same uncalibrated item-value scale as immediate combat, using
                # current quote instead of assuming the reference price is live.
                net = benefit-price*(.4 if name == 'Bomb' else .35)
                if net <= 0:
                    continue
                utility = min(80, net/(length+2))
                commands = ([{'action':'buy', 'name':name, 'num':1}] if length == 0 else
                            [{'action':'move', 'targetPos':[pos_json(p)]} for p in steps[:4]])
                candidates = [Candidate(actor.id, command, utility-i*.01,
                                        'observed-wave resupply; current residual threat, personal buyer and bounded cost',
                                        gold_reserve=policy.reserve_gold if length == 0 else 0)
                              for i, command in enumerate(commands)]
                candidates = [c for c in candidates if guidance is None or guidance.permit(c)]
                if candidates:
                    options.append((-utility, price, length, actor.id, name, candidates))
        if not options:
            return []
        _, price, length, identity, name, result = min(options, key=lambda row: row[:5])
        self.diagnostic.update(status='buy' if length == 0 else 'approach_shop', buyer=identity, item=name,
                               quote=price, travel_actions=length, minimum_actions=length+2,
                               residual_threats=[r.id for r in threats])
        if length == 0:
            self.offered[identity] = {'day':clock.day, 'round':world.round, 'name':name, 'price':price,
                                      'prior_count':world.ours[identity].inventory[name]}
        return result

    def finalize(self, world, response):
        for identity, order in self.offered.items():
            command = response['roleCommandMap'].get(identity, {})
            if command == {'action':'buy', 'name':order['name'], 'num':1}:
                self.pending[identity] = dict(order)
                self.spent[order['day']] = self.spent.get(order['day'], 0)+order['price']
        self.offered = {}
