"""Observed submission latency, never an assertion of official correctness.

Public task descriptors identify a point's observed workload, not a semantic
task family. Censored/failed episodes retain the deadline as their bound.
"""
from dataclasses import dataclass, field


def mark(task, kind, round_no, sent=None):
    """Small cumulative ledger, independent of the bounded detailed event log."""
    task.metrics.setdefault('first', {}).setdefault(kind, round_no)
    task.metrics.setdefault('last', {})[kind] = round_no
    counts=task.metrics.setdefault('counts', {})
    counts[kind]=counts.get(kind,0)+1
    if sent is not None:
        waits=task.metrics.setdefault('wait_rounds', {})
        waits[kind]=waits.get(kind,0)+max(0,round_no-sent)


def descriptor(task, cells):
    values = [task.get(k) for k in ('timeoutRounds', 'scoreReward', 'goldReward')]
    if not cells or any(type(v) is not int or v < 0 for v in values) or values[0] < 1:
        return None
    return (tuple(sorted(cells)), *values)


@dataclass
class TaskTiming:
    samples: dict = field(default_factory=dict)

    def close(self, task, reason, round_no):
        key = task.timing_descriptor
        if key is None or task.accept_round is None or task.timeout is None:
            return
        # Only an immediate, accepted, error-free full submission followed by
        # phase disappearance provides an observed completion-time scenario.
        # This remains UNKNOWN official correctness, even with action=true.
        last = task.submitted[-1] if task.submitted else {}
        feedback = last.get('feedback', {})
        usable = (reason == 'phaseTask ended; success not established'
                  and last.get('partial') is False
                  and last.get('round') == round_no - 1
                  and feedback.get('round') == round_no
                  and feedback.get('action_accepted') is True
                  and feedback.get('errors') == [])
        duration = last.get('round', task.accept_round) - task.accept_round
        usable = usable and 0 < duration <= task.timeout
        row = {'duration': duration if usable else task.timeout,
               'kind': 'acknowledged_full_submission' if usable else 'censored',
               'official_correctness': 'UNKNOWN',
               'reused_program': any(e.get('kind') == 'program_recipe_reuse' for e in task.events)}
        row['stages'] = task.metrics
        self.samples.setdefault(key, []).append(row)
        self.samples[key] = self.samples[key][-8:]
        # Bound per-session memory; changing public descriptors do not share data.
        while len(self.samples) > 32:
            del self.samples[next(iter(self.samples))]

    def estimate(self, key, timeout):
        own = self.samples.get(key, []) if key is not None else []
        rows = own
        pooled = False
        if len(own) < 3 and key is not None:
            # A sparse point must not be penalized with a full deadline while
            # a familiar cooling point gets a fast estimate. Pool only equal
            # public rewards/deadlines as a prior, never as a family identity.
            rows = [r for k, group in self.samples.items() if k[1:] == key[1:] for r in group]
            pooled = True
        usable = len(rows) >= 3 and all(r['kind'] == 'acknowledged_full_submission' for r in rows)
        duration = min(timeout, max(r['duration'] for r in rows) + 2) if usable else timeout
        source = ('pooled_public_descriptor_submission_scenario' if pooled else
                  'observed_submission_upper_plus_margin') if usable else 'deadline_fallback'
        return {'duration': duration, 'sample_count': len(rows), 'point_sample_count': len(own),
                'source': source,
                'duration_interval': [1, timeout], 'official_correctness': 'UNKNOWN',
                'censored_count': sum(r['kind'] == 'censored' for r in rows)}
