"""Observed low-health treatment trips; no shared inventory or predicted buys."""
from dataclasses import dataclass, field
from copy import copy
import time

from .arbitration import Candidate
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import WEAPONS, pos_json


def needs_treatment(world,actor,clock):
    maximum=200 if actor.kind=='pioneer' else 220
    if actor.health*2<=maximum:return True
    injury=getattr(world,'persistent_mover_injuries',{}).get(actor.id)
    roster=getattr(world,'night_roster',None)
    # Exterior workers need a new-day treatment opportunity for substantial
    # unrecovered damage. A fresh scratch does not consume the last reserve.
    # Damage comes from consecutive observations, not an assumed enemy DPS.
    return bool(roster and actor.id in (roster.m,roster.w,roster.p) and injury and clock.day is not None
                and injury['day']<clock.day and actor.health<=2*injury['damage'])


@dataclass
class MedicalSupply:
    pending: dict = field(default_factory=dict)
    spent: dict = field(default_factory=dict)
    offered: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)
    detours: dict = field(default_factory=dict)
    offered_detours: dict = field(default_factory=dict)

    def reconcile(self, world, clock):
        feedback = world.raw.get('lastRoundRoleActionResults', {})
        feedback = feedback if isinstance(feedback, dict) else {}
        for identity, order in list(self.pending.items()):
            actor = world.ours.get(identity)
            failed = world.round == order['round']+1 and feedback.get(identity) is False
            observed = actor is not None and actor.backpack is not None and actor.inventory['Medicine'] > 0
            if failed:
                self.spent[order['day']] = max(0, self.spent.get(order['day'], 0)-order['price'])
            if failed or observed:
                self.pending.pop(identity)
        self.spent = dict(sorted(self.spent.items())[-10:])

    def candidates(self, world, clock, rules, policy, deadline, guidance, excluded=(), *, construction_jobs=None):
        self.offered = {}
        self.offered_detours = {}
        self.detours = {i:d for i,d in self.detours.items() if d['day']==clock.day
                        and i in world.ours and world.ours[i].alive and i not in excluded}
        self.diagnostic = {'status': 'disabled', 'pending': sorted(self.pending), 'plans': {}}
        if not policy.medical_supply_enabled:
            return []
        self.diagnostic['status'] = 'no_eligible_trip'
        if clock.phases != {'day'}:
            return []
        result = []
        actors = sorted((a for a in world.movers if a.id not in excluded),
                        key=lambda a: (a.health/(200 if a.kind == 'pioneer' else 220), a.id))
        # Preserve enough current gold for the remaining known weapon slots.
        costs = [r.gold for name in sorted(WEAPONS) if (r := rules.build_rule(world, name)) is not None]
        reserve = min(costs, default=0)*max(0, rules.weapon_limit-len(world.weapons))
        budget = min(max(0, (world.gold or 0)-reserve), max(0, policy.medical_gold_limit-self.spent.get(clock.day, 0)))
        self.diagnostic.update(reserve_gold=reserve, available_budget=budget)
        optional_spent = 0
        for actor in actors:
            if time.monotonic() >= deadline:
                break
            maximum = 200 if actor.kind == 'pioneer' else 220
            treatment = needs_treatment(world,actor,clock)
            detour = self.detours.get(actor.id)
            view = copy(world)
            view.occupied = world.occupied | guidance.blocked_moves.get(actor.id,set())
            if detour and actor.health > detour['health']:
                if actor.pos == detour['origin']:
                    self.detours.pop(actor.id)
                    continue
                route=distance_field(view,{detour['origin']},actor.pos,deadline)
                length=route.get(actor.pos)
                required=length+detour['work_steps']+policy.return_buffer if length is not None else None
                if required is None or required>clock.until_night:
                    self.diagnostic['plans'][actor.id]=dict(stage='resume_blocked',reason='route or construction deadline')
                    self.detours.pop(actor.id)
                    continue
                guidance.medical_detour_actors.add(actor.id)
                permission=guidance.treatment_view(actor.id)
                offers=[Candidate(actor.id,dict(action='move',targetPos=[pos_json(p)]),230-i*.01,
                                  'return from treatment to the budgeted construction start')
                        for i,p in enumerate(sorted(p for p in neighbours(actor.pos) if route.get(p,float('inf'))<length))]
                offers=[c for c in offers if permission.permit(c)]
                result.extend(offers)
                self.diagnostic['plans'][actor.id]=dict(stage='resume_construction',required_steps=required,
                                                       origin=detour['origin'])
                continue
            stock = policy.medical_stock_enabled and world.near_zone(actor.pos, 'weaponShop')
            if (not treatment and not stock) or actor.backpack is None:
                continue
            permission = guidance.treatment_view(actor.id) if treatment or stock else guidance
            if actor.inventory['Medicine']:
                if not treatment:
                    continue  # Keep one personally carried emergency dose.
                offers = [Candidate(actor.id, {'action': 'use', 'name': 'Medicine'},
                                    250+maximum-actor.health, 'treat observed low HP before optional daytime work')]
                offers = [c for c in offers if permission.permit(c)]
                result.extend(offers)
                if offers:
                    self.diagnostic['plans'][actor.id] = {'stage': 'heal'}
                continue
            if not treatment:
                from .wall_policy import investment_fund
                reserve=max(reserve,investment_fund(world)[0])
                budget=min(budget,max(0,(world.gold or 0)-reserve))
                # A healthy spare dose must not shrink an affordable checkout.
                # Compare before purchase: arbitration already charges any buy
                # in this frame, so do not add its full cost as a second floor.
                stock_budget = max(0, (world.gold or 0) -
                    max(reserve, getattr(world,'checkout_cash_reserve',0)) - optional_spent)
                if world.shop.get('Medicine',float('inf')) > stock_budget:
                    self.diagnostic['plans'][actor.id] = dict(stage='stock_deferred',reason='funded_checkout')
                    continue
            if actor.id in self.pending or actor.capacity is None or len(actor.backpack) >= actor.capacity:
                continue
            price = world.shop.get('Medicine')
            from .day_schedule import day_endpoints
            goals = ({guidance.operator_stands[actor.id]} if actor.id in guidance.operator_stands else
                     day_endpoints(world, actor, guidance.operator_stands)[0])
            job=(construction_jobs or {}).get(actor.id,{})
            if treatment and not detour and job.get('helper') and job.get('helper_collect'):
                steps=job.get('construction_steps')
                if isinstance(steps,(int,float)) and steps>=0:
                    detour=dict(day=clock.day,origin=actor.pos,work_steps=steps,health=actor.health)
            if detour:
                goals={detour['origin']}
            if (price is None or price <= 0 or price > budget or not goals or clock.day is None or
                    not 1 <= clock.day <= 10 or not world.zones.get('weaponShop')):
                continue
            start = distance_field(view, [actor.pos], actor.pos, deadline)
            back = distance_field(view, goals, actor.pos, deadline)
            shops = interaction_cells(view, world.zones['weaponShop'], actor.pos)
            actions = 2 if treatment else 1
            # An idle role may make a personal trip. The existing task, repair,
            # construction and return commitments still have to permit it.
            margin = policy.return_buffer + (8 if actor.id in guidance.operator_stands else 0)
            if detour:
                margin += detour['work_steps']
            paths = [(start[p]+back[p]+actions, start[p], p) for p in shops if p in start and p in back
                     and start[p]+back[p]+actions+margin <= clock.until_night]
            if not paths or time.monotonic() >= deadline:
                continue
            total, length, shop_cell = min(paths)
            route = distance_field(view, [shop_cell], actor.pos, deadline)
            if time.monotonic() >= deadline or route.get(actor.pos) != length:
                continue
            steps = sorted(p for p in neighbours(actor.pos) if p in route and route[p] < length)
            commands = ([{'action': 'buy', 'name': 'Medicine', 'num': 1}] if length == 0 else
                        [{'action': 'move', 'targetPos': [pos_json(p)]} for p in steps[:4]])
            offers = [Candidate(actor.id, command, 200+(maximum-actor.health)/(total+1)-i*.01,
                                ('complete low-HP treatment trip before optional daytime work' if treatment else
                                 'idle role obtains one personal emergency Medicine before returning'), gold_reserve=reserve)
                      for i, command in enumerate(commands)]
            if detour:
                guidance.medical_detour_actors.add(actor.id)
                permission=guidance.treatment_view(actor.id)
            offers = [c for c in offers if permission.permit(c)]
            if offers:
                result.extend(offers)
                optional_spent += price
                budget -= price  # Personal plans may not jointly overspend the daily allocation.
                self.diagnostic['plans'][actor.id] = {'stage': 'buy' if length == 0 else 'travel',
                    'shop_cell': shop_cell, 'travel': length, 'planned_actions': total, 'quote': price,
                    'purpose': 'treatment' if treatment else 'emergency_stock'}
                if length == 0:
                    self.offered[actor.id] = {'round': world.round, 'day': clock.day, 'price': price}
                if detour:
                    self.offered_detours[actor.id]=dict(plan=detour,commands=[c.command for c in offers])
                    self.diagnostic['plans'][actor.id].update(resume_origin=detour['origin'],
                        construction_steps=detour['work_steps'],required_with_construction=total+margin)
        if result:
            self.diagnostic['status'] = 'treatment_planned'
        return result

    def finalize(self, world, response):
        for identity,order in self.offered_detours.items():
            if response['roleCommandMap'].get(identity) in order['commands']:
                self.detours[identity]=dict(order['plan'])
        self.offered_detours={}
        for identity, order in self.offered.items():
            if response['roleCommandMap'].get(identity) == {'action': 'buy', 'name': 'Medicine', 'num': 1}:
                self.pending[identity] = dict(order)
                self.spent[order['day']] = self.spent.get(order['day'], 0)+order['price']
        self.offered = {}
