"""Authoritative procurement work records for the W chain (2026-09-19 design).

One record owns demand identity, quote evidence, grant references, issued
command receipts and release conditions for a single actor's procurement
trip. SunsetMarket is the sole lifecycle writer; executors only return
quotes and candidates. Records never assert success before an observed
receipt and never reserve an actor they did not win in arbitration.
"""
from dataclasses import dataclass, field

# Work status (design §5). Terminal states release the actor's exclusivity.
PROPOSED = 'PROPOSED'
ACTIVE = 'ACTIVE'
WAIT_RECEIPT = 'WAIT_RECEIPT'
SUSPENDED = 'SUSPENDED'
DONE = 'DONE'
CANCELLED = 'CANCELLED'
TERMINAL = {DONE, CANCELLED}

# Quote outcomes (design §4.2). BUDGET_EXHAUSTED and UNKNOWN are unfinished
# judgements, never proof that no route or no cash exists.
FEASIBLE = 'FEASIBLE'
CASH_DEFICIT = 'CASH_DEFICIT'
NO_ROUTE = 'NO_ROUTE'
DEADLINE = 'DEADLINE'
CAPACITY = 'CAPACITY'
TARGET_INVALID = 'TARGET_INVALID'
BUDGET_EXHAUSTED = 'BUDGET_EXHAUSTED'
UNKNOWN = 'UNKNOWN'

# First-blocker layers (design §7): quote, funding, duty, rule, resource
# conflict and not-selected are distinguished instead of collapsed into a
# single low score.
BLOCK_LAYERS = ('quote', 'funding', 'duty', 'rule', 'resource', 'not_selected', 'receipt', 'target', 'strategy')

HISTORY_LIMIT = 12


@dataclass
class Quote:
    """One frame's feasibility answer for a work; never carried across rounds."""
    status: str
    round: int
    actor: str | None = None
    orders: dict = field(default_factory=dict)   # item -> quantity to buy this trip
    required: int | None = None                  # rounds the merged trip needs
    cost: int = 0                                # observed price total of orders
    reserve: int = 0                             # strategy cash protected alongside
    deadline: int | None = None
    source: str = ''                             # executor that produced it
    reason: str | None = None                    # blocker detail when not FEASIBLE


@dataclass
class ProcurementWork:
    work_id: str
    epoch: int
    actor: str
    purpose: str
    demands: tuple = ()                          # funding demand_ids this work consumes
    revision: int = 0
    status: str = PROPOSED
    step: str = 'quote'
    created: int = 0
    deadline: int | None = None
    blocked_since: int | None = None
    blocked_by: str | None = None                # one of BLOCK_LAYERS
    reason: str | None = None
    last_confirmed_progress: int = 0
    quote: Quote | None = None
    grants: dict = field(default_factory=dict)   # item -> granted gold this frame
    issued: dict | None = None                   # {'command','round'} awaiting receipt
    confirmed: dict = field(default_factory=dict)  # item -> observed quantity arrived
    bindings: list = field(default_factory=list)   # paid delivery targets [(unit_id, level)]
    preemption: dict | None = None               # {'reason','round','resume'}
    history: list = field(default_factory=list)  # bounded transition summary

    def item_names(self):
        names = set(self.confirmed)
        if self.quote:
            names.update(self.quote.orders)
        if self.issued:
            names.add(self.issued['command'].get('name'))
        return {n for n in names if n}

    def transition(self, world, status, step=None, reason=None, **extra):
        old = self.status
        if old == status and (step is None or step == self.step) and reason == self.reason:
            return False
        self.status = status
        if step is not None:
            self.step = step
        self.reason = reason
        entry = dict(round=world.round, status=status, step=self.step, reason=reason, **extra)
        self.history.append(entry)
        self.history = self.history[-HISTORY_LIMIT:]
        emit(world, self, 'transition', previous=old, **extra)
        return True

    def block(self, world, layer, reason):
        """Record the first layer actually stopping this work's next step."""
        if layer not in BLOCK_LAYERS:
            layer = 'strategy'
        if self.blocked_since is None or self.blocked_by != layer:
            self.blocked_since = world.round
        first = self.blocked_by != layer or self.reason != reason
        self.blocked_by = layer
        self.reason = reason
        if first:
            emit(world, self, 'blocked', layer=layer, block_reason=reason)

    def unblock(self):
        self.blocked_since = None
        self.blocked_by = None
        self.reason = None


def emit(world, work, event, **fields):
    """Queue one compact work event on this frame's world; diagnostics drains it."""
    events = getattr(world, 'work_events', None)
    if events is None:
        return
    record = dict(event=event, actor=work.actor, work_id=work.work_id, revision=work.revision,
                  status=work.status, step=work.step, deadline=work.deadline,
                  grant=sum(work.grants.values()) if work.grants else 0,
                  blocked_by=work.blocked_by, reason=work.reason,
                  last_confirmed_progress=work.last_confirmed_progress)
    record.update({k: v for k, v in fields.items() if v is not None})
    events.append(record)


def quote_status_for(stage):
    """Map legacy executor blocker diagnostics to a quote status.

    Unknown or missing blockers stay UNKNOWN; a finished computation never
    reports a route/cash verdict it did not actually prove.
    """
    mapping = {'planning_budget_exhausted': BUDGET_EXHAUSTED, 'route_budget': BUDGET_EXHAUSTED,
               'funding_planning_budget': BUDGET_EXHAUSTED, 'planner_budget_exhausted': BUDGET_EXHAUSTED,
               'checkout_return_deadline': DEADLINE, 'return_deadline': DEADLINE,
               'shop_return_deadline': DEADLINE, 'shop_service_return_deadline': DEADLINE,
               'daylight_window_insufficient': DEADLINE,
               'no_service_route': NO_ROUTE, 'route_incomplete': NO_ROUTE,
               'no_complete_quote_check_paid_delivery': NO_ROUTE, 'no_route': NO_ROUTE,
               'capacity': CAPACITY,
               'cash_deficit': CASH_DEFICIT, 'stock_ready_or_funds': CASH_DEFICIT,
               'target_gone': TARGET_INVALID, 'observed_upgraded': TARGET_INVALID}
    return mapping.get(stage or '', UNKNOWN)
