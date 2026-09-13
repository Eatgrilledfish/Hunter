"""Bounded personal repair stock, protected weapon funding, observed purchases."""
from dataclasses import dataclass, field
import time

from .arbitration import Candidate
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import pos_json
from .night_roles import defender_ids


def weapon_reserve(world, rules, deadline, excluded=()):
    """Match personal voucher counts to reachable remaining weapon upgrades.

    Higher-tier held vouchers cover stock demand, never confer an observed
    upgrade. Every missing prerequisite still reserves its own current quote.
    """
    world.repair_weapon_reserve_name = None
    guns = sorted(world.weapons, key=lambda u:u.id)
    if len(guns) != rules.weapon_limit or any(g.level not in (1,2,3) for g in guns):
        return None
    demand = [(level,g) for level in (1,2) for g in guns if g.level <= level]
    if not demand:
        return 0
    masks = []
    for actor in world.movers:
        if actor.id in excluded or actor.backpack is None:
            continue
        available = {level:min(len(demand),actor.inventory[f'WeaponUpgradeVoucher{level}']) for level in (1,2)}
        if not any(available.values()):
            continue
        reach = distance_field(world, [actor.pos], actor.pos, deadline)
        for level,count in available.items():
            mask = sum(1 << i for i,(tier,g) in enumerate(demand) if tier==level and
                       interaction_cells(world,[g.pos],actor.pos) & reach.keys())
            masks.extend([mask]*count)
        if time.monotonic() >= deadline:
            return None
    states = {0}
    for mask in masks:
        following = set(states)
        for used in states:
            free = mask & ~used
            while free:
                bit = free & -free
                following.add(used | bit)
                free -= bit
        states = following
    # Prefer coverage of the earlier tier when matchings have equal size.
    covered = max(states,key=lambda s:(s.bit_count(),tuple(bool(s & (1<<i)) for i in range(len(demand)))))
    for i,(level,_) in enumerate(demand):
        if not covered & (1<<i):
            world.repair_weapon_reserve_name = f'WeaponUpgradeVoucher{level}'
            return world.shop.get(world.repair_weapon_reserve_name)
    return 0


@dataclass
class RepairSupply:
    daily: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    offered: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)

    def reconcile(self, world, clock):
        feedback = world.raw.get('lastRoundRoleActionResults', {})
        feedback = feedback if isinstance(feedback,dict) else {}
        for identity,order in list(self.pending.items()):
            actor=world.ours.get(identity)
            observed=bool(actor and actor.backpack is not None and actor.inventory['WallFixer']>order['prior'])
            failed=world.round==order['round']+1 and feedback.get(identity) is False
            if failed and not observed:
                budget=self.daily[order['day']]
                budget['count']-=1;budget['spent']-=order['price']
            if failed or observed:
                self.pending.pop(identity)
        # Unresolved spends remain charged and block that buyer across days.
        self.daily=dict(sorted(self.daily.items())[-10:])

    def candidates(self, world, clock, rules, policy, deadline, guidance, excluded=()):
        self.offered={}
        used=self.daily.get(clock.day,dict(count=0,spent=0))
        self.diagnostic=dict(status='disabled',pending=sorted(self.pending),**used)
        if not policy.repair_supply_enabled or not getattr(world,'task_side_plan',None):
            return []
        if clock.phases!={'day'} or clock.day is None or not 1<=clock.day<=10:
            self.diagnostic['status']='no_daytime_window';return []
        reserve=weapon_reserve(world,rules,deadline,excluded)
        self.diagnostic.update(status='fund_or_quote_unavailable',weapon_reserve=reserve)
        price=world.shop.get('WallFixer')
        if reserve is None or price is None or price<0 or world.gold is None:
            return []
        budget=min(20-used['spent'],max(0,world.gold-reserve))
        slots=2-used['count']
        if price>budget or slots<=0:
            return []
        defender_ids(world)
        roster=world.night_roster
        result=[]
        from .defence_duties import enabled
        for identity in ((roster.w,) if enabled(world) else (roster.w,roster.p)):
            if time.monotonic()>=deadline or slots<=0 or price>budget:
                break
            actor=world.ours.get(identity)
            if not actor or not actor.alive or identity in excluded or identity in self.pending:
                continue
            if (actor.backpack is None or actor.capacity is None or len(actor.backpack)>=actor.capacity
                    or actor.inventory['WallFixer']>=1 or actor.health<(220 if actor.kind=='worker' else 200)*.75):
                continue
            if identity in guidance.urgent_upgrades or identity in guidance.recovery_actions or identity in guidance.site_clear_actions:
                continue
            # W stocks a package on an existing shop visit. P may make a
            # measured daytime trip while no task is active/pending.
            if not enabled(world) and identity==roster.w and not world.near_zone(actor.pos,'weaponShop'):
                continue
            home=guidance.operator_stands.get(identity)
            if home is None:
                continue
            start=distance_field(world,[actor.pos],actor.pos,deadline)
            back=distance_field(world,[home],actor.pos,deadline)
            shops=interaction_cells(world,world.zones.get('weaponShop',()),actor.pos)
            paths=[(start[q]+1+back[q],start[q],q) for q in shops & start.keys() & back.keys()
                   if start[q]+1+back[q]+policy.return_buffer<=clock.until_night]
            if not paths or time.monotonic()>=deadline:
                continue
            total,length,stand=min(paths)
            route=distance_field(world,[stand],actor.pos,deadline)
            if time.monotonic()>=deadline or route.get(actor.pos)!=length:
                continue
            commands=([dict(action='buy',name='WallFixer',num=1)] if length==0 else
                      [dict(action='move',targetPos=[pos_json(q)]) for q in sorted(neighbours(actor.pos))
                       if q in route and route[q]<length][:4])
            offers=[Candidate(identity,command,120-i*.01,'personal repair stock within daily quota',gold_reserve=reserve,
                              gold_reserve_item=world.repair_weapon_reserve_name)
                    for i,command in enumerate(commands)]
            # Return deadlines and triage apply before this optional stock trip
            # gets priority over ordinary wall/base procurement.
            if guidance.return_routes.get(identity,{}).get('due') or not offers:
                continue
            result.extend(offers);budget-=price;slots-=1
            self.diagnostic.setdefault('buyers',{})[identity]=dict(price=price,travel=length,total_actions=total)
            if length==0:
                self.offered[identity]=dict(day=clock.day,round=world.round,price=price,prior=actor.inventory['WallFixer'])
        self.diagnostic['status']='offered' if result else 'no_personal_stock_gap_with_feasible_route'
        return result

    def finalize(self, world, response):
        for identity,order in self.offered.items():
            if response['roleCommandMap'].get(identity)==dict(action='buy',name='WallFixer',num=1):
                self.pending[identity]=dict(order)
                budget=self.daily.setdefault(order['day'],dict(count=0,spent=0))
                budget['count']+=1;budget['spent']+=order['price']
        self.offered={}
