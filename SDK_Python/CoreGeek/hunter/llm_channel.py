"""One official prompt/reply channel, independent of its quota class."""
from dataclasses import dataclass, field
from .protocol import fingerprint


@dataclass
class LLMChannel:
    pending: dict = field(default_factory=dict)
    history: list = field(default_factory=list)

    def available(self):
        return not self.pending

    def owns(self, pending):
        return not self.pending or self.pending.get('nonce')==pending.get('context',{}).get('nonce')

    def claim(self, world, context, quota_class):
        if self.pending:raise ValueError('LLM channel already owned')
        self.pending=dict(nonce=context['nonce'],request_id=fingerprint(context['nonce'])[:16],
            purpose=context['purpose'],task_instance=context.get('task_instance'),
            cycle_id=context.get('cycle_id'),sent_round=world.round,quota_class=quota_class)
        self.history.append(dict(self.pending));self.history=self.history[-32:]

    def sync(self, session, world):
        task=session.tasks.active;news=session.intelligence
        # A missing optional news reply must never stall the next task step.
        if (news.pending and news.pending.get('quota_class')=='task_exempt'
                and world.round>news.pending['round'] and task
                and not task.llm_pending and not task.sandbox_pending):
            news.cycle.retired.add(news.pending['context']['nonce'])
            news.pending=None;news.llm_status='interrupted'
        active=[p for p in (task.llm_pending if task else None,news.pending) if p]
        if not any(self.pending.get('nonce')==p.get('context',{}).get('nonce') for p in active):
            self.pending={}


def available(world):
    channel=getattr(world,'llm_channel',None)
    return channel is None or channel.available()


def owns(world, pending):
    channel=getattr(world,'llm_channel',None)
    return channel is None or channel.owns(pending)


def claim(world, context, quota_class):
    channel=getattr(world,'llm_channel',None)
    if channel:channel.claim(world,context,quota_class)
