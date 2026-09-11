"""Receding-horizon role risk under explicitly hypothetical threat scenarios.

No attack cadence or robot speed is asserted as an official rule. Scenarios
hold positions or expand reach by one cell per opportunity, with full/half
attack opportunity rates. They ignore future friendly kills and unknown
obstacles on robot routes. Model weights are error discounts, not probabilities.
"""
from dataclasses import dataclass, field
import time

from .arbitration import Candidate
from .navigation import neighbours
from .protocol import distance, pos_json
from .rules import Clock

ROBOT_DAMAGE = {"smallRobot": 5, "middleRobot": 10, "largeRobot": 20, "bossRobot": 40}
SCENARIOS = (("fixed_full", False, 1.0), ("fixed_half", False, .5),
             ("approach_full", True, 1.0), ("approach_half", True, .5))


def losses(world, clock, path):
    result = {name: 0.0 for name, _, _ in SCENARIOS}
    for index, point in enumerate(path):
        if "night" not in Clock(world.round+index, clock.origin).phases:
            continue
        for robot in world.robots.values():
            if not robot.alive:
                continue
            power = ROBOT_DAMAGE.get(robot.kind, robot.attack_power or 0)
            for name, approaches, rate in SCENARIOS:
                if robot.abnormal == "dizzy" and (index == 0 or not approaches):
                    continue
                reach = 3+(index+1 if approaches else 0)
                if distance(robot.pos, point) <= reach:
                    result[name] += power*rate
    return result


@dataclass
class RiskMemory:
    errors: dict = field(default_factory=dict)
    samples: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    last_comparison: list = field(default_factory=list)
    recent_hp_losses: dict = field(default_factory=dict)
    damaged_actors: set = field(default_factory=set)

    def weights(self):
        return {name: 1/(1+4*self.errors.get(name, 0)) for name, _, _ in SCENARIOS}

    def observe(self, world):
        self.last_comparison = []
        pending, self.pending = self.pending, {}
        if pending.get("round") != world.round-1:
            self.recent_hp_losses.clear()
            self.damaged_actors.clear()
            return
        self.recent_hp_losses = {i:[row for row in rows if row[0] >= world.round-2]
                                 for i,rows in self.recent_hp_losses.items() if i in world.ours and world.ours[i].alive}
        self.damaged_actors.intersection_update(i for i,u in world.ours.items() if u.alive)
        for identity, prior in pending.get("assets", {}).items():
            current = world.ours.get(identity)
            if current is not None and current.health is not None and current.health > prior["health"]:
                self.damaged_actors.discard(identity)
            if current is None or current.health is None or current.health > prior["health"] or prior["healing"] or current.pos != prior["position"]:
                self.recent_hp_losses.pop(identity, None)
                continue
            observed = prior["health"]-current.health
            self.recent_hp_losses.setdefault(identity, []).append((world.round, observed))
            if observed > 0:
                self.damaged_actors.add(identity)
            # HP changes are not labelled as robot damage or a learned cadence.
            # Compare only this one-step loss proxy, including suppression error.
            for name, predicted in prior["losses"].items():
                expected = min(prior["health"], predicted)
                error = abs(observed-expected)/max(5, observed, expected)
                self.errors[name] = .8*self.errors.get(name, 0)+.2*error
                self.samples[name] = self.samples.get(name, 0)+1
            self.last_comparison.append({"asset": identity, "observed_hp_loss": observed,
                                         "predicted": prior["losses"], "attribution": "unassigned"})

    def finalize(self, world, clock, response):
        if clock.phases != {"night"}:
            self.pending = {}
            return
        commands = response["roleCommandMap"]
        assets = {}
        for unit in world.movers+world.stations:
            command = commands.get(unit.id, {})
            point = unit.pos
            if command.get("action") == "move":
                target = command["targetPos"][0]
                point = target["x"], target["y"]
            healing = command.get("action") == "use" and command.get("name") == "Medicine"
            healing |= any(c.get("action") == "use" and "UpgradeVoucher" in c.get("name", "")
                           and c.get("targetPos") == [pos_json(unit.pos)] for c in commands.values())
            assets[unit.id] = {"health": unit.health, "position": point, "losses": losses(world, clock, [point]), "healing": healing}
        self.pending = {"round": world.round, "assets": assets}


def weighted_loss(values, weights):
    return sum(values[k]*weights[k] for k in values)/sum(weights.values())


def propose(world, clock, actor, task, memory, policy, deadline):
    if clock.phases != {"night"} or not world.robots or not policy.lookahead_enabled:
        return [], {"status": "inactive"}
    weights = memory.weights() if memory else {name: 1.0 for name, _, _ in SCENARIOS}
    horizon = max(4, min(8, policy.lookahead_horizon))
    baseline = losses(world, clock, [actor.pos]*horizon)
    if weighted_loss(baseline, weights) >= actor.health:
        horizon = 8
        baseline = losses(world, clock, [actor.pos]*horizon)
    observation = {"horizon": horizon, "hold_losses": baseline, "weights": weights,
                   "status": "complete", "lower_possible_loss": 0,
                   "scope": "per-role hypothetical threat opportunities; not a joint damage forecast"}
    initial_penalty = max(0, weighted_loss(baseline, weights)-actor.health*.5)
    if initial_penalty <= 0:
        return [], observation
    occupied = world.occupied-{actor.pos}
    controlled = {w.id for w in world.weapons if distance(actor.pos, w.pos) <= 1}
    # Each state is a possible role path. Other units are never assumed to have
    # vacated their current cells; only the first action can be emitted.
    beam = [([], 0.0)]
    for depth in range(horizon):
        expanded = []
        for path, _ in beam:
            current = path[-1] if path else actor.pos
            for point in [current]+neighbours(current):
                if time.monotonic() >= deadline:
                    observation["status"] = "budget_incomplete"
                    return [], observation
                if not world.inside(point) or point in occupied:
                    continue
                # An uncertain long-range scenario alone cannot end a task.
                if task and not any(distance(point, p) <= 1 for p in task.cells):
                    continue
                proposed = path+[point]
                predicted = losses(world, clock, proposed)
                penalty = max(0, weighted_loss(predicted, weights)-actor.health*.5)
                moves = sum(a != b for a, b in zip([actor.pos]+proposed, proposed))
                leaving = bool(controlled) and any(not any(w.id in controlled and distance(p, w.pos) <= 1
                                                        for w in world.weapons) for p in proposed)
                value = penalty + moves*2 + (120 if leaving else 0)
                expanded.append((proposed, value))
        # Position plus first action retains alternative first steps for arbiter.
        seen, beam = set(), []
        for path, value in sorted(expanded, key=lambda p: (p[1], p[0])):
            key = path[0], path[-1]
            if key not in seen:
                seen.add(key)
                beam.append((path, value))
            if len(beam) == 12:
                break
    candidates, first_steps = [], set()
    for path, value in beam:
        first = path[0]
        improvement = initial_penalty-value
        if first == actor.pos or first in first_steps or improvement <= 0:
            continue
        first_steps.add(first)
        candidates.append(Candidate(actor.id, {"action": "move", "targetPos": [pos_json(first)]},
                                    min(60, improvement*policy.lookahead_weight),
                                    "bounded multi-round risk reduction; hypothetical robot scenarios"))
        if len(candidates) == 4:
            break
    observation["best_path"] = beam[0][0] if beam else []
    observation["best_penalty"] = beam[0][1] if beam else None
    return candidates, observation
