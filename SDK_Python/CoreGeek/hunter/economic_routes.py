"""Finish selected economic travel before revaluing competing ore/sell routes.

Only actual selected moves create state. Current inventory, facilities, phase,
movement feedback and stronger non-economic actions can cancel a trip.
"""
from copy import deepcopy
from dataclasses import dataclass, field
import time

from .arbitration import Candidate
from .navigation import route
from .protocol import MINERALS, distance, pos_json


@dataclass
class EconomicRoutes:
    active: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)

    def observe(self, world, clock):
        feedback = world.raw.get("lastRoundRoleActionResults", {})
        feedback = feedback if isinstance(feedback, dict) else {}
        for identity, entry in list(self.active.items()):
            actor = world.ours.get(identity)
            targets, zone = entry["goal"]["targets"], entry["goal"]["zone"]
            invalid = (clock.phases != {"day"} or world.round != entry["round"]+1 or
                       actor is None or not actor.alive or actor.kind != "worker" or actor.backpack is None or
                       feedback.get(identity) is False or actor.pos != entry["expected"] or
                       world.round-entry["started"] > 256 or
                       not set(targets).issubset(world.zones.get(zone, ())))
            if invalid or any(distance(actor.pos, target) <= 1 for target in targets):
                self.active.pop(identity, None)

    def candidates(self, world, clock, policy, materials, deadline, task_actor=None):
        self.diagnostic = {}
        if not policy.economic_route_commitment_enabled or clock.phases != {"day"}:
            self.active.clear()
            return []
        result = []
        for identity, entry in list(self.active.items()):
            if time.monotonic() >= deadline:
                break
            actor = world.ours.get(identity)
            if actor is None or identity == task_actor:
                self.active.pop(identity, None)
                continue
            goal = entry["goal"]
            reserve = materials.get(identity, {})
            if goal["purpose"] == "sell":
                useful = any(actor.inventory[name] > reserve.get(name, 0) and world.vendor.get(name, 0) > 0
                             for name in MINERALS)
            else:
                useful = (actor.capacity is not None and len(actor.backpack) < actor.capacity and
                          (actor.inventory[goal["zone"]] < reserve.get(goal["zone"], 0) or
                           world.vendor.get(goal["zone"], 0) > 0))
            length, steps = route(world, actor, goal["targets"], deadline) if useful else (None, [])
            if not length or not steps:
                self.active.pop(identity, None)
                continue
            self.diagnostic[identity] = {"goal":deepcopy(goal), "remaining_moves":length,
                                          "started_round":entry["started"]}
            result.extend(Candidate(identity, {"action":"move", "targetPos":[pos_json(point)]},
                                    entry["value"]-i*.01, "finish selected economic trip to current interaction cell",
                                    route_goal=deepcopy(goal)) for i, point in enumerate(steps[:4]))
        return result

    def finalize(self, world, selected, policy):
        current = {}
        if policy.economic_route_commitment_enabled:
            for candidate in selected:
                if candidate.command.get("action") != "move" or candidate.route_goal is None:
                    continue
                previous = self.active.get(candidate.actor, {})
                same = previous.get("goal") == candidate.route_goal
                point = candidate.command["targetPos"][0]
                current[candidate.actor] = {"goal":deepcopy(candidate.route_goal), "round":world.round,
                    "expected":(point["x"],point["y"]), "value":max(0, candidate.utility),
                    "started":previous["started"] if same else world.round}
        # A stronger action or a collision-induced hold releases the old trip;
        # next round plans from the new snapshot instead of preserving stale work.
        self.active = dict(list(current.items())[:16])
