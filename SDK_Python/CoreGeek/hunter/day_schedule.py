"""Rolling harvest -> vendor -> shop -> home budgets, using observed paths.

No future income is spendable. Timings include interaction actions and a margin;
new obstacles, prices, inventory and mine disappearance are observed each turn.
"""
from dataclasses import dataclass, field
from heapq import heapify, heappop, heappush
import time
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import MINERALS, pos_json
from .arbitration import Candidate


def weighted_field(world, costs, actor, deadline):
    blocked = (world.occupied | world.navigation_avoided.get(actor.pos,set()))-{actor.pos}
    best = {p:v for p,v in costs.items() if world.inside(p) and p not in blocked}
    queue = [(v,p) for p,v in best.items()];heapify(queue)
    while queue:
        if time.monotonic() >= deadline:
            return None  # Never treat a partially explored budget as complete.
        value,point=heappop(queue)
        if best[point]!=value:continue
        for p in neighbours(point):
            if world.inside(p) and p not in blocked and value+1<best.get(p,float('inf')):
                best[p]=value+1;heappush(queue,(value+1,p))
    return best


@dataclass
class DaySchedule:
    active: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)
    day: int | None = None

    def candidates(self, world, clock, policy, jobs, stands, upgrades, excluded, deadline):
        self.diagnostic={}
        if clock.day!=self.day:
            self.active.clear();self.day=clock.day
        if not getattr(world,'timed_economy',False) or clock.phases!={'day'}:
            return []
        result=[];claimed=set()
        for actor in sorted(world.movers,key=lambda u:u.id):
            if actor.kind!='worker' or actor.id in excluded or actor.backpack is None or actor.capacity is None:
                continue
            job=jobs.get(actor.id,{})
            reserve=job.get('stock_target',0)
            if job.get('gate') and world.seal_cells:
                continue  # The final closure must not wait behind an optional cash circuit.
            if job and (not job.get('gate') or actor.inventory['stone']<reserve and self.active.get(actor.id,'harvest')=='harvest'):continue
            if actor.id not in stands:continue
            home=distance_field(world,[stands[actor.id]],actor.pos,deadline)
            if actor.pos not in home:continue
            free={k:max(0,actor.inventory[k]-(reserve if k=='stone' else 0)) for k in MINERALS}
            kinds=sum(n>0 for n in free.values())
            sale_steps=max(1,kinds)
            vendors=interaction_cells(world,world.zones.get('vendor',()),actor.pos)
            shops=interaction_cells(world,world.zones.get('weaponShop',()),actor.pos)
            need_shop=any(u.level in (1,2) and 'WeaponUpgradeVoucher'+str(u.level) in world.shop for u in world.weapons)
            if need_shop:
                # Buying and applying one voucher are two separate actions.
                via_shop=weighted_field(world,{p:home[p]+2 for p in shops if p in home},actor,deadline)
            else:via_shop=home
            if via_shop is None:continue
            checkout=weighted_field(world,{p:via_shop[p]+sale_steps for p in vendors if p in via_shop},actor,deadline)
            if checkout is None or actor.pos not in checkout:continue
            margin=policy.return_buffer+(8 if world.defence_cells else 0)
            left=clock.until_night
            slack=left-checkout[actor.pos]-margin
            stage=self.active.get(actor.id,'harvest')
            if stage=='cashout' and not kinds:stage='shop'
            personal=upgrades.get(actor.id)
            if personal and personal['stage']=='deliver':stage='home'
            selected=[]
            if stage in ('shop','home'):
                if personal:
                    selected=list(personal['candidates'])
                elif home.get(actor.pos,0)>0:
                    selected=self.moves(actor,home,'cash circuit: return to assigned gun')
                else:stage='done'
            if stage=='harvest':
                start=distance_field(world,[actor.pos],actor.pos,deadline)
                options=[]
                for mineral in sorted(MINERALS):
                    price=world.vendor.get(mineral,0)
                    if price<=0:continue
                    for mine in sorted(world.zones.get(mineral,())):
                        goals=interaction_cells(world,[mine],actor.pos)
                        possible=[p for p in goals if p in start and p in checkout
                                  and start[p]+1+checkout[p]+margin+(not free[mineral] and kinds>0)<=left]
                        if not possible:continue
                        point=min(possible,key=lambda p:(start[p],checkout[p],p))
                        options.append(((mineral,mine) in claimed,-price/(start[point]+1),start[point],mineral,mine,point))
                if len(actor.backpack)>=actor.capacity or slack<=1 or not options:
                    if kinds:stage='cashout'
                elif options:
                    _,_,length,mineral,mine,point=min(options);claimed.add((mineral,mine))
                    if length==0:
                        selected=[Candidate(actor.id,{'action':'collect','targetPos':[pos_json(mine)]},20,
                                            'harvest until latest feasible cash circuit departure')]
                    else:
                        path=distance_field(world,[point],actor.pos,deadline)
                        selected=self.moves(actor,path,'harvest route fits vendor, shop and home deadline')
            if stage=='cashout':
                if world.near_zone(actor.pos,'vendor'):
                    mineral=max(free,key=lambda k:(free[k]*world.vendor.get(k,0),k))
                    if free[mineral]:selected=[Candidate(actor.id,{'action':'sell','name':mineral,'num':free[mineral]},30,
                                                        'cash circuit: sell personal batch, preserve gate stone')]
                else:selected=self.moves(actor,checkout,'cash circuit: latest departure for vendor then shop')
            self.active[actor.id]=stage
            self.diagnostic[actor.id]={'stage':stage,'checkout_steps':checkout[actor.pos],
                'margin':margin,'harvest_left':max(0,slack-1),'leave_by':world.round+max(0,slack-1),
                'shop_reserved':need_shop}
            result.extend(selected)
        return result

    @staticmethod
    def moves(actor, path, reason):
        length=path.get(actor.pos)
        if length is None:return []
        return [Candidate(actor.id,{'action':'move','targetPos':[pos_json(p)]},30-i*.01,reason)
                for i,p in enumerate(sorted(p for p in neighbours(actor.pos) if path.get(p,float('inf'))<length)[:4])]
