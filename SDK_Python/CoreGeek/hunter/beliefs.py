"""Bounded opponent observations; absence never certifies death or no defence."""
from dataclasses import dataclass, field

from .protocol import MOBILE, WEAPONS, distance


@dataclass
class OpponentBelief:
    sightings: dict = field(default_factory=dict)
    waves: list = field(default_factory=list)
    last_round: int = -1
    pressure: dict = field(default_factory=dict)
    night_pressure: dict = field(default_factory=dict)
    night_memory_round: int = -1

    def remember_night(self, world, clock):
        """Retain observed damage for the following daytime buying window.

        This is evidence of past pressure, never a claim that robots survived
        dawn, that hidden defences are absent, or that summons will be profitable.
        """
        if world.round <= self.night_memory_round:
            return
        self.night_memory_round = world.round
        for identity, record in list(self.night_pressure.items()):
            enemy = world.enemies.get(identity)
            expiry = min(origin+record['day']*130+70 for origin in clock.offsets)
            if (world.round >= expiry or enemy is None or not enemy.alive
                    or enemy.kind != 'station' or enemy.pos != record['pos']
                    or enemy.level != record['level'] or enemy.health is None
                    or enemy.health > record['last_health']):
                self.night_pressure.pop(identity)
            else:
                record['last_health'] = enemy.health
        if clock.phases != {'night'} or clock.day is None or not 1 <= clock.day < 10:
            return
        for identity, evidence in self.pressure.items():
            if evidence['round'] != world.round:
                continue
            enemy = world.enemies[identity]
            previous = self.night_pressure.get(identity)
            same_night = previous is not None and previous['day'] == clock.day
            self.night_pressure[identity] = {
                'day': clock.day, 'round': world.round, 'pos': enemy.pos,
                'level': enemy.level, 'last_health': enemy.health,
                'observed_loss': -evidence['hp_change']+(previous['observed_loss'] if same_night else 0),
                'observations': 1+(previous['observations'] if same_night else 0),
                'robot_ids_at_observation': list(evidence['robot_ids']),
                'basis': 'previous-night observed HP decreases with assigned robots; forecast opportunity only'}
        self.night_pressure = dict(sorted(self.night_pressure.items(),
                                         key=lambda row: (-row[1]['round'], row[0]))[:4])

    def previous_wave_evidence(self, world, clock):
        if clock.phases != {'day'} or clock.day is None:
            return []
        return [identity for identity, record in self.night_pressure.items()
                if clock.day == record['day']+1 and identity in world.enemies
                and world.enemies[identity].alive
                and world.enemies[identity].pos == record['pos']
                and world.enemies[identity].level == record['level']
                and world.enemies[identity].health is not None
                and world.enemies[identity].health <= record['last_health']]

    def reconcile(self, world):
        if world.round <= self.last_round:
            return
        other_side = 'defender' if world.side == 'challenger' else 'challenger'
        robots = [r for r in world.robots.values() if r.alive and r.target_team == other_side]
        self.pressure = {key: value for key, value in self.pressure.items()
                         if world.round-value['round'] <= 16}
        for identity, enemy in world.enemies.items():
            previous = self.sightings.get(identity)
            history = list(previous.get('history', [])) if previous else []
            delta = None
            if (previous and previous['last_seen_round'] == world.round-1
                    and previous['kind'] == enemy.kind and previous['pos'] == enemy.pos
                    and previous['health'] is not None and enemy.health is not None):
                delta = enemy.health-previous['health']
            if enemy.kind in {'station', 'wall'}:
                history.append({'round': world.round, 'health': enemy.health,
                                'level': enemy.level, 'hp_change': delta, 'attribution': 'unassigned'})
            self.sightings[identity] = {'pos': enemy.pos, 'health': enemy.health,
                                       'kind': enemy.kind, 'level': enemy.level,
                                       'last_seen_round': world.round, 'history': history[-8:]}
            if enemy.kind == 'station':
                near = [r for r in robots if distance(r.pos, enemy.pos) <= 8]
                recovered = (previous and previous['health'] is not None and enemy.health is not None
                             and enemy.health > previous['health'])
                changed_identity = previous and (previous['kind'] != enemy.kind or previous['pos'] != enemy.pos)
                if not enemy.alive or recovered or changed_identity:
                    self.pressure.pop(identity, None)
                elif delta is not None and delta < 0 and near:
                    self.pressure[identity] = {'round': world.round, 'hp_change': delta,
                                               'robot_ids': [r.id for r in near][:32],
                                               'basis': 'observed HP decrease with nearby assigned robots; cause unassigned'}
        self.sightings = dict(sorted(self.sightings.items(), key=lambda row: (-row[1]['last_seen_round'], row[0]))[:256])
        self.pressure = {k: v for k, v in self.pressure.items() if k in self.sightings}
        self.waves.append({'round': world.round, 'assigned_count': len(robots),
                           'assigned_hp': sum(r.health for r in robots),
                           'unassigned_count': sum(r.alive and r.target_team is None for r in world.robots.values())})
        self.waves = self.waves[-16:]
        self.last_round = world.round

    def describe(self, world):
        visible = [e for e in world.enemies.values() if e.alive]
        weapons = [e for e in visible if e.kind in WEAPONS]
        operators = [e for e in visible if e.kind in MOBILE]
        # Maximum matching counts observable staffing opportunities, not shots.
        matched = {}

        def assign(weapon, seen):
            for actor in operators[:64]:
                if actor.id in seen or distance(actor.pos, weapon.pos) > 1:
                    continue
                seen.add(actor.id)
                if actor.id not in matched or assign(matched[actor.id], seen):
                    matched[actor.id] = weapon
                    return True
            return False

        for weapon in weapons[:64]:
            assign(weapon, set())
        hidden = {identity: {'last_seen_round': record['last_seen_round'],
                             'age': world.round-record['last_seen_round'],
                             'last_position': record['pos'], 'alive_now': None,
                             'position_radius_if_alive': min(max(world.width, world.height), world.round-record['last_seen_round'])
                             if record['kind'] in MOBILE else 0,
                             'position_basis': 'one-cell mobile steps; stationary asset if it still exists'}
                  for identity, record in self.sightings.items()
                  if identity not in world.enemies}
        return {'visible_weapons': len(weapons), 'visible_operators': len(operators),
                'visible_staffing_opportunities': len(matched),
                'staffing_search_truncated': len(operators) > 64 or len(weapons) > 64,
                'additional_hidden_weapons': None, 'enemy_gold': None,
                'hidden_sightings': hidden, 'wave': self.waves[-1] if self.waves else None,
                'recent_pressure': self.pressure,
                'scope': 'observed availability and HP changes, not a fire-rate or defeat probability'}

    def purchase_evidence(self, world):
        other_side = 'defender' if world.side == 'challenger' else 'challenger'
        return [identity for identity, record in self.pressure.items()
                if 0 <= world.round-record['round'] <= 16
                and identity in world.enemies and world.enemies[identity].alive
                and any(r.alive and r.target_team == other_side
                        and distance(r.pos, world.enemies[identity].pos) <= 8 for r in world.robots.values())]
