"""Personal guard demand shared by every checkout path; no future-wave guesses."""
from collections import Counter
from dataclasses import dataclass, field
from .protocol import distance

ATTACK_ITEMS=('DizzyWeapon','Bomb')


def requirements(world, actor, policy):
    from .repair_decision import stock_target
    stock=[('WallFixer',stock_target(world,policy)),('Medicine',1)]
    if actor.health<220:stock.reverse()
    return stock+[(name,getattr(world,'guard_attack_targets',{}).get(name,1)) for name in ATTACK_ITEMS]


@dataclass
class GuardStock:
    usage: dict = field(default_factory=dict)
    unserved: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    observed: int = -1

    def prepare(self, world, clock):
        actor=world.ours.get(world.night_roster.w)
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
        # A bounded policy target; bag and observed cash still constrain buys.
        targets={name:min(4,max(1,used.get(name,0)+(len(missed.get(name,()))+2)//3)) for name in ATTACK_ITEMS}
        world.guard_attack_targets=targets
        world.guard_stock_report=dict(actor=actor.id,target=targets,
            owned={n:actor.inventory[n] for n in ATTACK_ITEMS},
            previous_night_used=dict(used),previous_night_unserved={n:len(v) for n,v in missed.items()},
            demand_basis='confirmed consumption plus distinct in-range threats while out of stock')

    def finalize(self, world, clock, response):
        actor=world.ours.get(world.night_roster.w)
        command=response['roleCommandMap'].get(actor.id,{}) if actor else {}
        name=command.get('name')
        if actor and clock.phases=={'night'} and command.get('action')=='use' and name in ATTACK_ITEMS:
            self.pending[name]=dict(actor=actor.id,day=clock.day,round=world.round,before=actor.inventory[name])
