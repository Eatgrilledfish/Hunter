"""Reserve consumables when a validated, exclusive ready gun can do useful work.

The two-opportunity exposure check is a conservative local safety bound, not
an assumption about a monster's chosen target or its future attack cadence.
"""
from .protocol import distance
from .robot_targets import opposing


def deferred_bomb_actors(world, checked):
    threats=[r for r in world.robots.values() if r.alive and not opposing(world,r)]
    assets=[u for u in world.ours.values() if u.alive]
    if not threats or not assets:
        return set()
    if any(r.attack_power is None or r.attack_range is None for r in threats):
        return set()
    for asset in assets:
        cells=getattr(asset,'cells',None) or (asset.pos,)
        exposure=2*sum(r.attack_power for r in threats
                       if min(distance(r.pos,p) for p in cells)<=r.attack_range)
        if asset.health is None or exposure>=asset.health:
            return set()
    controllers={}
    for candidate,_ in checked:
        if candidate.command.get('action')=='attack':
            controllers.setdefault(candidate.actor,set()).add(candidate.command.get('controllerId'))
    return {candidate.command['controllerId'] for candidate,_ in checked
            if candidate.command.get('action')=='attack'
            and len(controllers[candidate.actor])==1
            and any(amount>0 for amount in candidate.damage.values())}
