"""Shared observed exposure bounds, never a prediction of robot target choice."""
from .protocol import distance
from .robot_threats import active


def evidence(world, actor, position):
    threats=[r for r in active(world) if r.target_team in (None,world.side)]
    unknown=any(r.attack_range is None or r.attack_power is None for r in threats)
    upper=sum(2*r.attack_power for r in threats if r.attack_range is not None
              and r.attack_power is not None and distance(position,r.pos)<=r.attack_range)
    injury=getattr(world,'persistent_mover_injuries',{}).get(actor.id,{})
    return dict(two_opportunity_upper=None if unknown else upper,
                observed_injury=injury.get('damage'),unknown_attack=unknown,
                lethal=unknown or upper>=actor.health)
