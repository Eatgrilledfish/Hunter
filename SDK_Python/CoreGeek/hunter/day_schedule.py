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
from .day_division import DayDivision
from .night_roles import defender_ids, economic_endpoints


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


def day_endpoints(world, actor, stands):
    """Use actual duty destinations even when M has no gun assignment."""
    if actor.id not in defender_ids(world):
        return economic_endpoints(world, actor), 'exterior'
    return ({stands[actor.id]} if actor.id in stands else set()), 'defence'


@dataclass
class DaySchedule:
    active: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)
    day: int | None = None
    division: DayDivision = field(default_factory=DayDivision)

    def funded_delivery(self, world, clock, policy, jobs, stands, plans, deadline):
        """Finish an actually affordable weapon circuit using its own route.

        Ordinary harvesting keeps the conservative macro buffer. A funded
        delivery may use that buffer if buy -> use -> assigned stand plus a
        small explicit traffic/closure margin still fits before night.
        """
        actions=[];budgets={}
        if not policy.upgrade_commitment_enabled or clock.phases != {'day'}:return actions,budgets
        for identity,plan in plans.items():
            if not plan['name'].startswith('WeaponUpgradeVoucher'):continue
            actor=world.ours[identity];job=jobs.get(identity,{})
            if (getattr(world,'economy_first',False) and plan['stage']=='procure'
                    and self.active.get(identity,'harvest') not in ('cashout','shop','home')):continue
            if actor.kind!='worker' or (job and not job.get('gate')) or world.seal_cells:continue
            endpoint_goals, endpoint = day_endpoints(world, actor, stands)
            home=distance_field(world,endpoint_goals,actor.pos,deadline)
            target=world.ours.get(plan['target'])
            if target is None:continue
            ends=interaction_cells(world,[target.pos],actor.pos)
            deliver=weighted_field(world,{p:home[p]+1 for p in ends if p in home},actor,deadline)
            if deliver is None:continue
            if plan['stage']=='deliver':route=deliver
            else:
                if (world.gold or 0)<world.shop.get(plan['name'],float('inf')):continue
                shops=interaction_cells(world,world.zones.get('weaponShop',()),actor.pos)
                route=weighted_field(world,{p:deliver[p]+1 for p in shops if p in deliver},actor,deadline)
            if route is None or actor.pos not in route or time.monotonic()>=deadline:continue
            margin=2+(1 if job.get('gate') else 0)
            required=route[actor.pos]+margin
            if required>clock.until_night:continue
            # Reuse legal purchase/use candidates and their shared-gold checks;
            # route toward a feasible interaction cell, not back via the vendor.
            direct=[c for c in plan['candidates'] if c.command['action'] in {'buy','use'}]
            if direct:
                direct_cost=(home.get(actor.pos,float('inf'))+1 if plan['stage']=='deliver'
                             else deliver.get(actor.pos,float('inf'))+1)+margin
                if direct_cost>clock.until_night:direct=[]
                else:required=direct_cost
            selected=direct or self.moves(actor,route,'funded weapon circuit fits buy, use and return deadline')
            if selected:
                actions.extend(selected);budgets[identity]={'required':required,'left':clock.until_night,
                    'margin':margin,'target':target.id,'stage':plan['stage'],
                    'endpoint':endpoint,'endpoints':sorted(endpoint_goals)}
        return actions,budgets

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
            if job.get('economy_first'):
                if actor.inventory['stone'] < reserve or not job.get('defer_build'):continue
            elif job and (not job.get('gate') or actor.inventory['stone']<reserve and self.active.get(actor.id,'harvest')=='harvest'):continue
            endpoint_goals, endpoint = day_endpoints(world, actor, stands)
            home=distance_field(world,endpoint_goals,actor.pos,deadline)
            if actor.pos not in home:continue
            if job.get('economy_first'):
                entry, tail = job.get('construction_entry'), job.get('construction_tail')
                if entry is None or tail is None:continue
                home = weighted_field(world,{entry:tail},actor,deadline)
                if home is None or actor.pos not in home:continue
            free={k:max(0,actor.inventory[k]-(reserve if k=='stone' else 0)) for k in MINERALS}
            kinds=sum(n>0 for n in free.values())
            sale_steps=max(1,kinds)
            vendors=interaction_cells(world,world.zones.get('vendor',()),actor.pos)
            shops=interaction_cells(world,world.zones.get('weaponShop',()),actor.pos)
            # Reserve the shop detour only against observed gold and currently
            # carried sellable stock, not hypothetical future mining income.
            liquidation = world.gold or 0
            for worker in world.movers:
                if worker.kind != 'worker':continue
                held_reserve = jobs.get(worker.id,{}).get('stock_target',0)
                liquidation += sum(max(0,worker.inventory[k]-(held_reserve if k=='stone' else 0))*world.vendor.get(k,0) for k in MINERALS)
            prices = [world.shop['WeaponUpgradeVoucher'+str(u.level)] for u in world.weapons
                      if u.level in (1,2) and 'WeaponUpgradeVoucher'+str(u.level) in world.shop]
            if getattr(world, "staged_walls", False):
                from .wall_policy import priority_units
                prices = [world.shop[name] for u in priority_units(world) if u.level in (1,2)
                          if (name := ("Wall" if u.kind == "wall" else "Station" if u.kind == "station" else "Weapon")
                              + "UpgradeVoucher" + str(u.level)) in world.shop]
            need_shop=bool(prices and liquidation >= min(prices))
            if need_shop:
                # Buying and applying one voucher are two separate actions.
                via_shop=weighted_field(world,{p:home[p]+2 for p in shops if p in home},actor,deadline)
            else:via_shop=home
            if via_shop is None:continue
            checkout=weighted_field(world,{p:via_shop[p]+sale_steps for p in vendors if p in via_shop},actor,deadline)
            if checkout is None or actor.pos not in checkout:continue
            margin=3 if getattr(world,'economy_first',False) else policy.return_buffer+(8 if world.defence_cells and endpoint == 'defence' else 0)
            left=clock.until_night
            slack=left-checkout[actor.pos]-margin
            stage=self.active.get(actor.id,'harvest')
            if stage=='construction' and job.get('defer_build'):stage='harvest'
            if stage=='cashout' and not kinds:stage='shop'
            if stage in ('shop','done') and not upgrades.get(actor.id):
                stage='harvest'  # No affordable purchase: use remaining feasible mining time.
            personal=upgrades.get(actor.id)
            if personal and personal['stage']=='deliver':stage='home'
            selected=[]
            if stage in ('shop','home'):
                if personal:
                    fits=True
                    if getattr(world,'economy_first',False):
                        target=world.ours.get(personal['target'])
                        ends=interaction_cells(world,[target.pos],actor.pos) if target else set()
                        delivery=weighted_field(world,{p:home[p]+1 for p in ends if p in home},actor,deadline)
                        purchase=(weighted_field(world,{p:delivery[p]+1 for p in shops if p in delivery},actor,deadline)
                                  if delivery is not None and personal['stage']=='procure' else delivery)
                        fits=purchase is not None and purchase.get(actor.pos,float('inf'))+margin<=left
                    selected=list(personal['candidates']) if fits else self.moves(actor,home,'upgrade circuit no longer fits: return before night')
                elif home.get(actor.pos,0)>0:
                    selected=self.moves(actor,home,'cash circuit: return to assigned duty endpoint')
                else:stage='done'
            if stage=='harvest':
                start=distance_field(world,[actor.pos],actor.pos,deadline)
                options=[]
                for mineral in sorted(MINERALS):
                    price=world.vendor.get(mineral,0)
                    if price<=0:continue
                    mine_checkout=checkout
                    if (getattr(world,'economy_first',False) and not need_shop and prices
                            and liquidation+price>=min(prices)):
                        future_shop=weighted_field(world,{p:home[p]+2 for p in shops if p in home},actor,deadline)
                        if future_shop is None:continue
                        mine_checkout=weighted_field(world,{p:future_shop[p]+sale_steps for p in vendors if p in future_shop},actor,deadline)
                        if mine_checkout is None:continue
                    for mine in sorted(world.zones.get(mineral,())):
                        goals=interaction_cells(world,[mine],actor.pos)
                        possible=[p for p in goals if p in start and p in mine_checkout
                                  and start[p]+1+mine_checkout[p]+margin+(not free[mineral] and kinds>0)<=left]
                        if not possible:continue
                        point=min(possible,key=lambda p:(start[p],mine_checkout[p],p))
                        rank = -price if getattr(world,'economy_first',False) else -price/(start[point]+1)
                        options.append((rank,(mineral,mine) in claimed,start[point],mineral,mine,point))
                if len(actor.backpack)>=actor.capacity or slack<=1 or not options:
                    if kinds:stage='cashout'
                    elif job.get('economy_first'):
                        job['defer_build']=False
                        self.division.building=True
                        stage='construction'
                    elif getattr(world,'economy_first',False):
                        stage='home'
                        selected=self.moves(actor,home,'no remaining feasible harvest: return and release market corridor')
                elif options:
                    _,_,length,mineral,mine,point=min(options);claimed.add((mineral,mine))
                    if length==0:
                        selected=[Candidate(actor.id,{'action':'collect','targetPos':[pos_json(mine)]},20,
                                            'harvest until latest feasible cash circuit departure')]
                    else:
                        path=distance_field(world,[point],actor.pos,deadline)
                        selected=self.moves(actor,path,'harvest route fits vendor, shop and home deadline')
            if (stage=='cashout' and getattr(world,'economy_first',False)
                    and checkout[actor.pos]+margin>left):
                if job.get('economy_first'):
                    job['defer_build']=False
                    self.division.building=True
                    stage='construction';selected=[]
                else:
                    stage='home'
                    selected=self.moves(actor,home,'cash circuit no longer fits: return before night')
            if stage=='cashout':
                if world.near_zone(actor.pos,'vendor'):
                    mineral=max(free,key=lambda k:(free[k]*world.vendor.get(k,0),k))
                    if free[mineral]:selected=[Candidate(actor.id,{'action':'sell','name':mineral,'num':free[mineral]},30,
                                                        'cash circuit: sell personal batch, preserve gate stone')]
                else:selected=self.moves(actor,checkout,'cash circuit: latest departure for vendor then shop')
            self.active[actor.id]=stage
            self.diagnostic[actor.id]={'stage':stage,'checkout_steps':checkout[actor.pos],
                'endpoint':endpoint,'endpoints':sorted(endpoint_goals),
                'margin':margin,'harvest_left':max(0,slack-1),'leave_by':world.round+max(0,slack-1),
                'construction_steps':job.get('construction_steps'), 'role':'stone' if job.get('economy_first') else 'cash',
                'shop_reserved':need_shop,'liquidation':liquidation,'upgrade_price':min(prices) if prices else None}
            result.extend(selected)
        return result

    @staticmethod
    def moves(actor, path, reason):
        length=path.get(actor.pos)
        if length is None:return []
        return [Candidate(actor.id,{'action':'move','targetPos':[pos_json(p)]},30-i*.01,reason)
                for i,p in enumerate(sorted(p for p in neighbours(actor.pos) if path.get(p,float('inf'))<length)[:4])]
