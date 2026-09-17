"""Base restoration from observed HP loss, without inventing maximum HP."""
from dataclasses import dataclass, field
import time

from .navigation import distance_field, interaction_cells


def restoration_needed(base, reference, weapon_demand):
    """Any credible loss creates demand; urgency and funding are separate."""
    return bool(base.alive and base.level in (1, 2) and reference > 0
                and base.health is not None and reference-base.health > 0)


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
            # Record every credible injury. The investment stage separately
            # reserves unfinished weapons before a noncritical joint checkout.
            weapon_demand = any(u.level in (1,2) for u in world.weapons)
            threshold = 0
            self.diagnostic['bases'][base.id].update(priority_loss_fraction=threshold,
                weapon_demand=weapon_demand)
            if policy.base_recovery_enabled and restoration_needed(base, reference, weapon_demand):
                result.add(base.id)
        return result

    def adjacent_night(self, world, clock, targets, alternatives, task_actor=None):
        """One-step paid rescue, with a conservative effective-fire fallback.

        Unknown simultaneous damage ordering cannot prove that losing a useful
        shot saves the base. Keep that shot/control and report the conflict.
        No remote delivery or role reservation is created by this preview.
        """
        if clock.phases!={'night'}:return []
        from .arbitration import Candidate
        from .protocol import distance,pos_json
        result=[]
        for actor in world.movers:
            if actor.id==task_actor or actor.backpack is None:continue
            for base in world.stations:
                name=f'StationUpgradeVoucher{base.level}'
                if base.id not in targets or not actor.inventory[name] or distance(actor.pos,base.pos)>1:continue
                fire=[c for c in alternatives if
                    (c.command.get('controllerId')==actor.id or c.actor==actor.id)
                    and (any(n>0 for n in c.damage.values()) or c.suppression
                         or c.command.get('name') in {'Bomb','DizzyWeapon'})]
                report=self.diagnostic['bases'][base.id]
                if fire:
                    report.update(night_rescue='effective_defence_preserved',carrier=actor.id,
                        reason='simultaneous restoration and threat settlement not proven',
                        alternatives=len(fire))
                    continue
                report.update(night_rescue='adjacent_paid_candidate',carrier=actor.id)
                result.append(Candidate(actor.id,dict(action='use',name=name,targetPos=[pos_json(base.pos)]),
                    90,'adjacent paid base restoration without displacing effective fire'))
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
