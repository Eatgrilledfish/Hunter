"""Shared observed exposure bounds, never a prediction of robot target choice."""
from .protocol import distance
from .robot_threats import active


def maintenance_worker(world, actor):
    from .rear_open import enabled
    clock=getattr(world,'strategy_clock',None) or getattr(world,'night_clock',None)
    return bool(enabled(world) and clock and clock.phases=={'night'}
                and actor.id==getattr(getattr(world,'night_roster',None),'w',None))


def evidence(world, actor, position):
    threats=[r for r in active(world) if r.target_team in (None,world.side)]
    unknown=any(r.attack_range is None or r.attack_power is None for r in threats)
    upper=sum(2*r.attack_power for r in threats if r.attack_range is not None
              and r.attack_power is not None and distance(position,r.pos)<=r.attack_range)
    injury=getattr(world,'persistent_mover_injuries',{}).get(actor.id,{})
    measured=maintenance_worker(world,actor)
    loss=getattr(world,'observed_mover_losses',{}).get(actor.id,0)
    recent=getattr(world,'recent_mover_injuries',{}).get(actor.id,{})
    if (measured and recent.get('position')==actor.pos
            and 0<=world.round-recent.get('round',-99)<=2):
        loss=max(loss,recent.get('loss',0))
    nearby=any(r.attack_range is None or distance(actor.pos,r.pos)<=r.attack_range for r in threats)
    withdraw=bool(measured and loss>0 and loss>=actor.health and nearby)
    # W stays at work until actual personal damage threatens another lethal
    # hit. Crowding, unknown stats and an accumulated range sum are not hits.
    lethal=(withdraw and any(r.attack_range is None or distance(position,r.pos)<=r.attack_range
                            for r in threats)) if measured else unknown or upper>=actor.health
    return dict(two_opportunity_upper=None if unknown else upper,
                observed_injury=injury.get('damage'),unknown_attack=unknown,
                actual_recent_loss=loss,withdraw=withdraw,measured_maintenance=measured,
                basis='actual personal loss' if measured else 'range upper bound',lethal=lethal)
