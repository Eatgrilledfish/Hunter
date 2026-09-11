"""Small transactional session state; predictions never overwrite the snapshot."""
from collections import OrderedDict
from dataclasses import dataclass, field

from .protocol import obj, array, fingerprint, distance
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
from .navigation import neighbours
from .protocol import pos_json


@dataclass
class Session:
    epoch: int
    key: tuple[str, str]
    origin: int | None = None
    last_round: int = -1
    last_response: dict = field(default_factory=dict)
    cache: OrderedDict = field(default_factory=OrderedDict)
    failed: dict = field(default_factory=dict)
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

    def task_actor(self, world):
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
        self.risk.observe(world)
        self.joint_risk.observe(world)
        self.recovery.observe(world)
        self.economic_routes.observe(world, clock)
        if world.round == self.last_round + 1:
            feedback = obj(world.raw.get("lastRoundRoleActionResults"))
            previous = self.last_response.get("roleCommandMap", {})
            for identity, command in previous.items():
                outcome = feedback.get(identity)
                signature = (identity, fingerprint(command))
                if outcome is False:
                    old = self.failed.get(signature, (0, 0))
                    self.failed[signature] = (old[0]+1, world.round)
                    self.feedback_counts["action_false"] = self.feedback_counts.get("action_false", 0)+1
                elif outcome is True:
                    self.failed.pop(signature, None)
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
        self.enemy_memory = self.opponent.belief.sightings
        return clock

    def backed_off(self, actor, command, round_no):
        failure = self.failed.get((actor, fingerprint(command)))
        return bool(failure and round_no-failure[1] < min(4, failure[0]+1))

    def failed_move_steps(self, world):
        """Temporary first-step exclusions from attributed action feedback only.

        A false result does not reveal its cause or an enemy's future position.
        Reuse the action retry window, without changing observed occupancy.
        """
        return {actor.id: {p for p in neighbours(actor.pos)
                           if self.backed_off(actor.id, {"action": "move", "targetPos": [pos_json(p)]}, world.round)}
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
