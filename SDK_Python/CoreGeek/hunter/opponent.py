"""Visible-strength beliefs and conservative, globally reserved summon pressure."""
from dataclasses import dataclass, field
import time

from .arbitration import Candidate
from .beliefs import OpponentBelief
from .protocol import distance
from .summons import ORDERS, plan

SUMMONS = ORDERS


def next_wave_window(clock):
    """Use opportunities before the earliest next night and quota reset.

    Origin uncertainty must leave a future wave under both interpretations.
    Buying then using costs two distinct actions; inventory needs only one.
    """
    windows, starts = [], []
    for origin in clock.offsets:
        elapsed = clock.round-origin
        day, phase = divmod(elapsed, 130)
        start = origin+day*130+70 if phase < 70 else origin+(day+1)*130+70
        if elapsed < 0 or start >= origin+1300:
            return None
        windows.append(min(start-clock.round, origin+(day+1)*130-clock.round))
        starts.append(start)
    return {"night_rounds": sorted(set(starts)), "use_turns_before_boundary": min(windows)}


@dataclass
class Opponent:
    day: int | None = None
    quota_known: bool = False
    reserved_uses: int = 0
    belief: OpponentBelief = field(default_factory=OpponentBelief)
    pending_buys: dict = field(default_factory=dict)
    diagnostic: dict = field(default_factory=dict)
    reserved_waves: dict = field(default_factory=dict)
    # Independent daily reservations for each still-possible round origin.
    # None means this day's pre-session usage is unknown.
    uncertain_quotas: dict = field(default_factory=dict)

    def reconcile_quota(self, clock):
        if clock.origin is None:
            for origin in clock.offsets:
                day = (clock.round-origin)//130+1
                previous = self.uncertain_quotas.get(origin)
                if previous is None:
                    used = 0 if clock.round == origin+(day-1)*130 else None
                    self.uncertain_quotas[origin] = (day, used)
                elif day > previous[0]:
                    self.uncertain_quotas[origin] = (day, 0)
            counts = [entry[1] for entry in self.uncertain_quotas.values()]
            self.quota_known = all(count is not None for count in counts)
            self.reserved_uses = max(counts) if self.quota_known else 0
            self.day = clock.day
            return
        if self.uncertain_quotas:
            self.day, used = self.uncertain_quotas[clock.origin]
            self.quota_known = used is not None
            self.reserved_uses = used or 0
            self.uncertain_quotas.clear()
        boundary = (clock.round-clock.origin) % 130 == 0
        if clock.day != self.day and (boundary or self.quota_known):
            self.day, self.quota_known, self.reserved_uses = clock.day, True, 0

    def reconcile(self, world, clock):
        self.belief.reconcile(world)
        self.belief.remember_night(world, clock)
        self.reserved_waves = {key: value for key, value in self.reserved_waves.items() if max(key) > world.round}
        self.reconcile_quota(clock)
        feedback = world.raw.get("lastRoundRoleActionResults", {})
        if not isinstance(feedback, dict):
            feedback = {}
        for actor_id, pending in list(self.pending_buys.items()):
            actor = world.ours.get(actor_id)
            failed = world.round == pending["round"]+1 and feedback.get(actor_id) is False
            covered = (actor is not None and actor.backpack is not None
                       and actor.inventory[pending["name"]] >= pending["prior_count"]+pending["num"])
            if failed or covered:
                self.pending_buys.pop(actor_id)

    @property
    def remaining(self):
        return max(0, 10-self.reserved_uses) if self.quota_known else 0

    def candidates(self, world, clock, policy, task_actor=None, deadline=None):
        window = next_wave_window(clock)
        self.diagnostic = {"belief": self.belief.describe(world), "wave_window": window,
                           "pending_purchases": len(self.pending_buys), "status": "unavailable",
                           "summon_remaining": self.remaining, "quota_known": self.quota_known}
        if not policy.summon_pressure_enabled or not self.remaining or not world.stations:
            return []
        if window is None:
            self.diagnostic["status"] = "no_later_night"
            return []
        # Deliberately observable inputs only: enemy gold/score/inventory are not
        # available. Hidden weapons remain unknown, never counted as destroyed.
        enemy_stations = [u for u in world.enemies.values() if u.alive and u.kind == "station"]
        if not enemy_stations or not world.weapons:
            return []
        base = world.stations[0]
        if any(r.alive and r.target_team != ("defender" if world.side == "challenger" else "challenger")
               and distance(r.pos, base.pos) <= 8 for r in world.robots.values()):
            return []
        worker_count = sum(u.kind == "worker" for u in world.movers)
        if worker_count < 2 or base.health < 200:
            return []
        result = []
        # Current inventories are personal; only the holder may use an order.
        # Team counts constrain procurement, never create a transferable bag.
        held = sum(sum(actor.inventory[name] for name in SUMMONS) for actor in world.movers)
        inventory_complete = all(actor.backpack is not None for actor in world.movers)
        purchase_slots = max(0, self.remaining-held-sum(p["num"] for p in self.pending_buys.values()))
        if not inventory_complete:
            purchase_slots = 0
        pending_gold = sum(p["gold"] for p in self.pending_buys.values())
        budget = max(0, (world.gold or 0)-max(150, policy.reserve_gold)-pending_gold)
        pending_slots = max(0, 8-len(self.pending_buys))
        evidence = self.belief.purchase_evidence(world)
        basis = 'current_pressure'
        if not evidence and policy.summon_wave_memory_enabled:
            evidence = self.belief.previous_wave_evidence(world, clock)
            basis = 'previous_night_observations'
        self.diagnostic.update(status=("held_orders_only" if not evidence else
                                       "observed_pressure" if basis == 'current_pressure' else "previous_wave_pressure"),
                               purchase_evidence=evidence, held_orders=held, purchase_slots=purchase_slots,
                               purchase_evidence_basis=basis if evidence else None,
                               inventory_complete=inventory_complete,
                               summon_purchase_budget=budget, pending_purchase_gold=pending_gold)
        if policy.summon_portfolio_enabled:
            candidates, portfolio = plan(world, remaining=max(0, self.remaining-sum(p["num"] for p in self.pending_buys.values())),
                                         turns=window["use_turns_before_boundary"], budget=budget,
                                         purchase_slots=purchase_slots if evidence else 0,
                                         pending_buys=self.pending_buys, task_actor=task_actor,
                                         committed=self.reserved_waves.get(tuple(window["night_rounds"]), (0, 0, 0, 0)),
                                         deadline=min(deadline if deadline is not None else float("inf"), time.monotonic()+.3))
            self.diagnostic["portfolio"] = portfolio
            for candidate in candidates:
                if candidate.command['action'] == 'buy':
                    candidate.gold_reserve = max(150, policy.reserve_gold)
            return candidates
        for actor in world.movers:
            if actor.id == task_actor:
                continue
            for name in SUMMONS:
                if actor.inventory[name]:
                    result.append(Candidate(actor.id, {"action": "use", "name": name}, 6,
                                            "bounded opponent pressure; no assumed enemy private resources"))
            # A budget floor accounts for our known upgrade opportunity. These
            # are tunable heuristics, not a proof of profitable summons.
            if (evidence and window["use_turns_before_boundary"] >= 2 and purchase_slots
                    and not any(actor.inventory[name] for name in SUMMONS)
                    and actor.id not in self.pending_buys and pending_slots
                    and actor.backpack is not None and actor.capacity is not None
                    and len(actor.backpack) < actor.capacity and world.near_zone(actor.pos, "weaponShop")):
                name = "SmallRobotSummonOrder"
                if name in world.shop and budget >= world.shop[name]:
                    result.append(Candidate(actor.id, {"action": "buy", "name": name, "num": 1}, 2,
                                            "observed opponent pressure; shared surplus and inventory reservation",
                                            gold_reserve=max(150, policy.reserve_gold)))
                    budget -= world.shop[name]
                    purchase_slots -= 1
                    pending_slots -= 1
        return result

    def finalize(self, response, world=None):
        uses = sum(c["action"] == "use" and c.get("name") in SUMMONS
                   for c in response["roleCommandMap"].values())
        self.reserved_uses += uses
        for origin, (day, used) in self.uncertain_quotas.items():
            if used is not None:
                self.uncertain_quotas[origin] = (day, used+uses)
        window = self.diagnostic.get("wave_window")
        if window:
            key = tuple(window["night_rounds"])
            counts = list(self.reserved_waves.get(key, (0, 0, 0, 0)))
            for command in response["roleCommandMap"].values():
                if command["action"] == "use" and command.get("name") in SUMMONS:
                    counts[SUMMONS.index(command["name"])] += 1
            if any(counts):
                self.reserved_waves[key] = tuple(counts)
        if world is not None:
            for actor_id, command in response["roleCommandMap"].items():
                if command["action"] == "buy" and command.get("name") in SUMMONS and actor_id in world.ours:
                    self.pending_buys[actor_id] = {"round": world.round, "name": command["name"],
                                                  "num": command.get("num", 1),
                                                  "gold": world.shop[command["name"]]*command.get("num", 1),
                                                  "prior_count": world.ours[actor_id].inventory[command["name"]]}
