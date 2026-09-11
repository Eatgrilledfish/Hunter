"""Base restoration from observed HP loss, without inventing maximum HP."""
from dataclasses import dataclass, field
import time

from .navigation import distance_field, interaction_cells


@dataclass
class BaseRecovery:
    observations: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)

    def observe(self, world):
        current = {}
        for base in world.stations[:16]:
            if base.level not in {1, 2, 3} or base.health is None:
                continue
            previous = self.observations.get(base.id, {})
            same = previous.get("position") == base.pos and previous.get("level") == base.level
            peak = max(base.health, previous.get("peak", 0)) if same else base.health
            current[base.id] = {"position": base.pos, "level": base.level, "peak": peak}
        # Missing/dead/replaced buildings do not inherit old damage evidence.
        self.observations = current

    def targets(self, world, rules, policy):
        result = set()
        self.diagnostic = {"enabled": policy.base_recovery_enabled, "bases": {}}
        for base in world.stations:
            prior = self.observations.get(base.id)
            if not prior or base.level not in {1, 2}:
                continue
            verified = rules.max_health.get("station", {}).get(base.level, 0)
            reference = max(prior["peak"], verified)
            loss = max(0, reference-base.health)
            self.diagnostic["bases"][base.id] = {"reference_hp": reference, "observed_hp": base.health,
                "loss_lower_bound": loss, "source": "verified_maximum" if verified >= prior["peak"] else "observed_peak"}
            # 20% is a strategy threshold, not an official maximum or damage model.
            if policy.base_recovery_enabled and loss > 0 and loss >= reference*.2:
                result.add(base.id)
        return result

    def ready(self, world, clock, policy, plans, targets, operator_stands, deadline):
        if not targets or clock.phases != {"day"} or time.monotonic() >= deadline:
            return []
        result = []
        for identity, plan in plans.items():
            if plan["target"] not in targets or not plan["name"].startswith("StationUpgradeVoucher"):
                continue
            actor, base = world.ours[identity], world.ours[plan["target"]]
            return_steps = 0
            stand = operator_stands.get(identity)
            if stand is not None:
                field = distance_field(world, [stand], actor.pos, deadline)
                ends = interaction_cells(world, [base.pos], actor.pos)
                if not ends or any(p not in field for p in ends):
                    continue
                return_steps = max(field[p] for p in ends)
            required = plan["steps"]+return_steps+policy.return_buffer
            self.diagnostic["bases"][base.id].update(worker=identity, required_steps=required)
            if required <= clock.until_night and time.monotonic() < deadline:
                result.extend(plan["candidates"])
        return result
