"""Shared cooperative CPU-time allowance for the new duty planners per callback."""
from dataclasses import dataclass, field
import time


@dataclass
class DutyBudget:
    end: float
    seconds: float = .100
    used: float = 0.0
    modules: dict = field(default_factory=dict)

    def run(self, name, planner, deadline=float('inf')):
        start = time.monotonic()
        limit = min(deadline, self.end, start + max(0.0, self.seconds-self.used))
        try:
            # Even at expiry let the planner clear ephemeral permissions using
            # its existing deadline fallback, never reuse last round's actions.
            return planner(limit)
        finally:
            elapsed = max(0.0, time.monotonic()-start)
            self.used += elapsed
            self.modules[name] = self.modules.get(name, 0.0) + elapsed

    def diagnostic(self):
        return {'allowance_ms': self.seconds*1000, 'used_ms': self.used*1000,
                'remaining_ms': max(0.0, self.seconds-self.used)*1000,
                'modules_ms': {k:v*1000 for k,v in self.modules.items()}}
