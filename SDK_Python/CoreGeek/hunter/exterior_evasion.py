"""Escape actual attack areas, executing one step then replanning from feedback."""
import heapq
import time
from .arbitration import Candidate
from .navigation import neighbours
from .protocol import distance, pos_json
from .rules import station_rings
from .robot_threats import active


def propose(world, clock, deadline, trapped=False):
    if clock.phases != {'night'} or len(world.stations) != 1:
        return [], {'status': 'inactive'}
    actor = world.ours.get(world.night_roster.m)
    if not actor or not actor.alive or actor.id in world.night_defenders:
        return [], {'status': 'not an exterior economist'}
    blue, yellow = station_rings(world.stations[0].pos)
    interior = blue | yellow | world.stations[0].cells
    if actor.pos in interior:
        return [], {'status': 'interior transit handled by roster'}
    threats = active(world)
    known = [r for r in threats if r.attack_range is not None and r.attack_power is not None]
    unknown = [r for r in threats if r not in known]
    # Unknown power does not create an infinite range. Unknown kinds remain
    # unknown; do not invent a radius or claim a survivable route through them.
    def uncertain(q):
        return any(r.attack_range is not None and distance(q, r.pos) <= r.attack_range for r in unknown)
    def damage(q):
        return 2 * sum(r.attack_power for r in known if distance(q, r.pos) <= r.attack_range)
    current = damage(actor.pos)
    report = {'known_damage_before': current, 'unknown_sources': len(unknown),
              'nearest': min((distance(actor.pos,r.pos) for r in threats),default=None),
              'in_range': sum(distance(actor.pos,r.pos)<=r.attack_range for r in threats if r.attack_range is not None)}
    plan = getattr(world, 'task_side_plan', None)
    goals = ({q for q in neighbours(plan['gate']) if world.inside(q) and q not in interior
              and q not in world.occupied and not damage(q) and not uncertain(q)}
             if trapped and plan and not current else None)
    if not current and not uncertain(actor.pos) and not goals:
        return [], dict(report, status='no observed need to retreat')
    blocked = world.occupied | interior
    # This is a risk policy, not an assertion about attack cadence. Two attack
    # opportunities per tile; whole-route loss <= 25% of current HP. Search
    # loss first, then distance: a zero-loss detour always wins over taking hits.
    budget = max(0, actor.health * .25)
    queue = [(0, 0, actor.pos, ())]
    best = {actor.pos: (0, 0)}
    route = None
    while queue:
        if time.monotonic() >= deadline:
            return [], dict(report, status='budget exhausted')
        loss, steps, origin, path = heapq.heappop(queue)
        if best.get(origin) != (loss, steps):
            continue
        if path and damage(origin) == 0 and not uncertain(origin) and (goals is None or origin in goals):
            route = path
            break
        for point in neighbours(origin):
            if (not world.inside(point) or point in blocked or uncertain(point)
                    or point in world.navigation_avoided.get(origin, set())):
                continue
            cost = (loss + damage(point), steps + 1)
            if cost[0] > budget or cost >= best.get(point, (float('inf'), 0)):
                continue
            best[point] = cost
            heapq.heappush(queue, (*cost, point, path + (point,)))
    if route is None:
        return [], dict(report, status='no improving legal exterior route', damage_budget=budget)
    point = route[0]
    report.update(status='retreat', actor=actor.id, **{'from':actor.pos,'to':point},
                  known_damage_after=damage(point), observed_route=route,
                  route_end_damage=0, route_damage_bound=loss, damage_budget=budget,
                  no_observed_exposure=not unknown and damage(point)==0)
    return [Candidate(actor.id, {'action':'move','targetPos':[pos_json(point)]}, 1200,
                      'observed exterior economic-worker retreat')], report
