"""Personal liquidation before a final, observed-cash night-stock checkout."""
from dataclasses import dataclass, field
from copy import copy
import time

from .arbitration import Candidate
from .navigation import distance_field, interaction_cells, neighbours
from .protocol import MINERALS, WEAPONS, distance, fingerprint, pos_json
from .day_schedule import day_endpoints
from .night_roles import defender_ids
from . import procurement
from . import procurement_work as pw
from .caretaker_day import CaretakerDay


def moves(actor, route, reason):
    length = route.get(actor.pos)
    if not length:
        return []
    return [Candidate(actor.id, {'action':'move','targetPos':[pos_json(p)]}, 180-i*.01, reason)
            for i,p in enumerate(sorted(p for p in neighbours(actor.pos) if route.get(p,float('inf'))<length)[:4])]


@dataclass
class SunsetMarket:
    day: int | None = None
    started: bool = False
    settled: set = field(default_factory=set)
    pending: dict = field(default_factory=dict)
    delivery_pending: dict = field(default_factory=dict)
    delivery_receipts: list = field(default_factory=list)
    diagnostic: dict = field(default_factory=dict)
    upgrade_travellers: set = field(default_factory=set)
    upgrade_owner: str | None = None
    checkout_intents: dict = field(default_factory=dict)
    checkout_targets: dict = field(default_factory=dict)
    checkout_context: tuple = ()
    checkout_deliveries: set = field(default_factory=set)
    checkout_stage: tuple = ()
    checkout_primary: str | None = None
    maintenance_assignment: dict = field(default_factory=dict)
    caretaker_day: CaretakerDay = field(default_factory=CaretakerDay)
    # Authoritative W procurement work records (2026-09-19 design §4.3).
    works: dict = field(default_factory=dict)          # work_id -> ProcurementWork
    work_seq: dict = field(default_factory=dict)       # (actor, purpose) -> int
    work_offers: dict = field(default_factory=dict)    # this frame: (actor, command fingerprint) -> work_id
    recent_receipts: dict = field(default_factory=dict)  # actor -> last purchase receipt outcome
    reconciled_round: int = -1

    def begin_frame(self, world):
        """Per-frame init before any consumer; publishes the work views."""
        if getattr(world, 'work_events', None) is None:
            world.work_events = []
        self.work_offers = {}
        for work in self.works.values():
            if work.status not in pw.TERMINAL:
                work.grants = {}
        world.procurement_market = self
        world.procurement_works = list(self.works.values())
        world.committed_work_ids = {w.work_id for w in self.works.values()
                                    if w.status in (pw.ACTIVE, pw.WAIT_RECEIPT)}

    def active_work(self, actor):
        """The actor's currently executing trip; suspended works only await resume."""
        for work in self.works.values():
            if work.actor == actor and work.status in (pw.PROPOSED, pw.ACTIVE, pw.WAIT_RECEIPT):
                return work
        return None

    def work_for(self, world, actor, purpose, *, deadline=None, preempt=False, preempt_reason=None):
        # A same-purpose work resumes regardless of suspension; it is the same trip.
        for work in self.works.values():
            if work.actor == actor and work.purpose == purpose and work.status not in pw.TERMINAL:
                return work
        existing = self.active_work(actor)
        if existing is not None:
            if not preempt or existing.status == pw.WAIT_RECEIPT:
                return None  # A receipt in flight is never preempted (§5.3).
            existing.preemption = dict(reason=preempt_reason or purpose, round=world.round,
                                       resume='requote after '+purpose)
            existing.transition(world, pw.SUSPENDED, reason='preempted_by_'+purpose)
        seq = self.work_seq.get((actor, purpose), 0) + 1
        self.work_seq[(actor, purpose)] = seq
        work = pw.ProcurementWork(work_id=f'{actor}:{purpose}:{seq}', epoch=getattr(world, 'session_epoch', 0),
                                  actor=actor, purpose=purpose, created=world.round,
                                  last_confirmed_progress=world.round, deadline=deadline)
        self.works[work.work_id] = work
        pw.emit(world, work, 'created', purpose=purpose)
        return work

    def attach_quote(self, world, work, quote):
        """Quote evidence attaches to the work; never marks success (§6.2)."""
        work.quote = quote
        work.revision += 1
        work.deadline = quote.deadline or work.deadline
        if quote.status != pw.FEASIBLE:
            layer = 'funding' if quote.status == pw.CASH_DEFICIT else \
                    'target' if quote.status == pw.TARGET_INVALID else 'quote'
            work.block(world, layer, quote.reason or quote.status)
        else:
            work.unblock()
        return quote

    def propose_step(self, world, work, candidates, step):
        """Candidates carry the work reference; no execution fact advances here.

        Offering a step never commits the trip: PROPOSED holds no cross-round
        exclusivity (§5). Only record_selected after final arbitration commits.
        """
        work.step = step
        for candidate in candidates:
            candidate.work_id = work.work_id
            self.work_offers[(candidate.actor, fingerprint(candidate.command))] = work.work_id
            if candidate.command.get('action') == 'buy':
                self._commit_purchase_reserve(world, candidate)
        if candidates:
            pw.emit(world, work, 'proposed', next_action=candidates[0].command.get('action'),
                    item=candidates[0].command.get('name'))
        return candidates

    @staticmethod
    def _commit_purchase_reserve(world, candidate):
        """Apply at creation what the purchase gate used to mutate mid-permit (E4).

        Mirrors purchase_permitted's branch order exactly: branches that used
        to return True without mutating still leave the reserve untouched.
        """
        from .wall_policy import investment_fund, minimum_stock
        command = candidate.command
        name = command.get('name', '')
        actor = world.ours.get(candidate.actor)
        if actor is None:
            return
        if (not getattr(world, 'staged_walls', False)
                or not getattr(getattr(world, 'strategy_policy', None), 'upgrade_commitment_enabled', True)):
            return
        from .funding import item_granted
        if item_granted(world, actor.id, name, command.get('num', 1)):
            return
        if 'UpgradeVoucher' in name:
            return
        if name == 'Medicine':
            from .medical import needs_treatment
            clock = getattr(world, 'strategy_clock', None)
            if (actor.health <= (200 if actor.kind == 'pioneer' else 220)*.5
                    or clock and needs_treatment(world, actor, clock)):
                return
        if name == 'WallFixer' and getattr(world, 'critical_base_ids', ()):
            return
        guard = getattr(world, 'essential_guard_stock', {}).get((actor.id, name), {})
        repair = getattr(world, 'essential_repair_stock', {}).get(actor.id, {})
        if (actor.id == getattr(getattr(world, 'night_roster', None), 'w', None)
                and name in {'DizzyWeapon', 'Bomb'} and actor.inventory[name] < 1
                and guard.get('round') == world.round and guard.get('price') == world.shop.get(name)
                and command.get('num', 1) == 1 == guard.get('count')):
            candidate.gold_reserve = max(candidate.gold_reserve, getattr(world, 'treasure_reserved_gold', 0))
            return
        if name == 'WallFixer' and repair.get('round') == world.round \
                and 0 < command.get('num', 1) <= repair.get('count', 0):
            candidate.gold_reserve = max(candidate.gold_reserve, getattr(world, 'treasure_reserved_gold', 0))
            return
        if name.endswith('SummonOrder'):
            return
        minimum = max(0, minimum_stock(world, actor, name) - actor.inventory[name])
        reserve, item = investment_fund(world, preserve_reconstruction=command.get('num', 1) > minimum)
        candidate.gold_reserve = max(candidate.gold_reserve, reserve)
        candidate.gold_reserve_item = item

    def track(self, world, candidate, purpose, *, step='checkout', deadline=None, preempt=False,
              preempt_reason=None):
        """Single entry for any migrated W buy candidate; no separate pending."""
        work = self.work_for(world, candidate.actor, purpose, deadline=deadline, preempt=preempt,
                             preempt_reason=preempt_reason)
        if work is None:
            return None
        self.attach_quote(world, work, pw.Quote(
            status=pw.FEASIBLE, round=world.round, actor=candidate.actor,
            orders={candidate.command['name']: candidate.command.get('num', 1)},
            cost=world.shop.get(candidate.command.get('name'), 0) * candidate.command.get('num', 1),
            deadline=deadline, source=purpose))
        return self.propose_step(world, work, [candidate], step)[0]

    def purchase_pending(self, world, actor):
        return actor in self.pending

    def receipt_outcome(self, world, actor):
        """Read-only last purchase receipt evidence for migrated budget keepers."""
        row = self.recent_receipts.get(actor)
        return row if row and world.round <= row['round'] + 1 else None

    def rebind_delivery(self, world, old_id, new_wall, extra_bindings=None):
        """WallRebuild supplies the new wall instance; bindings rebind here (§3)."""
        for owner, targets in list(self.checkout_targets.items()):
            self.checkout_targets[owner] = [(new_wall.id if uid == old_id else uid, level)
                                            for uid, level in targets]
        for owner, targets in (extra_bindings or {}).items():
            saved = self.checkout_targets.setdefault(owner, [])
            for _, level in targets:
                if (new_wall.id, level) not in saved:
                    saved.append((new_wall.id, level))
        for work in self.works.values():
            if work.bindings:
                work.bindings = [(new_wall.id if uid == old_id else uid, level)
                                 for uid, level in work.bindings]

    def publish_checkout_targets(self,world):
        """Expose live paid ownership before repair and night candidates run."""
        from .wall_rebuild import upgrade_ready
        if not hasattr(world,'wall_delivery_waits'):world.wall_delivery_waits={}
        released={i:[(uid,level) for uid,level in targets if uid in world.ours
                     and world.ours[uid].kind=='wall' and not upgrade_ready(world,world.ours[uid])]
                  for i,targets in self.checkout_targets.items()}
        for identity,targets in released.items():
            if targets:
                self.checkout_intents.pop(identity,None)
                getattr(world,'wall_delivery_waits',{}).update({identity:dict(reason='waiting_for_rebuild',released_targets=targets)})
        self.checkout_targets={i:[(uid,level) for uid,level in targets
            if uid in world.ours and world.ours[uid].alive
            and world.ours[uid].level is not None and world.ours[uid].level<=level
            and (world.ours[uid].kind!='wall' or upgrade_ready(world,world.ours[uid]))]
            for i,targets in self.checkout_targets.items() if i in world.ours and world.ours[i].alive}
        world.checkout_targets=self.checkout_targets
        # The filtered table above is authoritative; sync work bindings so a
        # target completed via any entry releases the work (design §4.3/§5.6).
        live={binding for targets in self.checkout_targets.values() for binding in targets}
        for work in self.works.values():
            if work.status in pw.TERMINAL or not work.bindings:
                continue
            kept=[b for b in work.bindings if b in live]
            if len(kept)!=len(work.bindings):
                dropped=[b for b in work.bindings if b not in live]
                work.bindings=kept
                pw.emit(world,work,'bindings_released',dropped=len(dropped))
        job=self.maintenance_assignment
        target=world.ours.get(job.get('target'))
        clock=getattr(world,'strategy_clock',None)
        if job and (not target or not target.alive or target.level!=job['level']
                    or world.phase_task or not clock or clock.phases!={'day'}
                    or world.round-job['selected_round']>3):
            self.maintenance_assignment={};job={}
        if job:
            # Health eligibility is rechecked by the maintenance producer.
            world.maintenance_targets=dict(getattr(world,'maintenance_targets',{}))
            world.maintenance_targets[target.id]=job['owner']

    def reconcile_work(self, world, clock):
        """Receipt-driven work progression (design §5/§6.2 reconcile_work).

        Consumes last round's purchase/delivery feedback exactly once per frame,
        advances the linked work records, and applies terminal/release rules.
        """
        if self.reconciled_round == world.round:
            return
        self.reconciled_round = world.round
        for identity, order in list(self.pending.items()):
            actor = world.ours.get(identity)
            feedback = world.raw.get('lastRoundRoleActionResults', {})
            failed = (world.round == order['round']+1 and isinstance(feedback,dict)
                      and feedback.get(identity) is False)
            known=bool(actor and actor.backpack is not None)
            if known:
                current=actor.inventory[order['name']]
                order['received']=order.get('received',0)+max(0,current-order.get('last_inventory',order['prior']))
                order['last_inventory']=current
            delta=order.get('received',0)
            settled=(world.round==order['round']+1 and isinstance(feedback,dict)
                     and feedback.get(identity) is True and known)
            arrived=delta>=order.get('num',1) or settled and delta>0
            if (arrived or failed) and delta and identity in self.checkout_intents:
                intent=self.checkout_intents[identity]
                name=order['name']
                intent[name]=max(0,intent.get(name,0)-delta)
                if not any(intent.values()):
                    self.checkout_intents.pop(identity)
                    self.checkout_deliveries.add(identity)
            absent=(world.round>order['round']+1 and known and delta==0
                    and actor.inventory[order['name']]<=order['prior'])
            if failed or arrived or absent:
                outcome='failed' if failed else 'confirmed' if arrived else 'unknown'
                self.recent_receipts[identity]=dict(round=world.round,outcome=outcome,
                    name=order['name'],price=order.get('price',0),day=order.get('day',clock.day),
                    purpose=order.get('purpose'),received=delta)
                work=self.works.get(order.get('work') or '')
                if work is not None and work.status not in pw.TERMINAL:
                    work.issued=None
                    if arrived:
                        work.confirmed[order['name']]=work.confirmed.get(order['name'],0)+delta
                        work.last_confirmed_progress=world.round
                        work.transition(world,pw.ACTIVE,reason=None)
                    elif failed:
                        # A confirmed failure requotes from the current snapshot;
                        # draft.failed backoff prevents an unconditional resend.
                        work.transition(world,pw.ACTIVE,reason='buy_failed')
                        work.block(world,'receipt','buy_failed')
                    else:
                        # An unknown receipt is not success and never locks W
                        # permanently; the fresh snapshot re-proves any deficit.
                        work.transition(world,pw.ACTIVE,reason='receipt_unknown')
                        work.block(world,'receipt','receipt_unknown')
                    pw.emit(world,work,'receipt',receipt=outcome,item=order['name'],received=delta)
                self.pending.pop(identity)
        for identity,order in list(self.delivery_pending.items()):
            if world.round<=order['round']:continue
            actor=world.ours.get(identity);target=world.ours.get(order['target'])
            feedback=world.raw.get('lastRoundRoleActionResults',{})
            failed=world.round==order['round']+1 and isinstance(feedback,dict) and feedback.get(identity) is False
            changed=not target or not target.alive or target.pos!=order['position'] or target.level!=order['level']
            consumed=bool(actor and actor.backpack is not None and actor.inventory[order['name']]<order['prior'])
            not_applied=bool((world.round>order['round']+1 or not isinstance(feedback,dict)
                or identity not in feedback) and actor and actor.alive
                and actor.backpack is not None and actor.inventory[order['name']]==order['prior']
                and target and target.alive and target.pos==order['position'] and target.level==order['level'])
            if failed or changed or consumed or not_applied:
                confirmed=bool(not failed and consumed and target and target.alive and target.pos==order['position']
                               and target.level==order['level']+1)
                self.delivery_receipts.append(dict(actor=identity,item=order['name'],target=order['target'],
                    issued=order['round'],observed=world.round,consumed=consumed,
                    confirmed=confirmed,failed=failed,not_applied=not_applied))
                self.delivery_receipts=self.delivery_receipts[-40:]
                work=self.works.get(order.get('work') or '')
                if work is not None and work.status not in pw.TERMINAL:
                    work.issued=None
                    if confirmed:
                        work.last_confirmed_progress=world.round
                        work.bindings=[b for b in work.bindings
                                       if not (b[0]==order['target'])]
                        work.transition(world,pw.ACTIVE,step=work.step,reason=None)
                    else:
                        work.block(world,'receipt','delivery_failed' if failed else
                                   'target_changed' if changed else 'delivery_not_applied')
                    pw.emit(world,work,'receipt',receipt='confirmed' if confirmed else
                            'failed' if failed else 'changed' if changed else 'not_applied',
                            item=order['name'],target=order['target'])
                self.delivery_pending.pop(identity)
        world.checkout_use_pending=set(self.delivery_pending)
        world.checkout_pending_actors=set(self.pending)
        self._sweep_works(world, clock)

    def _sweep_works(self, world, clock):
        """Terminal rules: observed completion, invalidation, bounded history."""
        day_changed=self.day is not None and clock.day is not None and self.day!=clock.day
        rebuild_plan=getattr(world,'wall_rebuild_plan',None)
        for work in list(self.works.values()):
            if work.status in pw.TERMINAL:
                continue
            actor=world.ours.get(work.actor)
            if actor is None or not actor.alive or actor.backpack is None:
                # Death/replacement: the old role's inventory stays with it;
                # the successor takes new work with its own resources (§5.7).
                work.transition(world,pw.CANCELLED,reason='actor_unavailable')
                continue
            if work.issued or work.actor in self.pending or work.actor in self.delivery_pending:
                continue  # Receipts in flight; never judge from a missing observation.
            if work.bindings:
                continue  # Paid delivery obligations survive pause and target change.
            orders=dict(work.quote.orders) if work.quote else {}
            if orders and all(work.confirmed.get(name,0)>=num for name,num in orders.items()):
                work.transition(world,pw.DONE,reason='confirmed')
                continue
            if work.purpose=='emergency_medical' and (actor.inventory['Medicine'] or actor.health>=220):
                work.transition(world,pw.DONE,reason='observed_stock_or_health')
                continue
            if work.purpose=='wall_rebuild_supply' and not rebuild_plan:
                work.transition(world,pw.CANCELLED,reason='rebuild_plan_released')
                continue
            if day_changed and work.purpose in ('day_checkout','night_stock','upgrade_chain'):
                work.transition(world,pw.CANCELLED,reason='day_expired')
                continue
            if clock.phases=={'day'} and work.purpose in ('night_clear_resupply','night_attack_stock'):
                work.transition(world,pw.CANCELLED,reason='night_ended')
                continue
            last=max(work.created,work.last_confirmed_progress,
                     work.quote.round if work.quote else 0,work.blocked_since or 0)
            if world.round-last>130:
                work.transition(world,pw.CANCELLED,reason='stale_unprogressed')
        terminal=[w for w in self.works.values() if w.status in pw.TERMINAL]
        if len(terminal)>24:
            for work in sorted(terminal,key=lambda w:w.history[-1]['round'] if w.history else 0)[:-24]:
                self.works.pop(work.work_id,None)
        world.procurement_works=list(self.works.values())
        world.committed_work_ids={w.work_id for w in self.works.values()
                                  if w.status in (pw.ACTIVE,pw.WAIT_RECEIPT)}

    def cancel_for_critical_base(self, world):
        """A new survival priority overrides unpaid routine purchases (§5.5).

        Mirrors the legacy checkout clearing: unpaid work cancels; a work whose
        paid bindings were just cleared stays active on inventory facts and
        re-matches its targets after the emergency.
        """
        for work in self.works.values():
            if work.status in pw.TERMINAL or work.purpose == 'emergency_medical':
                continue
            if work.issued or work.actor in self.pending or work.actor in self.delivery_pending:
                continue  # Paid responsibility and in-flight receipts are kept.
            if work.bindings:
                work.bindings = []
                work.transition(world, pw.ACTIVE, reason='critical_base_bindings_released')
                continue
            work.transition(world, pw.CANCELLED, reason='critical_base_survival_priority')

    def prepare(self, world, clock, rules, policy, guidance, jobs, excluded, deadline):
        guidance.market_permit = lambda candidate: permits(world, candidate)
        world.pioneer_trade_stands = guidance.operator_stands
        world.sunset_actions = {}
        world.sunset_buyer = None
        world.checkout_cash_reserve = 0
        # NightClear runs before this planner. Preserve its current-round,
        # route-validated authorization even when daylight work is inactive.
        world.essential_repair_stock = {i:r for i,r in getattr(world,'essential_repair_stock',{}).items()
                                        if r.get('round')==world.round}
        self.diagnostic = {'stage':'inactive'}
        self.reconcile_work(world, clock)
        if self.day != clock.day:
            self.day = clock.day; self.started = False; self.settled.clear()
            self.upgrade_travellers.clear()
            self.upgrade_owner=None
            self.checkout_intents.clear()
            # A new day expires the route/quote, not personally paid delivery.
        from .day_access import gate as access_gate
        context=(world.gold,access_gate(world),
            tuple(sorted((u.id,u.pos,u.level,u.health) for u in world.ours.values() if u.alive and u.kind in WEAPONS|{'wall','station'})),
            tuple(sorted((u.id,tuple(sorted((k,n) for k,n in u.inventory.items()
                if k in {'Medicine','WallFixer','DizzyWeapon','Bomb'})))
                for u in world.movers if u.backpack is not None)),tuple(sorted(i for i in excluded if i is not None)),
            tuple(sorted(getattr(world,'guard_attack_targets',{}).items())))
        if self.checkout_context and context!=self.checkout_context:
            for identity in list(self.checkout_intents):
                actor=world.ours.get(identity)
                # A paid upgrade chain finishes its existing checkout and
                # delivery before enlarging the basket. Ore collection and
                # its own purchase receipts are not new unfunded demand.
                if actor and any(n and 'UpgradeVoucher' in k for k,n in actor.inventory.items()):continue
                if context[1:]!=self.checkout_context[1:] or (world.gold or 0)>(self.checkout_context[0] or 0):
                    self.checkout_intents.pop(identity,None)
        self.checkout_context=context
        from .wall_policy import purchase_units
        stage=tuple(sorted((u.kind,u.pos,u.level) for u in purchase_units(world)))
        if self.checkout_stage and stage!=self.checkout_stage:
            # A confirmed weapon receipt can unlock the wall stage while the
            # carrier is still at the counter. Requote there with the same
            # route/deadline checks; never turn an underway paid delivery back.
            for actor in world.movers:
                if world.near_zone(actor.pos,'weaponShop'):
                    self.checkout_intents.pop(actor.id,None)
                    self.checkout_deliveries.discard(actor.id)
        self.checkout_stage=stage
        if getattr(world,'critical_base_ids',()):
            self.checkout_intents.clear()  # New survival priority overrides routine purchases.
            self.checkout_targets.clear()
            self.cancel_for_critical_base(world)
        world.checkout_order_limits=dict(self.checkout_intents)
        self.checkout_deliveries={i for i in self.checkout_deliveries if i in world.ours
            and any(n and 'UpgradeVoucher' in k for k,n in world.ours[i].inventory.items())}
        for actor in world.movers:
            if actor.id not in self.checkout_intents and actor.id in self.checkout_deliveries:
                world.checkout_order_limits[actor.id]={}
        self.publish_checkout_targets(world)
        world.checkout_pending_actors=set(self.pending)
        world.quoted_checkout_targets={}
        if (not policy.day_schedule_enabled or clock.phases != {'day'} or clock.day is None
                or not world.phase_task_observed or time.monotonic()>=deadline):
            return []
        from .defence_duties import enabled
        if enabled(world):
            if policy.staged_walls_enabled and policy.upgrade_commitment_enabled:
                roster = world.night_roster
                if (getattr(world,'news_task_hold',False) and roster.p not in self.pending
                        and roster.p in world.ours and not any(n and 'UpgradeVoucher' in k
                            for k,n in world.ours[roster.p].inventory.items())):
                    excluded=set(excluded)|{roster.p}  # Only new, unpaid departures wait for dawn analysis.
                free = {i for i in (roster.w,roster.p) if i not in excluded and i in world.ours
                        and world.ours[i].alive and world.ours[i].backpack is not None}
                # The leader budgets unassigned purchases; per-building grants
                # also let W carry its own wall work during P's shopping trip.
                # A free pioneer may still be trapped behind the worker at C.
                # Assign checkout only to a guard with an observed route to
                # the shop; otherwise both reserve work for an immobile P.
                reachable = {i for i in free if world.ours[i].pos in distance_field(
                    world, interaction_cells(world,world.zones.get('weaponShop',()),world.ours[i].pos),
                    world.ours[i].pos,deadline)}
                if (self.upgrade_owner == roster.w and self.caretaker_day.day == clock.day
                        and self.caretaker_day.phase in {'close','use'}):
                    # W has left checkout. Its personal vouchers remain
                    # reserved, but a free P can spend newly received gold.
                    self.upgrade_owner = None
                owner = self.upgrade_owner if self.upgrade_owner in reachable else None
                at_shop = [i for i in reachable if world.near_zone(world.ours[i].pos,'weaponShop')]
                if (owner is not None and owner not in at_shop and at_shop
                        and owner not in self.pending
                        and (owner in self.checkout_deliveries or not any(n and 'UpgradeVoucher' in name
                            for name,n in world.ours[owner].inventory.items()))):
                    # An unpaid traveller cannot reserve the counter while a
                    # free teammate is already there. A completed checkout's
                    # vouchers keep their personal targets while new unpaid
                    # investment passes to the counter. Pending receipts wait.
                    owner=min(at_shop)
                if owner is None:
                    owner = min(at_shop) if at_shop else roster.p if roster.p in reachable else roster.w
                funded=[r['owner'] for r in getattr(world,'funding_plan',()) if r['purpose'].endswith('_upgrade')
                        and r['granted']==r['cost'] and r['owner'] in reachable]
                if funded and owner not in self.checkout_deliveries and owner not in self.pending:
                    owner=min(funded,key=lambda i:(i not in at_shop,i!=roster.p,i))
                if owner != self.checkout_primary and owner in world.ours:
                    buyer = world.ours[owner]
                    if (owner not in self.pending and not any(n and 'UpgradeVoucher' in name
                            for name,n in buyer.inventory.items())):
                        # A personal-stock quote made as the secondary buyer
                        # must not cap the newly assigned investment checkout.
                        self.checkout_intents.pop(owner,None)
                        world.checkout_order_limits.pop(owner,None)
                self.checkout_primary = owner
                world.upgrade_checkout_actor = owner
                emergency = [u for u in world.stations if u.id in getattr(world,'critical_base_ids',()) and u.level in (1,2)]
                worker = world.ours.get(roster.w)
                worker_rescue = bool(emergency and worker and (owner == roster.w or any(
                    worker.inventory[f'StationUpgradeVoucher{u.level}'] for u in emergency)))
                # Each free guard gets a bounded share. A complex wall tour
                # must not consume the pioneer's entire shopping search.
                worker_deadline=(time.monotonic()+max(0,deadline-time.monotonic())*.5
                                 if roster.p in free and not (owner==roster.w and roster.w in at_shop) else deadline)
                daily = (None if worker_rescue else
                         self.caretaker_day.prepare(world,clock,rules,policy,guidance,jobs,excluded,worker_deadline))
                from .upgrade_dispatch import prepare
                owned = {world.night_roster.w} if daily is not None else set()
                upgrades=prepare(self,world,clock,rules,policy,guidance,jobs,excluded | owned,deadline)
                if daily is not None:
                    self.diagnostic['worker_day'] = dict(self.caretaker_day.diagnostic)
                    return daily + (upgrades or [])
                if upgrades is not None:return upgrades
            return self.worker_stock(world,clock,rules,policy,guidance,jobs,excluded,deadline)
        # Without a live pioneer, retain the workers' existing complete
        # sale/purchase/delivery schedule and emergency replacement duties.
        if not any(a.kind=='pioneer' for a in world.movers):
            return []
        # Thirty rounds is a policy window, not a game rule. Long sale/return
        # routes open it earlier; each circuit is checked against today's map.
        pioneers = [a for a in world.movers if a.kind=='pioneer' and a.id not in excluded
                    and a.backpack is not None and a.capacity is not None and len(a.backpack)<a.capacity
                    and world.phase_task_observed and not world.phase_task]
        buyer = pioneers[0] if len(pioneers)==1 else None
        margin = policy.return_buffer+(8 if world.defence_cells else 0)
        home_fields = {}
        def home(actor):
            if actor.id not in home_fields:
                goals = ({guidance.operator_stands[actor.id]} if actor.id in guidance.operator_stands else set())
                if actor.kind=='worker':
                    goals,_ = day_endpoints(world,actor,guidance.operator_stands)
                elif actor.kind=='pioneer' and not goals and not policy.pioneer_defence_enabled:
                    # Disabling automatic night defence does not remove the
                    # daytime trader's physical home route.
                    from .pioneer_trade import home_field
                    home_fields[actor.id] = home_field(world,actor,deadline)
                    return home_fields[actor.id]
                home_fields[actor.id] = distance_field(world,goals,actor.pos,deadline)
            return home_fields[actor.id]
        def circuits(actor, zone, actions):
            start = distance_field(world,[actor.pos],actor.pos,deadline)
            back = home(actor)
            return sorted((start[q]+actions+back[q],start[q],q,back[q])
                          for q in interaction_cells(world,world.zones.get(zone,()),actor.pos)
                          if q in start and q in back)
        checkout_actions = 1  # Replan each observed purchase; a four-item basket is not mandatory.
        shopping = circuits(buyer,'weaponShop',checkout_actions) if buyer else []
        shopping = [r for r in shopping if r[0]+margin<clock.until_night]
        if not shopping:
            buyer = None
        if (buyer and buyer.id not in self.pending and not self.order(world,buyer,rules,policy,deadline)
                and any(a.kind=='worker' and a.id in defender_ids(world) and a.id not in excluded
                        and self.order(world,a,rules,policy,deadline) for a in world.movers)):
            buyer=None  # A stocked pioneer must release W's personal upgrade/supply checkout.
        if buyer is None:
            # W may finish a short shop stop on its actual return circuit.
            # Never recruit the exterior miner or interrupt construction,
            # traffic, or a carried-voucher delivery to manufacture a buyer.
            options=[]
            for worker in world.movers:
                if (worker.kind!='worker' or worker.id not in defender_ids(world)
                        or worker.id in excluded or (jobs.get(worker.id) and not jobs[worker.id].get('gate'))
                        or worker.id not in guidance.operator_stands
                        or guidance.return_routes.get(worker.id,{}).get('due')
                        or worker.backpack is None or worker.capacity is None
                        or len(worker.backpack)>=worker.capacity
                        or any('UpgradeVoucher' in k and n for k,n in worker.inventory.items())):
                    continue
                paths=[r for r in circuits(worker,'weaponShop',1) if r[0]+margin<clock.until_night]
                if paths:options.append((paths[0][0],worker.id,worker,paths))
            if options:
                _,_,buyer,shopping=min(options,key=lambda o:o[:2])
                checkout_actions=1
        rows = {}; empty_workers = set()
        from .economy import construction_reservations
        reserves = construction_reservations(world,rules,jobs=jobs)
        for actor in world.movers:
            if actor.id in excluded or actor.backpack is None:
                continue
            if (getattr(world,'task_side_plan',None) and actor.id==world.night_roster.m
                    and clock.until_night<=35):
                continue  # Exterior miner cashes out after dawn, not before dusk.
            reserve = reserves.get(actor.id,{})
            # External gate preparation removes M's ordinary job. Its personal
            # stones still fund remaining walls and the next daily gate.
            stone = reserve.get('stone',0)
            if actor.kind=='worker' and actor.id==getattr(world.night_roster,'m',None):
                walls={u.pos for u in world.ours.values() if u.alive and u.kind=='wall'}
                stone=max(stone,len((world.wall_targets or set())-walls),1 if clock.day<10 and getattr(world,'task_side_plan',None) else 0)
            stock={k:max(0,actor.inventory[k]-(stone if k=='stone' else reserve.get(k,0)))
                   for k in sorted(MINERALS) if world.vendor.get(k,0)>0}
            stock={k:n for k,n in stock.items() if n}
            if not stock:
                if (actor.kind=='worker' and not any('UpgradeVoucher' in k and n for k,n in actor.inventory.items())
                        and actor.inventory['stone']>=stone
                        and (not jobs.get(actor.id) or jobs[actor.id].get('gate'))):
                    empty_workers.add(actor.id)
                continue
            paths=circuits(actor,'vendor',len(stock))
            if time.monotonic()>=deadline:
                self.diagnostic={'stage':'route_budget'};return []
            if paths:
                rows[actor.id]=(actor,stock,paths)
        earliest = max((paths[0][0]+margin+4 for _,_,paths in rows.values()),default=0)
        if shopping:
            _,travel,_,back=shopping[0]
            last_sale=max((paths[0][1]+len(stock) for _,stock,paths in rows.values()),default=0)
            earliest=max(earliest,max(travel,last_sale)+checkout_actions+back+margin)
        ready_purchase=bool(buyer and self.order(world,buyer,rules,policy,deadline))
        dawn_sale=bool(getattr(world,'task_side_plan',None) and world.night_roster.m in rows
                       and clock.day>1 and clock.until_night>35)
        if not self.started and clock.until_night>max(30,earliest) and not ready_purchase and not dawn_sale:
            return []
        self.started = True
        self.settled.update(empty_workers)
        self.diagnostic={'stage':'cashout','sellers':{},'buyer':buyer.id if buyer else None,
                         'fallback_worker':bool(buyer and buyer.kind=='worker'),
                         'blocked':{a.id:('reserved_duty' if a.id in excluded else
                             'no_return_stand' if a.id not in guidance.operator_stands else 'route_or_stock')
                             for a in world.movers if a.id in defender_ids(world)} if not buyer else {}}
        result=[];waiting=[]
        def install(actor, choices, stage):
            choices=[c for c in choices if guidance.permit(c)]
            if choices or stage=='wait_sales':
                world.sunset_actions[actor.id]=[c.command for c in choices]
                result.extend(choices)
                return True
            return False
        for identity,(actor,stock,paths) in rows.items():
            if actor.kind=='worker' and jobs.get(identity) and not jobs[identity].get('gate'):
                self.diagnostic['sellers'][identity]='construction';continue
            paths=[r for r in paths if r[0]+margin<clock.until_night]
            if not paths:
                self.diagnostic['sellers'][identity]='return_deadline';continue
            total,length,stand,back=paths[0]
            choices=([Candidate(identity,{'action':'sell','name':name,'num':stock[name]},180,
                                'sell personal surplus before final checkout')]
                     if length==0 and (name:=max(stock,key=lambda k:(stock[k]*world.vendor[k],k))) else
                     moves(actor,distance_field(world,[stand],actor.pos,deadline),'cash out personal surplus before night'))
            if install(actor,choices,'cashout'):
                self.settled.add(identity)
                # Wait only for sales that can finish before the buyer's last safe
                # checkout. Forecasts coordinate time, never spendable gold.
                if buyer and length+len(stock)+shopping[0][3]+checkout_actions+margin<clock.until_night:
                    waiting.append(identity)
                self.diagnostic['sellers'][identity]={'stock':stock,'steps':length+len(stock)}
            else:
                self.diagnostic['sellers'][identity]='duty_precedes_sale'
        # Once a worker has liquidated, do not restart mining before dusk and
        # strand a fresh batch. Builders can still finish their reserved walls.
        for identity in sorted(self.settled-set(rows)-set(excluded)-({buyer.id} if buyer else set())):
            if getattr(world,'task_side_plan',None) and identity==world.night_roster.m:continue
            # The due route already prevents restarting economic work. Its
            # gate staging/yield destination can differ from day_endpoints;
            # an empty market home lock must not cancel that real movement.
            if guidance.return_routes.get(identity,{}).get('due'):
                continue
            actor=world.ours.get(identity)
            if (actor and actor.alive and actor.kind=='worker'
                    and not any('UpgradeVoucher' in k and n for k,n in actor.inventory.items())):
                choices=moves(actor,home(actor),'surplus sold: return to assigned evening duty')
                if not choices and actor.pos in home(actor):
                    world.sunset_actions[identity]=[]
                else:
                    install(actor,choices,'home')
        if buyer:
            world.sunset_buyer=buyer.id
            world.pioneer_trade_ids.discard(buyer.id)
            if buyer.id not in world.sunset_actions:
                total,length,stand,back=shopping[0]
                route=distance_field(world,[stand],buyer.pos,deadline)
                if waiting:
                    choices=moves(buyer,route,'designated buyer approaches checkout while personal sales settle')
                    # A real return deadline is never replaced by a wait.
                    if not guidance.return_routes.get(buyer.id,{}).get('due'):
                        install(buyer,choices,'wait_sales')
                        self.diagnostic['stage']='wait_sales'
                        self.diagnostic['waiting']=waiting
                elif buyer.id in self.pending:
                    install(buyer,moves(buyer,home(buyer),'purchase unresolved: return with observed inventory'),'home')
                    self.diagnostic['stage']='purchase_pending'
                else:
                    order=self.order(world,buyer,rules,policy,deadline)
                    if order:
                        name,num,reserve=order
                        choices=([Candidate(buyer.id,{'action':'buy','name':name,'num':num},180,
                                            'final checkout with observed team gold',gold_reserve=reserve)]
                                 if length==0 else moves(buyer,route,'designated buyer night-stock checkout'))
                        if buyer.id==world.night_roster.w:
                            work=self.work_for(world,buyer.id,'night_stock',deadline=world.round+clock.until_night)
                            if work is None:
                                choices=[]
                                self.diagnostic.update(stage='checkout',blocked='procurement_work_busy')
                            else:
                                self.attach_quote(world,work,pw.Quote(status=pw.FEASIBLE,round=world.round,
                                    actor=buyer.id,orders={name:num},cost=world.shop.get(name,0)*num,
                                    reserve=reserve,deadline=world.round+clock.until_night,source='checkout'))
                                choices=self.propose_step(world,work,choices,'checkout')
                        if choices:
                            install(buyer,choices,'checkout')
                            self.diagnostic.update(stage='checkout',item=name,num=num,gold=world.gold)
                        elif buyer.id==world.night_roster.w:
                            self.diagnostic['blocked']='procurement_work_busy'
                    else:
                        choices=moves(buyer,home(buyer),'night stock ready or unaffordable: return before dusk')
                        if not choices and buyer.pos in home(buyer):world.sunset_actions[buyer.id]=[]
                        else:install(buyer,choices,'home')
                        self.diagnostic['stage']='home'
        if time.monotonic()>=deadline:
            world.sunset_actions={};world.sunset_buyer=None
            self.diagnostic={'stage':'route_budget'}
            return []
        return result

    def worker_stock(self, world, clock, rules, policy, guidance, jobs, excluded, deadline):
        """Fund personal night supplies early, before construction uses the day.

        Voucher delivery retains its existing complete route planner. This
        short checkout owns only the supplies consumed by the maintenance W.
        """
        identity=world.night_roster.w
        buyer=world.ours.get(identity)
        self.diagnostic={'stage':'worker_delivery_schedule','buyer':identity}
        if (not buyer or not buyer.alive or identity in excluded
                or buyer.backpack is None or buyer.capacity is None
                or identity not in guidance.operator_stands
                or identity in guidance.recovery_actions
                or identity in getattr(world,'return_recovery_actions',{})):
            self.diagnostic['blocked']='duty_or_inventory';return []
        if identity in self.pending:
            self.diagnostic['blocked']='purchase_pending';return []
        # Keep the full carried-voucher delivery commitment ahead of shopping.
        if any(n and 'UpgradeVoucher' in k for k,n in buyer.inventory.items()):
            self.diagnostic['blocked']='carried_voucher_delivery';return []
        job=jobs.get(identity,{})
        if job and job.get('name')!='wall':
            self.diagnostic['blocked']='weapon_construction';return []
        if job.get('defer_build') and not job.get('gate'):
            self.diagnostic['blocked']='wall_material_project';return []
        from .guard_stock import requirements
        choices=[(name,max(0,target-buyer.inventory[name])) for name,target in requirements(world,buyer,policy)]
        costs=[r.gold for k in WEAPONS if (r:=rules.build_rule(world,k)) is not None]
        reserve=min(costs,default=0)*max(0,rules.weapon_limit-len(world.weapons))
        reserve+=getattr(world,'treasure_reserved_gold',0)
        cash=max(0,(world.gold or 0)-reserve)
        space=buyer.capacity-len(buyer.backpack)
        orders=[(name,min(num,space,cash//world.shop[name])) for name,num in choices
                if num and space>0 and world.shop.get(name,0)>0 and cash>=world.shop[name]]
        if not orders:
            self.diagnostic['blocked']='stock_ready_or_funds';return []
        start=distance_field(world,[buyer.pos],buyer.pos,deadline)
        from .defence_duties import rotator
        reserved=({world.task_side_plan['w']} if rotator(world) in world.night_defenders else set())
        back=distance_field(world,[guidance.operator_stands[identity]],buyer.pos,deadline,extra_blocked=reserved)
        shops=interaction_cells(world,world.zones.get('weaponShop',()),buyer.pos)
        # Prove the next actual purchase and return. Recheck later items on
        # their own observed frames; a short window can still fund repair stock
        # even when the entire optional basket would not fit.
        from .rules import station_rings
        _, yellow=station_rings(world.task_side_plan['anchor'])
        # Budget the route after P occupies its assigned stand, not a shortcut
        # through that stand while P is still outside. A partial ring needs no
        # final seal work; the ordinary return margin still applies.
        margin=policy.return_buffer+(8 if set(world.wall_targets or ())==yellow else 0)
        actions=1
        paths=sorted((start[q]+actions+back[q],start[q],q) for q in shops & start.keys() & back.keys()
                     if start[q]+actions+back[q]+margin<=clock.until_night)
        if not paths or time.monotonic()>=deadline:
            self.diagnostic['blocked']='checkout_return_deadline';return []
        total,length,stand=paths[0]
        name,num=orders[0]
        candidates=([Candidate(identity,dict(action='buy',name=name,num=num),180,
                               'maintenance worker buys personal night supplies',gold_reserve=reserve)]
                    if not length else moves(buyer,distance_field(world,[stand],buyer.pos,deadline),
                                              'maintenance worker stocks supplies before dusk'))
        work=self.work_for(world,identity,'night_stock',deadline=world.round+clock.until_night)
        if work is None:
            self.diagnostic['blocked']='procurement_work_busy';return []
        self.attach_quote(world,work,pw.Quote(status=pw.FEASIBLE,round=world.round,actor=identity,
            orders={name:num},cost=world.shop.get(name,0)*num,reserve=reserve,
            deadline=world.round+clock.until_night,required=total,source='worker_stock'))
        candidates=self.propose_step(world,work,candidates,'checkout')
        preview=copy(guidance)
        preview.return_routes={i:r for i,r in guidance.return_routes.items() if i!=identity}
        candidates=[c for c in candidates if preview.permit(c)]
        if not candidates or time.monotonic()>=deadline:
            self.diagnostic['blocked']='duty_or_route_budget';return []
        world.sunset_buyer=identity
        world.sunset_actions[identity]=[c.command for c in candidates]
        # Publish the same commitment to the downstream construction/funding
        # planners; two disjoint whitelists would otherwise reject every move.
        guidance.day_actions[identity]=list(world.sunset_actions[identity])
        guidance.funded_actions[identity]=list(world.sunset_actions[identity])
        self.diagnostic.update(stage='worker_stock_checkout',item=name,num=num,steps=total)
        return candidates

    def order(self, world, buyer, rules, policy, deadline):
        if world.gold is None or buyer.capacity is None or len(buyer.backpack)>=buyer.capacity:
            return None
        # Preserve construction cash; purchases use quotes from this snapshot.
        costs=[r.gold for k in WEAPONS if (r:=rules.build_rule(world,k)) is not None]
        reserve=min(costs,default=0)*max(0,rules.weapon_limit-len(world.weapons))
        reserve += getattr(world,"treasure_reserved_gold",0)
        cash=max(0,world.gold-reserve)
        targets,rank,restricted,_=procurement.upgrade_demand(world,policy,rules=rules)
        actors={a.id:a for a in world.movers if a.backpack is not None}
        fields={}
        def route(actor,target):
            key=actor.id,target['unit'].id
            if key not in fields:
                fields[key]=distance_field(world,interaction_cells(world,[target['unit'].pos],actor.pos),actor.pos,deadline)
            return fields[key]
        targets,_,_=procurement.match_carried_supply(targets,actors,deadline,route)
        stand=[getattr(world,'pioneer_trade_stands',{}).get(buyer.id,buyer.pos)]
        # Night stock belongs to this buyer: only buy for targets reachable
        # from its own assigned stand; never promise an inventory transfer.
        demands=sorted((t for t in targets.values() if (not restricted or t['rank']==rank)
                        and any(distance(p,t['unit'].pos)<=1 for p in stand)),key=lambda t:(t['rank'],t['unit'].id))
        choices=[(t['name'],1) for t in demands]
        walls=[u for u in world.ours.values() if u.alive and u.kind=='wall'
               and any(distance(p,u.pos)<=1 for p in stand)]
        if buyer.id==world.night_roster.w:
            from .guard_stock import requirements
            choices=[(name,max(0,target-buyer.inventory[name]))
                     for name,target in requirements(world,buyer,policy)]+choices
        else:
            choices.append(('Medicine',max(0,1-buyer.inventory['Medicine'])))
        for name,num in choices:
            price=world.shop.get(name)
            if num and price is not None and price>0 and cash>=price:
                return name,min(num,buyer.capacity-len(buyer.backpack),cash//price),reserve
        return None

    def record_selected(self, world, response):
        """Only actually issued commands create pending evidence (§6.2).

        Selection is what commits a work: any offered command that ships moves
        it to ACTIVE (purchases/voucher uses to WAIT_RECEIPT); offered but
        unselected candidates create nothing and the work stays PROPOSED.
        """
        for actor, command in response['roleCommandMap'].items():
            work = self.works.get(self.work_offers.get((actor, fingerprint(command)), ''))
            if work is None or work.status in pw.TERMINAL:
                continue
            action = command.get('action')
            if action == 'buy':
                work.issued = dict(command=dict(command), round=world.round)
                work.preemption = None
                work.transition(world, pw.WAIT_RECEIPT, step='checkout', reason=None)
                pw.emit(world, work, 'selected', next_action='buy', item=command.get('name'),
                        num=command.get('num', 1))
            elif action == 'use' and 'UpgradeVoucher' in command.get('name', ''):
                work.issued = dict(command=dict(command), round=world.round)
                work.preemption = None
                work.transition(world, pw.WAIT_RECEIPT, step='deliver', reason=None)
                pw.emit(world, work, 'selected', next_action='use', item=command.get('name'))
            elif work.status in (pw.PROPOSED, pw.SUSPENDED):
                work.preemption = None
                work.transition(world, pw.ACTIVE, reason=None)

    def finalize(self, world, response):
        self.record_selected(world, response)
        courier=self.diagnostic.get('buyer')
        issued=response['roleCommandMap'].get(courier,{})
        actor=world.ours.get(courier)
        if (self.diagnostic.get('stage')=='day_repair' and actor
                and issued in getattr(world,'sunset_actions',{}).get(courier,())):
            job=self.diagnostic['maintenance'];target=world.ours[job['target']]
            self.maintenance_assignment=dict(job,owner=courier,level=target.level,selected_round=world.round)
        elif courier==self.maintenance_assignment.get('owner'):
            self.maintenance_assignment={}
        if (self.diagnostic.get('stage') in ('upgrade_deliver','upgrade_return')
                and not self.diagnostic.get('basket') and courier not in self.pending
                and issued.get('action') in ('move','use')
                and issued in getattr(world,'sunset_actions',{}).get(courier,())
                and actor and any(n and 'UpgradeVoucher' in name for name,n in actor.inventory.items())):
            # A fitted basket may shrink after its first purchase. Dispatching
            # paid delivery completes checkout even if the old ceiling still
            # contains unbought entries; personal target bindings remain.
            self.checkout_intents.pop(courier,None)
            self.checkout_deliveries.add(courier)
        for actor,command in response['roleCommandMap'].items():
            from .rear_open import enabled as rear_enabled
            maintenance_order=(rear_enabled(world) and actor in (world.night_roster.w,world.night_roster.p)
                               and command.get('name') in {'WallFixer','Medicine','Bomb','DizzyWeapon'})
            work_id=self.work_offers.get((actor,fingerprint(command)))
            work_purpose=self.works[work_id].purpose if work_id in self.works else None
            if command.get('action')=='buy' and ('UpgradeVoucher' in command.get('name','') or maintenance_order
                                                 or work_id):
                self.pending.setdefault(actor,dict(name=command['name'],num=command.get('num',1),
                    prior=world.ours[actor].inventory[command['name']],round=world.round,
                    work=work_id,purpose=work_purpose,price=world.shop.get(command['name'],0),
                    day=getattr(getattr(world,'strategy_clock',None),'day',None)))
            if command.get('action')=='use' and 'UpgradeVoucher' in command.get('name',''):
                target=next((u for u in world.ours.values() if u.pos==tuple(
                    command['targetPos'][0][k] for k in ('x','y'))),None)
                if target and actor not in self.delivery_pending:
                    self.delivery_pending[actor]=dict(name=command['name'],prior=world.ours[actor].inventory[command['name']],
                        target=target.id,position=target.pos,level=target.level,round=world.round,work=work_id)
            if (command.get('action')=='buy' and 'UpgradeVoucher' in command.get('name','')
                    and actor in getattr(world,'quoted_checkout_targets',{})):
                # Preserve the feasible delivery subset, not hypothetical
                # inventory. Matching still consumes only observed vouchers.
                self.checkout_targets[actor]=world.quoted_checkout_targets[actor]
                if work_id and work_id in self.works:
                    self.works[work_id].bindings=list(world.quoted_checkout_targets[actor])
            if (command.get('action')=='use' and 'UpgradeVoucher' in command.get('name','')
                    and command in getattr(world,'sunset_actions',{}).get(actor,())):
                self.upgrade_travellers.add(actor)
                if self.upgrade_owner==actor:self.upgrade_owner=None
        identity=getattr(world,'sunset_buyer',None)
        cmd=response['roleCommandMap'].get(identity,{})
        if self.diagnostic.get('stage')=='upgrade_procure' and cmd.get('action') in ('move','buy'):
            self.upgrade_owner=identity
            if identity not in self.checkout_intents and self.diagnostic.get('basket'):
                self.checkout_intents[identity]=dict(self.diagnostic['basket'])
        worker = getattr(world,'caretaker_day_actor',None)
        daily_cmd = response['roleCommandMap'].get(worker,{})
        if (getattr(world,'caretaker_day_phase',None)=='buy' and daily_cmd.get('action') in ('move','buy')
                and getattr(world,'upgrade_checkout_actor',None)==worker):
            self.upgrade_owner=worker
        if cmd.get('action')=='buy':
            buyer_work=self.work_offers.get((identity,fingerprint(cmd)))
            self.pending[identity]={'name':cmd['name'],'prior':world.ours[identity].inventory[cmd['name']],
                                    'round':world.round,'num':cmd.get('num',1),
                                    'work':buyer_work,
                                    'purpose':self.works[buyer_work].purpose if buyer_work in self.works else None,
                                    'price':world.shop.get(cmd['name'],0),
                                    'day':getattr(getattr(world,'strategy_clock',None),'day',None)}


def permits(world, candidate):
    """Apply the same evening commitment to incumbents and later planners."""
    command=candidate.command;identity=candidate.actor
    allowed=getattr(world,'sunset_actions',{})
    buyer=getattr(world,'sunset_buyer',None)
    actor=world.ours.get(identity)
    if command.get('action')=='buy' and identity in getattr(world,'checkout_pending_actors',()):return False
    if (command.get('action')=='use' and 'UpgradeVoucher' in command.get('name','')
            and identity in getattr(world,'checkout_use_pending',())):return False
    work = None
    if candidate.work_id:
        market = getattr(world, 'procurement_market', None)
        work = market.works.get(candidate.work_id) if market else None
        if work is not None and (work.status in pw.TERMINAL or work.actor != identity):
            return False  # A terminal or foreign work no longer authorizes.
    personal_dose = (command.get('action')=='buy' and command.get('name')=='Medicine'
        and command.get('num',1)==1 and actor and actor.backpack is not None
        and not actor.inventory['Medicine']
        and (actor.health <= (200 if actor.kind=='pioneer' else 220)*.5
             or world.near_zone(actor.pos,'weaponShop'))
        and getattr(getattr(world,'strategy_policy',None),'medical_stock_enabled',False))
    if command in getattr(world,'personal_repair_stock_commands',{}).get(identity,()):
        return True  # Exact in-place order; investment and joint cash validation still apply.
    if command in getattr(world,'treasure_actions',{}).get(identity,()):
        return True
    if identity == getattr(world,'caretaker_day_actor',None):
        phase = getattr(world,'caretaker_day_phase',None)
        if command.get('action') in {'collect','sell'}:
            expected = 'harvest' if command['action'] == 'collect' else 'sell'
            return phase == expected and command in allowed.get(identity,())
        if command.get('action') == 'use' and command.get('name') == 'WallFixer' and phase != 'repair':
            return False
        if command.get('action') == 'buy':
            return phase == 'buy' and command in allowed.get(identity,())
        if command.get('action') == 'use' and 'UpgradeVoucher' in command.get('name',''):
            return phase == 'use' and command in allowed.get(identity,())
    if (command.get('action')=='buy' and not personal_dose and getattr(world,'upgrade_priority_pending',False)
            and command.get('name') not in world.upgrade_priority_items):
        return False
    if command.get('action')=='buy' and buyer and identity!=buyer:
        if work is not None and work.status not in pw.TERMINAL:
            # The work record authorizes its own actor's purchase; the retired
            # global-buyer veto no longer re-judges migrated entries (E2).
            return True
        actor=world.ours.get(identity)
        return bool(personal_dose or command.get('name')=='Medicine' and actor and actor.health<=110)
    if identity not in allowed:
        return True
    if command.get('action')=='use' and command.get('name') in {'Medicine','Bomb','DizzyWeapon','WallFixer'}:
        return True
    if identity!=buyer and command.get('action') in {'build','remove','use'}:
        return True
    return command in allowed[identity]
