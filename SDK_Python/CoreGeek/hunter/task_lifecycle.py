"""Six finite task episodes, counted only after an observed termination.

Ending an episode consumes a task even when its answer was wrong or absent.
The protocol has no task ID, remaining count or pass-rate receipt. Missing
observations therefore never establish completion or point exhaustion.
"""
from dataclasses import dataclass, field

from .protocol import position


FAMILIES = ('自进化类1', '自进化类2')
TASKS_PER_FAMILY = 3


def family(world, task):
    """Bind to one observed own point; a two-cell point is still one family."""
    anchor = position(task.get('taskPosition'))
    names = [i for i in (1, 2)
             if anchor is not None and anchor in world.zones.get(world.side+'TaskPoint'+str(i), ())]
    if len(names) != 1:
        return None
    value = FAMILIES[names[0]-1]
    # Older requests omitted taskType; the explicit owned zone still identifies
    # its point. A contradictory advertised type is not a quota identity.
    return value if task.get('taskType', value) == value else None


@dataclass
class TaskLifecycle:
    consumed: dict = field(default_factory=lambda: {name: 0 for name in FAMILIES})
    ended: set = field(default_factory=set)
    events: list = field(default_factory=list)

    @property
    def exhausted(self):
        return all(self.consumed[name] >= TASKS_PER_FAMILY for name in FAMILIES)

    def available(self, world):
        # Do not retain vanished descriptors or turn missing map observations
        # into permanent exhaustion. Callers still check cooldown/isValid.
        return [task for task in world.tasks
                if (name := family(world, task)) is not None
                and self.consumed[name] < TASKS_PER_FAMILY]

    def end(self, task, reason, world):
        name = task.task_family
        if name not in FAMILIES or task.key in self.ended or self.consumed[name] >= TASKS_PER_FAMILY:
            return
        actor = world.ours.get(task.actor)
        observed = (
            reason == 'phaseTask ended; success not established'
            and world.phase_task_observed and world.phase_task == ''
            or reason == 'explicit task timeout' and world.phase_task_observed
            or reason == 'task text changed; old feedback quarantined'
            and world.phase_task_observed and bool(world.phase_task) and world.phase_task != task.text
            or reason == 'pioneer unavailable' and actor is not None and actor.health == 0
            or reason == 'pioneer left task neighbourhood' and actor is not None and actor.alive
        )
        if not observed:
            return
        self.ended.add(task.key)
        before = self.consumed[name]
        self.consumed[name] = min(TASKS_PER_FAMILY, before+1)
        self.events.append({'task_key': task.key, 'family': name, 'round': world.round,
                            'reason': reason, 'consumed': self.consumed[name],
                            'counted': before < TASKS_PER_FAMILY, 'official_correctness': 'UNKNOWN'})
        self.events = self.events[-12:]

    def snapshot(self, world):
        return {'limit_per_family': TASKS_PER_FAMILY, 'consumed_observed': dict(self.consumed),
                'remaining_upper_bound': {name: TASKS_PER_FAMILY-count for name, count in self.consumed.items()},
                'exhausted': self.exhausted,
                'available_families': sorted({family(world, task) for task in self.available(world)}),
                'phase_task_observed': world.phase_task_observed, 'events': list(self.events)}
