"""Observed guard capacity and personal repair coverage before night foraging."""
import time
import math
from .navigation import neighbours
from .protocol import distance
from .rules import station_rings
from .task_side_layout import _field, BudgetExpired
from .repair_plan import pressure


def attack_key(identity, command):
    points = command.get('targetPos', ())
    valid = isinstance(points, (list, tuple)) and all(
        isinstance(p, dict) and type(p.get('x')) is int and type(p.get('y')) is int for p in points)
    return (str(identity), str(command.get('controllerId')),
            tuple((p['x'], p['y']) for p in points) if valid else ())


def prepare_fire(world, checked, examined, candidates, deadline):
    """Index only current, validated fire proposals; never generate targets here.

    Four availability masks cover the two actual guards. At most three guns
    with eight options each give 153 fixed-duty matchings, including waiting.
    Precomputed non-dominated effects avoid enumerating matchings in the beam.
    """
    source = getattr(world, 'combat_fire_observation', {})
    signature = (id(source), tuple(sorted(attack_key(c.actor, c.command) for c, _ in checked
                                         if c.command.get('action') == 'attack')),
                 tuple(sorted(examined)), tuple(sorted(attack_key(c.actor, c.command) for c in candidates
                                                       if c.command.get('action') == 'attack')))
    previous = getattr(world, 'forage_fire', {})
    if previous.get('round') == world.round and previous.get('complete') and previous.get('signature') == signature:
        return  # A checked current-state incumbent remains usable at expiry.
    result = {'round': world.round, 'complete': False, 'reason': 'combat proposal incomplete', 'signature': signature}
    world.forage_fire = result
    if source.get('round') != world.round or not source.get('complete'):
        return
    if time.monotonic() >= deadline:
        result['reason'] = 'fire capacity budget exhausted'
        return
    effects = source['effects']
    offered = {attack_key(c.actor, c.command) for c in candidates
               if c.command.get('action') == 'attack'} & effects.keys()
    if offered - examined:
        result['reason'] = 'fire proposals exceed checked candidate budget'
        return
    roster = world.night_roster
    identities = (roster.w, roster.p)
    robots = tuple(sorted(r.id for r in world.robots.values() if r.alive))
    health = tuple(world.robots[i].health for i in robots)
    options = {identity: [] for identity in identities}
    known = {}
    for candidate, resource in checked:
        if candidate.command.get('action') != 'attack':
            continue
        key = attack_key(candidate.actor, candidate.command)
        if key not in effects or key[1] not in options:
            continue
        damage = effects[key]
        if (not damage or any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
                              for v in damage.values())):
            result['reason'] = 'legal shot damage is unknown'
            return
        vector = tuple(damage.get(i, 0) for i in robots)
        if not any(vector):
            continue
        known[key] = vector
        options[key[1]].append((key, vector, resource.locks))
    if len(options[roster.w]) > 16 or len(options[roster.p]) > 8 or len(known) > 24:
        result['reason'] = 'fire capacity option bound exceeded'
        return
    profiles = {}
    zero = (0,) * len(robots)
    for mask in range(4):
        rows = [[None] + ([] if mask & (1 << i) else options[identity])
                for i, identity in enumerate(identities)]
        vectors, capacity = {zero}, 0
        for first in rows[0]:
            for second in rows[1]:
                if time.monotonic() >= deadline:
                    result['reason'] = 'fire capacity budget exhausted'
                    return
                if first and second and first[2] & second[2]:
                    continue
                shots = [row for row in (first, second) if row]
                capacity = max(capacity, len(shots))
                vectors.add(tuple(min(hp, sum(row[1][j] for row in shots))
                                  for j, hp in enumerate(health)))
        frontier = []
        # Larger sums first: an equal-sum, different vector cannot dominate.
        for vector in sorted(vectors, key=lambda row: (-sum(row), row)):
            if time.monotonic() >= deadline:
                result['reason'] = 'fire capacity budget exhausted'
                return
            if not any(all(a >= b for a, b in zip(other, vector)) for other in frontier):
                frontier.append(vector)
        profiles[mask] = {'capacity': capacity, 'frontier': frozenset(frontier)}
    result.update(complete=True, reason='validated current fire matchings',
                  identities=identities, robots=robots, health=health, effects=known, profiles=profiles)


def fire_allowed(world, commands, complete=True):
    report = getattr(world, 'forage_fire', {})
    if report.get('round') != world.round or not report.get('complete'):
        return False
    if not complete:
        return True  # A later beam candidate may still supply the actual shot.
    mask = sum(1 << i for i, identity in enumerate(report['identities']) if identity in commands)
    damage = [0] * len(report['robots'])
    used = set()
    for identity, command in commands.items():
        if command.get('action') != 'attack':
            continue
        key = attack_key(identity, command)
        if key not in report['effects'] or key[1] in used or key[1] in commands:
            return False
        used.add(key[1])
        for i, value in enumerate(report['effects'][key]):
            damage[i] += value
    vector = tuple(min(hp, value) for hp, value in zip(report['health'], damage))
    return vector in report['profiles'][mask]['frontier']


def fire_summary(world, response):
    report = getattr(world, 'forage_fire', {})
    result = {k: report.get(k) for k in ('round', 'complete', 'reason')}
    if report.get('complete'):
        commands = response['roleCommandMap']
        result.update(available=report['profiles'][0]['capacity'],
                      without_w=report['profiles'][1]['capacity'],
                      without_p=report['profiles'][2]['capacity'],
                      selected=sum(c.get('action') == 'attack' for c in commands.values()),
                      preserves_useful_fire=fire_allowed(world, commands))
    return result


def service_valid(world, identity):
    """A current feasible proposal, never evidence that its action succeeded."""
    row=getattr(world,'repair_service',{}).get(identity)
    actor=world.ours.get(identity)
    if not row or not actor or not actor.alive:
        return False
    guns=row.get('served_weapon')
    guns=(guns,) if isinstance(guns,str) else tuple(guns or ())
    observed=[world.ours.get(i) for i in guns]
    return bool(row.get('round')==world.round and row.get('observed_position')==actor.pos
                and identity in world.night_defenders and observed
                and all(g and g.alive and g.cooldown is not None for g in observed)
                and row.get('observed_cooldown')==min(g.cooldown for g in observed)
                and type(row.get('remaining_actions')) is int
                and 0<=row['remaining_actions']<=row['observed_cooldown']
                and not row.get('delayed_fire') and row.get('allowed_commands'))


def pioneer_service_available(world):
    plan=world.task_side_plan;roster=world.night_roster
    p=world.ours.get(roster.p)
    if not p or not p.alive or p.id not in world.night_defenders:
        return False
    if world.phase_task:
        task=getattr(world,'forage_task',None)
        return bool(task and p.pos in plan['c_stands']
                    and any(distance(p.pos,q)<=1 for q in task.cells))
    if p.pos in plan['c_stands']:
        return True
    return bool(service_valid(world,p.id)
                and world.repair_service[p.id].get('continuing_selected_repair'))


def contract(world, actor, command, admission):
    return {'actor':actor,'command':command,'task_commands':getattr(world,'forage_task_commands',[]),'critical':[
        row for row in admission.get('walls',()) if row['critical']]}


def bundle_allowed(world, bundle, *, complete=True, require_service=False):
    """Validate a whole action set, allowing unfinished dependencies in a beam.

    Only the exact permitted night economy command depends on guard service;
    an actual return/medical alternative remains selectable without it.
    """
    intent=getattr(world,'forage_contract',None)
    if not intent:return True
    commands=bundle if isinstance(bundle,dict) else {c.actor:c.command for c in bundle}
    if not require_service and commands.get(intent['actor'])!=intent['command']:
        return True
    plan=world.task_side_plan;roster=world.night_roster
    if intent.get('task_commands'):
        task_command=commands.get(roster.p)
        if task_command not in intent['task_commands']:
            if complete or task_command is not None or any(
                    c.get('controllerId')==roster.p for c in commands.values()):
                return False
    for identity,keys in ((roster.w,('a','b')),(roster.p,('c',))):
        actor=world.ours.get(identity)
        if not actor or not actor.alive or identity not in world.night_defenders:
            return False
        guns=[g for g in world.weapons if g.pos in {plan[k] for k in keys}]
        if len(guns)!=len(keys) or any(g.cooldown is None for g in guns):return False
        window=min(g.cooldown for g in guns)
        command=commands.get(identity)
        on_station=actor.pos==plan['w'] if identity==roster.w else actor.pos in plan['c_stands']
        service=getattr(world,'repair_service',{}).get(identity,{})
        available=service_valid(world,identity)
        if not on_station:
            if identity==roster.w or not available or not service.get('continuing_selected_repair'):
                return False
            if command is None:
                if complete:return False
            elif command not in service['allowed_commands']:
                return False
        elif command:
            if command.get('action')=='move':
                if not available or command not in service['allowed_commands']:
                    return False
            elif window<1:
                # No invented empty cooldown just because an attack candidate
                # was not yet generated. A stationary action consumes this tick.
                return False
    for wall in intent['critical']:
        choices=[]
        for identity in wall['eligible']:
            row=getattr(world,'repair_service',{}).get(identity,{})
            if (service_valid(world,identity) and row.get('wall')==wall['id']
                    and row.get('phase') in ('GO_REPAIR','USE_PENDING')):
                chosen=commands.get(identity)
                if chosen in row['allowed_commands']:
                    choices.append(identity)
                elif not complete and chosen is None and not any(
                        c.get('controllerId')==identity for c in commands.values()):
                    choices.append(identity)
        if not choices:return False
    return fire_allowed(world, commands, complete)


def assess(world, rules, policy, deadline):
    plan=world.task_side_plan
    roster=world.night_roster
    w,p=(world.ours.get(i) for i in (roster.w,roster.p))
    report={'allowed':False,'reason':'guard observations incomplete','walls':[]}
    if not all(a and a.alive and a.backpack is not None for a in (w,p)):
        return report
    guns={g.pos:g for g in world.weapons}
    if any(q not in guns or any(v is None for v in (guns[q].cooldown,guns[q].attack_range,
            guns[q].level)) for _,q in plan['slots']):
        report['reason']='weapon observations incomplete'
        return report
    threats=[r for r in world.robots.values() if r.alive and r.abnormal!='dizzy']
    if any(r.attack_power is None or r.attack_range is None for r in threats):
        report['reason']='robot damage or range unknown'
        return report
    blue,yellow=station_rings(plan['anchor'])
    walls={u.pos:u for u in world.ours.values() if u.alive and u.kind=='wall'}
    if not yellow-{plan['gate']} <= walls.keys():
        report['reason']='permanent wall missing'
        return report
    served={w.id:[guns[plan['a']],guns[plan['b']]],p.id:[guns[plan['c']]]}
    windows={i:min(g.cooldown for g in rows) for i,rows in served.items()}
    ready={a.id:min(1,sum(g.cooldown==0 and distance(a.pos,g.pos)<=1 and g.attack_range>0
                          for g in served[a.id])) for a in (w,p)}
    report.update(personal_fixers={a.id:a.inventory['WallFixer'] for a in (w,p)},
                  observed_windows=windows,adjacent_ready_without_w=ready[p.id],adjacent_ready_without_p=ready[w.id])
    # P's repair trip reserves W's real stand and avoids observed lethal cells.
    blocked=(world.occupied|world.navigation_avoided.get(p.pos,set())|{plan['w'],plan['gate']})-{p.pos}
    for x in range(world.width):
        if time.monotonic()>=deadline:raise BudgetExpired
        for y in range(world.height):
            q=(x,y)
            if 2*sum(r.attack_power for r in threats if distance(r.pos,q)<=r.attack_range)>=p.health:
                blocked.add(q)
    blocked.discard(p.pos)
    outgoing=_field(world,{p.pos},blocked,deadline)
    home=_field(world,set(plan['c_stands'])-blocked,blocked,deadline)
    critical_options=[]
    for point,wall in sorted(walls.items()):
        if point not in yellow:continue
        if time.monotonic()>=deadline:raise BudgetExpired
        maximum=rules.max_health.get('wall',{}).get(wall.level)
        if maximum is None:
            report['reason']='wall maximum health unknown'
            return report
        hit=pressure(world,wall)
        critical=wall.health*10<maximum*3 or (hit is not None and hit>=wall.health)
        actions={}
        if w.pos==plan['w'] and distance(w.pos,point)<=1:
            actions[w.id]=1
        trips=[outgoing[q]+1+home[q] for q in neighbours(point) if q in outgoing and q in home]
        if trips:actions[p.id]=min(trips)
        eligible=[a.id for a in (w,p) if wall.health*10<maximum*3 and policy.repair_plan_enabled and a.inventory['WallFixer']>0
                  and actions.get(a.id,float('inf'))<=windows[a.id]
                  and (not world.phase_task or a.id==w.id or actions[a.id]==1)]
        if critical and getattr(world,'forage_service_checked',False):
            eligible=[i for i in eligible if service_valid(world,i)
                      and world.repair_service[i].get('wall')==wall.id
                      and world.repair_service[i].get('phase') in ('GO_REPAIR','USE_PENDING')]
        report['walls'].append(dict(id=wall.id,critical=critical,repair_actions=actions,eligible=eligible))
        if critical:critical_options.append(eligible)
    # At most one immediate repair per guard: the same personal voucher/window
    # cannot cover several simultaneously critical walls by independent claims.
    assignments={frozenset()}
    for eligible in critical_options:
        assignments={used|{identity} for used in assignments for identity in eligible if identity not in used}
    report['allowed']=bool(assignments)
    report['reason']='observed coverage adequate' if assignments else 'critical walls lack distinct timely repairs'
    return report
