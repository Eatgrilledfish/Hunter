"""Personal guard demand shared by every checkout path; no future-wave guesses."""
from collections import Counter
from dataclasses import dataclass, field
from .protocol import distance

from .purchase_roles import ATTACK_ITEMS, attack_targets


def requirements(world, actor, policy):
    from .repair_decision import stock_target
    roster = getattr(world, 'night_roster', None)
    stock = [('Medicine', 1)]
    if roster is None or actor.id == roster.w:
        stock.insert(0, ('WallFixer', stock_target(world, policy)))
        if actor.health < 220: stock.reverse()
    if roster is None or actor.id == roster.p:
        stock += [(name, min(attack_targets(world)[name], actor.inventory[name]+getattr(world, 'stun_purchase_remaining', 2))) for name in ATTACK_ITEMS]
    return stock


@dataclass
class GuardStock:
    usage: dict = field(default_factory=dict)
    unserved: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    observed: int = -1
    stun_last: dict = field(default_factory=dict)
    bought: dict = field(default_factory=dict)
    buy_pending: dict | None = None

    def prepare(self, world, clock):
        world.stun_next_rounds={identity:number+5 for identity,number in self.stun_last.items()}
        if self.buy_pending and world.round > self.buy_pending['round']:
            order=self.buy_pending
            feedback=world.raw.get('lastRoundRoleActionResults',{})
            if (world.round==order['round']+1 and isinstance(feedback,dict)
                    and feedback.get(order['actor']) is False):
                self.bought[order['day']]=max(0,self.bought.get(order['day'],0)-order['num'])
            self.buy_pending=None
        world.stun_purchase_remaining=max(0,2-self.bought.get(clock.day,0))
        actor=world.ours.get(world.night_roster.p)
        if not actor or actor.backpack is None:return
        if self.observed!=world.round:
            for name,row in list(self.pending.items()):
                if world.round<=row['round']:continue
                carrier=world.ours.get(row['actor'])
                if (world.round==row['round']+1 and carrier and carrier.backpack is not None
                        and carrier.inventory[name]<row['before']):
                    self.usage.setdefault(row['day'],Counter())[name]+=1
                self.pending.pop(name,None)
            if clock.phases=={'night'}:
                # Count distinct nearby threats, not repeated missing-stock
                # frames. Unknown/out-of-range robots create no demand.
                assets=[u for u in world.ours.values() if u.alive and u.kind in {'worker','pioneer','station','wall'}]
                threats=[r for r in world.robots.values() if r.alive and r.abnormal!='dizzy'
                    and r.target_team in (None,world.side) and r.attack_range is not None
                    and r.attack_power is not None and r.attack_power>0
                    and any(distance(r.pos,q)<=r.attack_range for u in assets for q in u.cells)]
                for name in ATTACK_ITEMS:
                    if not actor.inventory[name] and threats:
                        self.unserved.setdefault(clock.day,{}).setdefault(name,set()).update(r.id for r in threats)
            self.observed=world.round
        previous=(clock.day or 0)-1
        used=self.usage.get(previous,{})
        missed=self.unserved.get(previous,{})
        targets=attack_targets(world)
        world.guard_attack_targets=targets
        world.guard_stock_report=dict(actor=actor.id,target=targets,
            owned={n:actor.inventory[n] for n in ATTACK_ITEMS},
            previous_night_used=dict(used),previous_night_unserved={n:len(v) for n,v in missed.items()},
            buy_remaining=world.stun_purchase_remaining,stun_next_round=world.stun_next_rounds.get(actor.id),
            demand_basis='day five onward: two stuns, no bombs; five-round use spacing')

    def finalize(self, world, clock, response):
        for identity,cmd in response['roleCommandMap'].items():
            if cmd.get('action')=='use' and cmd.get('name')=='DizzyWeapon':
                self.stun_last[identity]=world.round
        actor=world.ours.get(world.night_roster.p)
        command=response['roleCommandMap'].get(actor.id,{}) if actor else {}
        name=command.get('name')
        if actor and command.get('action')=='buy' and name=='DizzyWeapon':
            self.bought[clock.day]=self.bought.get(clock.day,0)+command.get('num',1)
            self.buy_pending=dict(actor=actor.id,day=clock.day,round=world.round,num=command.get('num',1))
        if actor and clock.phases=={'night'} and command.get('action')=='use' and name in ATTACK_ITEMS:
            self.pending[name]=dict(actor=actor.id,day=clock.day,round=world.round,before=actor.inventory[name])
