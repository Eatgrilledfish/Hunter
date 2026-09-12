"""Small transactional session state; predictions never overwrite the snapshot."""
from collections import OrderedDict
from dataclasses import dataclass, field

from .protocol import obj, array, fingerprint, distance, position
from .rules import Clock
from .tasks import TaskEngine
from .intelligence import Intelligence
from .opponent import Opponent
from .lookahead import RiskMemory
from .joint_lookahead import Memory as JointMemory
from .defence import DefenceProcurement
from .medical import MedicalSupply
from .recovery import BaseRecovery
from .economic_routes import EconomicRoutes
from .sunset_market import SunsetMarket
from .day_schedule import DaySchedule
from .task_side_layout import TaskSideLayout
from .external_gate import ExternalGate
from .night_roles import NightRoster
from .repair_plan import RepairPlan
from .repair_supply import RepairSupply
from .navigation import neighbours
from .protocol import pos_json


def _move_retry_round(failure):
    count, last = failure
    return last + min(32, 2**min(count, 5))


@dataclass
class Session:
    epoch: int
    key: tuple[str, str]
    origin: int | None = None
    last_round: int = -1
    last_response: dict = field(default_factory=dict)
    cache: OrderedDict = field(default_factory=OrderedDict)
    failed: dict = field(default_factory=dict)
    failed_moves: dict = field(default_factory=dict)
    last_positions: dict = field(default_factory=dict)
    enemy_memory: dict = field(default_factory=dict)
    news: list = field(default_factory=list)
    feedback_counts: dict = field(default_factory=dict)
    tasks: TaskEngine = field(default_factory=TaskEngine)
    intelligence: Intelligence = field(default_factory=Intelligence)
    opponent: Opponent = field(default_factory=Opponent)
    risk: RiskMemory = field(default_factory=RiskMemory)
    joint_risk: JointMemory = field(default_factory=JointMemory)
    defence: DefenceProcurement = field(default_factory=DefenceProcurement)
    medical: MedicalSupply = field(default_factory=MedicalSupply)

    recovery: BaseRecovery = field(default_factory=BaseRecovery)
    economic_routes: EconomicRoutes = field(default_factory=EconomicRoutes)
    day_schedule: DaySchedule = field(default_factory=DaySchedule)
    sunset_market: SunsetMarket = field(default_factory=SunsetMarket)
    task_layout: TaskSideLayout = field(default_factory=TaskSideLayout)
    external_gate: ExternalGate = field(default_factory=ExternalGate)
    night_roster: NightRoster = field(default_factory=NightRoster)
    repair: RepairPlan = field(default_factory=RepairPlan)
    repair_supply: RepairSupply = field(default_factory=RepairSupply)

    def task_actor(self, world):
        if not world.phase_task_observed and self.tasks.active:
            actor = world.ours.get(self.tasks.active.actor)
            return actor.id if actor is not None and actor.alive else None
        if not world.phase_task:
            return None
        for actor in world.movers:
            if actor.kind == "pioneer":
                # Freeze the pioneer while active text exists. The task engine
                # later determines identity/termination and channel eligibility.
                return actor.id
        return None

    def reconcile(self, world):
        if world.round == 0:
            self.origin = 0
        clock = Clock(world.round, self.origin)
        world.strategy_clock = clock
        self.risk.observe(world)
        self.joint_risk.observe(world)
        self.recovery.observe(world)
        self.economic_routes.observe(world, clock)
        world.move_yielding = set()
        if world.round == self.last_round + 1:
            feedback = obj(world.raw.get("lastRoundRoleActionResults"))
            previous = self.last_response.get("roleCommandMap", {})
            collisions = {}
            for identity, command in previous.items():
                actor = world.ours.get(identity)
                if (command.get('action')=='move' and feedback.get(identity) is False
                        and actor and actor.alive and actor.pos==self.last_positions.get(identity)):
                    target = position(command.get('targetPos',[{}])[0])
                    if target is not None:
                        collisions.setdefault(target,[]).append(identity)
            retry = set()
            for identities in collisions.values():
                if len(identities)>1:
                    ordered = sorted(identities)
                    retry.add(ordered[0])
                    world.move_yielding.update(ordered[1:])
            for identity, command in previous.items():
                outcome = feedback.get(identity)
                signature = (identity, fingerprint(command))
                if command.get("action") == "move" and identity in self.last_positions:
                    origin = self.last_positions[identity]
                    target = position(command.get("targetPos", [{}])[0])
                    key = (identity, origin, target)
                    actor = world.ours.get(identity)
                    if identity in retry:
                        # A shared-target failure is not proof of a blocked
                        # edge. Retry one actor while its peers wait. A later
                        # solo failure still enters ordinary obstacle backoff.
                        self.failed_moves.pop(key,None)
                        self.failed.pop(signature,None)
                        continue
                    if outcome is False and actor is not None and actor.alive and actor.pos == origin:
                        count = self.failed_moves.get(key, (0, 0))[0] + 1
                        self.failed_moves[key] = (count, world.round)
                    elif outcome is True:
                        self.failed_moves.pop(key, None)
                if outcome is False:
                    old = self.failed.get(signature, (0, 0))
                    self.failed[signature] = (old[0]+1, world.round)
                    self.feedback_counts["action_false"] = self.feedback_counts.get("action_false", 0)+1
                elif outcome is True:
                    self.failed.pop(signature, None)
        self.failed_moves = {k:v for k,v in self.failed_moves.items()
                             if world.round-v[1] <= 130 and k[0] in world.ours and world.ours[k[0]].alive}
        self.failed_moves = dict(list(self.failed_moves.items())[-128:])
        world.navigation_avoided = {actor.pos: {target for (identity, origin, target), (count, last) in self.failed_moves.items()
                                   if identity == actor.id and origin == actor.pos and target is not None
                                   and world.round < _move_retry_round((count, last))}
                                   for actor in world.movers}
        self.last_positions = {actor.id: actor.pos for actor in world.movers}
        # A skipped round cannot be attributed to the last command we sent.
        self.failed = {k: v for k, v in self.failed.items() if world.round-v[1] <= 8}
        self.failed = dict(list(self.failed.items())[-64:])
        for error in array(world.raw.get("errors")):
            code = obj(error).get("errorCode")
            if type(code) is int and 0 <= code <= 5:
                key = f"reported_error_{code}"
                self.feedback_counts[key] = self.feedback_counts.get(key, 0)+1
        news = obj(world.raw.get("worldNews"))
        for section in ("officialNews", "folkLegends"):
            text = news.get(section)
            if not isinstance(text, str) or not text:
                continue
            digest = fingerprint(text)
            previous = next((n for n in reversed(self.news) if n["section"] == section), None)
            # At a certain new day, identical text is retained as a publication
            # candidate with ambiguity, rather than silently shifting an old date.
            if previous and previous["hash"] == digest and (clock.day is None or previous["observed_day"] == clock.day):
                continue
            self.news.append({"section": section, "hash": digest, "text": text[:131072],
                              "observed_round": world.round, "observed_day": clock.day,
                              "publication_certain": self.origin is not None and (world.round-self.origin) % 130 == 0,
                              "truncated_locally": len(text) > 131072})
        self.news = self.news[-64:]
        self.tasks.reconcile(world, clock, self.epoch)
        self.intelligence.reconcile(world, clock, self)
        self.opponent.reconcile(world, clock)
        self.defence.reconcile(world, clock)
        self.medical.reconcile(world, clock)
        self.repair_supply.reconcile(world, clock)
        self.enemy_memory = self.opponent.belief.sightings
        return clock

    def backed_off(self, actor, command, round_no):
        if command.get("action") == "move":
            targets = array(command.get("targetPos"))
            target = position(targets[0]) if targets else None
            failure = self.failed_moves.get((actor, self.last_positions.get(actor), target))
            if failure and round_no < _move_retry_round(failure):
                return True
        failure = self.failed.get((actor, fingerprint(command)))
        return bool(failure and round_no-failure[1] < min(4, failure[0]+1))

    def move_retry_windows(self, round_no):
        return [{"actor": actor, "origin": origin, "target": target,
                 "failures": failure[0], "last_failure_round": failure[1],
                 "retry_round": _move_retry_round(failure)}
                for (actor, origin, target), failure in self.failed_moves.items()
                if self.last_positions.get(actor) == origin and round_no < _move_retry_round(failure)]

    def failed_move_steps(self, world):
        """Temporary first-step exclusions from attributed action feedback only.

        A false result does not reveal its cause or an enemy's future position.
        Reuse both retry windows, without changing observed occupancy.
        """
        return {actor.id: {p for p in neighbours(actor.pos)
                           if self.backed_off(actor.id, {"action": "move", "targetPos": [pos_json(p)]}, world.round)}
                          | world.navigation_avoided.get(actor.pos, set())
                for actor in world.movers}

    def filter_failures(self, candidates, round_no):
        result = []
        for candidate in candidates:
            # Rotate to alternate routes/targets after a confirmed false. Backoff
            # expires, since a moving robot may have caused the collision.
            if self.backed_off(candidate.actor, candidate.command, round_no):
                continue
            result.append(candidate)
        return result
