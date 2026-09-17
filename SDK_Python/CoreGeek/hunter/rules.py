"""Official facts, explicit unknowns, and separately named engineering policy."""
from dataclasses import dataclass, field, fields
from functools import lru_cache
import json
from pathlib import Path

from .protocol import fingerprint, integer, position


@dataclass(frozen=True)
class Clock:
    round: int
    origin: int | None

    @property
    def offsets(self):
        return (self.origin,) if self.origin is not None else (0, 1)

    @property
    def phases(self):
        return {"day" if (self.round - o) % 130 < 70 else "night" for o in self.offsets}

    @property
    def day(self):
        days = {(self.round - o) // 130 + 1 for o in self.offsets}
        return next(iter(days)) if len(days) == 1 else None

    @property
    def until_night(self):
        return min(max(0, 70 - (self.round - o) % 130) for o in self.offsets)


@dataclass(frozen=True)
class BuildCost:
    gold: int
    items: dict[str, int]
    source: str


@dataclass(frozen=True)
class BuildingStats:
    health: int
    attack_range: int | str | None
    listed_power: int | None
    projectiles: int | None
    cooldown: int | None
    source: str


@dataclass(frozen=True)
class BuildRule:
    gold: int
    items: dict[str, int]
    cells: frozenset[tuple[int, int]]
    source: str


@lru_cache(maxsize=64)
def station_rings(anchor):
    """User-confirmed 2x2 footprint, with complete one-cell square rings."""
    x, y = anchor
    base = {(x, y), (x+1, y), (x, y-1), (x+1, y-1)}
    inner = {(a, b) for a in range(x-1, x+3) for b in range(y-2, y+2)}
    outer = {(a, b) for a in range(x-2, x+4) for b in range(y-3, y+3)}
    return frozenset(inner-base), frozenset(outer-inner)


@dataclass
class Rules:
    version: str = "brief-2026-09-10+unknowns-v1"
    round_origin: int | None = None
    empty_actions_verified: bool = False
    duplicate_targets: bool | None = None
    weapon_limit: int = 3
    wall_limit: int = 20
    rail_energy: dict[int, int] = field(default_factory=dict)
    builds: dict[str, BuildRule] = field(default_factory=dict)
    build_map_signature: str | None = None
    max_health: dict[str, dict[int, int]] = field(default_factory=dict)
    build_profiles: dict[str, dict[str, BuildRule]] = field(default_factory=dict)
    build_costs: dict[str, BuildCost] = field(default_factory=dict)
    building_stats: dict[str, dict[int, BuildingStats]] = field(default_factory=dict)
    build_geometry_source: str | None = None

    def health_limit(self, world, unit):
        """Use clean restoration evidence for this session, else formal rules."""
        if unit.kind == 'wall':
            record = getattr(world,'wall_health_levels',{}).get(unit.level)
            if record and not record.get('conflict'):
                return record['hp']
        return self.max_health.get(unit.kind,{}).get(unit.level)

    @staticmethod
    def wall_count(world):
        # A present wall with missing HP still occupies a slot conservatively.
        return sum(u.kind == "wall" and (u.health is None or u.health > 0) for u in world.ours.values())

    def observation_differences(self, world):
        """Reference-table discrepancies, without rewriting authoritative input."""
        differences = []
        for unit in world.ours.values():
            stats = self.building_stats.get(unit.kind, {}).get(unit.level)
            if stats is None:
                continue
            expected = stats.attack_range
            actual = unit.attack_range
            if expected == "map":
                # Any radius reaching every cell from this weapon is full-map.
                radius = max(unit.pos[0], world.width-1-unit.pos[0],
                             unit.pos[1], world.height-1-unit.pos[1])
                differs = actual is not None and actual < radius
            else:
                differs = expected is not None and actual is not None and expected != actual
            if differs:
                differences.append({"id": unit.id, "field": "attackRange", "observed": actual,
                                    "reference": expected, "source": stats.source})
            if unit.health is not None and unit.health > stats.health:
                differences.append({"id": unit.id, "field": "health", "observed": unit.health,
                                    "reference_max": stats.health, "source": stats.source})
            if len(differences) >= 64:
                break
        base, _ = self._automatic_base(world)
        if base is not None:
            blue, yellow = station_rings(base.pos)
            for unit in world.ours.values():
                allowed = yellow if unit.kind == "wall" else blue if unit.kind in {"gatling", "railgun", "rocket"} else None
                if allowed is not None and unit.alive and unit.pos not in allowed:
                    differences.append({"id": unit.id, "field": "buildRegion", "observed": list(unit.pos),
                                        "reference": "yellow" if unit.kind == "wall" else "blue",
                                        "source": self.build_geometry_source})
                if len(differences) >= 64:
                    break
        return differences[:64]

    @staticmethod
    def map_signature(world):
        # Fixed task/shop geography and our side bind an imported build mask;
        # refreshing minerals and newly built structures must not change it.
        return fingerprint({"width": world.width, "height": world.height, "side": world.side,
                            "fixed_zones": {k: sorted(v) for k, v in world.zones.items()
                                            if k not in {"stone", "iron", "copper"}}})

    def build_rule(self, world, name):
        signature = self.map_signature(world)
        if signature in self.build_profiles:
            return self.build_profiles[signature].get(name)
        if signature == self.build_map_signature:
            return self.builds.get(name)
        base, _ = self._automatic_base(world)
        cost = self.build_costs.get(name)
        if base is None or cost is None:
            return None
        blue, yellow = station_rings(base.pos)
        return BuildRule(cost.gold, cost.items, yellow if name == "wall" else blue,
                         cost.source+"; geometry: "+self.build_geometry_source)

    def _automatic_base(self, world):
        # Imported profiles remain an explicit, exclusive geography override.
        # Missing entries must not silently fall back to a different map policy.
        if self.build_map_signature is not None or self.build_profiles or self.builds:
            return None, "explicit_profiles"
        if not self.build_geometry_source:
            return None, "geometry_unconfigured"
        if (world.width, world.height) != (41, 32):
            return None, "map_dimensions_unverified"
        stations = [u for u in world.ours.values() if u.kind == "station"]
        if len(stations) != 1:
            return None, "station_missing_or_ambiguous"
        station = stations[0]
        if not station.alive:
            return None, "station_lifetime_unverified"
        x, y = station.pos
        if x < 2 or x+3 >= world.width or y < 3 or y+2 >= world.height:
            return None, "ring_crosses_map_boundary"
        for enemy in world.enemies.values():
            if enemy.kind == "station" and abs(x-enemy.pos[0]) <= 5 and abs(y-enemy.pos[1]) <= 5:
                return None, "overlapping_base_regions_unverified"
        return station, "station_rings"

    def build_region_status(self, world):
        base, reason = self._automatic_base(world)
        if base is None:
            return {"mode": reason}
        return {"mode": reason, "station_id": base.id, "station_pos": list(base.pos),
                "blue_cells": 12, "yellow_cells": 20, "source": self.build_geometry_source}

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema_version") != 1:
            raise ValueError("unsupported rule schema")
        origin = data.get("round_origin")
        if origin is not None and (not integer(origin) or origin not in (0, 1)):
            raise ValueError("invalid round origin")
        if origin is not None and not data.get("round_origin_source"):
            raise ValueError("round origin requires evidence source")
        result = cls(version=data["version"], round_origin=origin)
        geometry = data.get("build_geometry")
        if geometry is not None:
            if (not isinstance(geometry, dict) or set(geometry) != {"type", "source"}
                    or geometry["type"] != "station_rings_v1"
                    or not isinstance(geometry["source"], str) or not geometry["source"].strip()):
                raise ValueError("build geometry requires station_rings_v1 and source")
            result.build_geometry_source = geometry["source"]
        wall_limit = data.get("wall_limit")
        if wall_limit is not None:
            if not integer(wall_limit, 1) or not isinstance(data.get("wall_limit_source"), str) or not data["wall_limit_source"].strip():
                raise ValueError("wall limit requires positive count and source")
            result.wall_limit = wall_limit

        def cost_entry(value):
            if (not isinstance(value, dict) or not isinstance(value.get("source"), str)
                    or not value["source"].strip() or not integer(value.get("gold"), 0)):
                raise ValueError("build costs require source and explicit gold")
            items = value.get("items")
            if (not isinstance(items, dict) or any(k not in {"stone", "iron", "copper"}
                    or not integer(v, 0) for k, v in items.items())):
                raise ValueError("invalid material costs")
            return BuildCost(value["gold"], items.copy(), value["source"])

        costs = data.get("build_costs", {})
        if not isinstance(costs, dict):
            raise ValueError("build_costs must be an object")
        for name, value in costs.items():
            if name not in {"wall", "gatling", "railgun", "rocket"}:
                raise ValueError("unknown build cost type")
            result.build_costs[name] = cost_entry(value)
        for key in ("empty_actions_verified", "duplicate_targets"):
            value = data.get(key)
            if value is not None and type(value) is not bool:
                raise ValueError(f"invalid {key}")
            if value is True and not data.get(key + "_source"):
                raise ValueError(f"{key} requires evidence source")
            if value is not None:
                setattr(result, key, value)
        result.build_map_signature = data.get("build_map_signature")
        def parse_builds(values, signature):
            if not isinstance(values, dict):
                raise ValueError("builds must be an object")
            parsed = {}
            for name, value in values.items():
                if name not in {"wall", "gatling", "railgun", "rocket"} or not isinstance(value, dict):
                    raise ValueError("unknown build type or invalid entry")
                cost = cost_entry(value)
                coordinates = value.get("cells", [])
                if not isinstance(coordinates, list) or len(coordinates) > 16384:
                    raise ValueError("invalid build cell list")
                cells = [position(p) for p in coordinates]
                if None in cells or not signature:
                    raise ValueError("build zones require coordinates and map signature")
                parsed[name] = BuildRule(cost.gold, cost.items, frozenset(cells), cost.source)
            return parsed
        result.builds = parse_builds(data.get("builds", {}), result.build_map_signature)
        profiles = data.get("build_profiles", {})
        if not isinstance(profiles, dict) or len(profiles) > 16:
            raise ValueError("build profile registry exceeds local bounds")
        for signature, profile in profiles.items():
            if (not isinstance(signature, str) or len(signature) != 64
                    or any(c not in "0123456789abcdef" for c in signature)
                    or not isinstance(profile, dict) or set(profile) != {"builds"}):
                raise ValueError("build profile requires a map SHA256 and builds")
            if signature == result.build_map_signature:
                raise ValueError("ambiguous legacy and registry build profile")
            result.build_profiles[signature] = parse_builds(profile["builds"], signature)
        # A mask can reuse sourced global costs without duplicating numbers.
        # Without an explicit mask, sourced automatic geometry may supply cells.
        masks = data.get("build_masks", {})
        if not isinstance(masks, dict) or len(masks)+len(profiles) > 16:
            raise ValueError("build mask registry exceeds local bounds")
        for signature, entries in masks.items():
            if (not isinstance(signature, str) or len(signature) != 64
                    or any(c not in "0123456789abcdef" for c in signature)
                    or not isinstance(entries, dict)):
                raise ValueError("build mask requires map SHA256 and entries")
            if signature == result.build_map_signature or signature in result.build_profiles:
                raise ValueError("ambiguous build mask and profile")
            combined = {}
            for name, entry in entries.items():
                cost = result.build_costs.get(name)
                if (cost is None or not isinstance(entry, dict) or set(entry) != {"source", "cells"}
                        or not isinstance(entry["source"], str) or not entry["source"].strip()):
                    raise ValueError("mask requires sourced cells and known costs")
                combined[name] = {"gold": cost.gold, "items": cost.items, "cells": entry["cells"],
                                  "source": cost.source+"; mask: "+entry["source"]}
            result.build_profiles[signature] = parse_builds(combined, signature)

        stats = data.get("building_stats", {})
        if not isinstance(stats, dict):
            raise ValueError("building_stats must be an object")
        for kind, levels in stats.items():
            if kind not in {"station", "wall", "gatling", "railgun", "rocket"} or not isinstance(levels, dict):
                raise ValueError("invalid building stats type")
            result.building_stats[kind] = {}
            result.max_health[kind] = {}
            for level, entry in levels.items():
                required = {"health", "attack_range", "listed_power", "projectiles", "cooldown", "source"}
                if (level not in {"1", "2", "3"} or not isinstance(entry, dict) or set(entry) != required
                        or not integer(entry["health"], 1) or not isinstance(entry["source"], str) or not entry["source"].strip()):
                    raise ValueError("building stats require level, fields and source")
                weapon = kind in {"gatling", "railgun", "rocket"}
                if weapon:
                    if (not (integer(entry["attack_range"], 1) or entry["attack_range"] == "map")
                            or not integer(entry["listed_power"], 1) or not integer(entry["cooldown"], 0)
                            or entry["projectiles"] != (1 if kind == "railgun" else int(level))
                            or type(entry["projectiles"]) is not int):
                        raise ValueError("invalid weapon stats")
                elif any(entry[k] is not None for k in ("attack_range", "listed_power", "projectiles", "cooldown")):
                    raise ValueError("nonweapon attack stats must be null")
                result.building_stats[kind][int(level)] = BuildingStats(**entry)
                result.max_health[kind][int(level)] = entry["health"]
        for level, entry in data.get("rail_energy", {}).items():
            if level not in {"1", "2", "3"} or not entry.get("source") or not integer(entry.get("value"), 1):
                raise ValueError("rail energy requires level, value and source")
            result.rail_energy[int(level)] = entry["value"]
        for kind, levels in data.get("max_health", {}).items():
            result.max_health.setdefault(kind, {})
            for level, entry in levels.items():
                if level not in {"1", "2", "3"} or not entry.get("source") or not integer(entry.get("value"), 1):
                    raise ValueError("health requires value and source")
                previous = result.max_health[kind].get(int(level))
                if previous is not None and previous != entry["value"]:
                    raise ValueError("conflicting maximum health sources")
                result.max_health[kind][int(level)] = entry["value"]
        return result


@dataclass(frozen=True)
class Policy:
    """Local strategy parameters, never substituted for missing game rules."""
    planning_seconds: float = 2.8
    lock_seconds: float = 0.15
    beam_width: int = 128
    max_candidates: int = 96
    cache_entries: int = 32
    session_entries: int = 4
    return_buffer: int = 4
    return_commitment_enabled: bool = True
    return_detour_enabled: bool = True
    medical_supply_enabled: bool = True
    medical_stock_enabled: bool = False
    medical_gold_limit: int = 60
    pioneer_defence_enabled: bool = True
    construction_commitment_enabled: bool = True
    closed_ring_rockets_enabled: bool = True
    forward_battery_enabled: bool = False
    task_side_layout_enabled: bool = False
    pioneer_rotation_enabled: bool = False
    rear_open_enabled: bool = False
    caretaker_repair_target: int = 3
    external_gate_enabled: bool = False
    gate_seal_choice: str = 'auto'
    gate_dawn_choice: str = 'auto'
    night_foraging_enabled: bool = True
    emergency_gate_enabled: bool = False
    repair_plan_enabled: bool = False
    repair_supply_enabled: bool = False
    corner_battery_ports_enabled: bool = False
    gatling_upgrade_health_fraction: float = 0.5
    upgrade_commitment_enabled: bool = True
    base_recovery_enabled: bool = True
    economic_route_commitment_enabled: bool = True
    day_schedule_enabled: bool = True
    economy_first_enabled: bool = True
    staged_walls_enabled: bool = True
    construction_site_coordination_enabled: bool = True
    wall_repair_health_fraction: float = 0.30
    defence_procurement_enabled: bool = False
    defence_gold_limit: int = 200
    joint_lookahead_enabled: bool = False
    joint_lookahead_seconds: float = 0.3
    lookahead_enabled: bool = False
    lookahead_horizon: int = 4
    lookahead_weight: float = 0.5
    sell_batch: int = 12
    reserve_gold: int = 20
    telemetry_entries: int = 256
    treasure_enabled: bool = True
    treasure_gold_limit: int = 90
    treasure_attempt_limit: int = 2
    news_hold_enabled: bool = True
    news_daily_enabled: bool = True
    news_start_day: int = 2
    news_context_chars: int = 64000  # Local input ceiling, not a model-window claim.
    summon_pressure_enabled: bool = True
    summon_portfolio_enabled: bool = False
    summon_wave_memory_enabled: bool = False
    joint_fire_enabled: bool = True
    rocket_diversity_enabled: bool = True
    base_fire_enabled: bool = False
    skill_reuse_enabled: bool = True
    task_schedule_enabled: bool = True
    task_full_timeout_guard_enabled: bool = False
    task_gold_weight: float = 0.5
    operator_safety_enabled: bool = False
    operator_handoff_enabled: bool = False
    operator_cycle_enabled: bool = False
    rocket_rotation_enabled: bool = False
    lethal_entry_guard_enabled: bool = False
    firing_lanes_enabled: bool = False
    weapon_portfolio_enabled: bool = True
    portfolio_saturation_enabled: bool = False

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or not isinstance(data.get("parameters"), dict):
            raise ValueError("invalid strategy schema")
        parameters = data["parameters"]
        defaults = cls()
        if set(parameters) - {f.name for f in fields(cls)}:
            raise ValueError("unknown strategy parameter")
        for name, value in parameters.items():
            default = getattr(defaults, name)
            if name in ('gate_seal_choice', 'gate_dawn_choice'):
                if type(value) is not str or value not in ('auto', 'm', 'w'):
                    raise ValueError(f"{name} must be auto, m or w")
            elif type(default) is bool:
                if type(value) is not bool:
                    raise ValueError(f"{name} must be boolean")
            elif type(default) is int:
                if not integer(value, 0) or value > 100000:
                    raise ValueError(f"{name} must be a bounded nonnegative integer")
            elif type(value) not in (int, float) or not 0 <= value <= 3:
                raise ValueError(f"{name} exceeds local timing bounds")
        result = cls(**parameters)
        if result.rear_open_enabled and not (result.task_side_layout_enabled and result.pioneer_rotation_enabled):
            raise ValueError('rear-open mode requires task-side layout and pioneer rotation')
        if not 2 <= result.caretaker_repair_target <= 100:
            raise ValueError('caretaker repair target must fit a worker backpack')
        if not 1 <= result.news_start_day <= 10 or not 8000 <= result.news_context_chars <= 100000:
            raise ValueError('news scheduling/context limits out of bounds')
        if not 0 < result.gatling_upgrade_health_fraction <= 1:
            raise ValueError("gatling upgrade health fraction must be in (0, 1]")
        if not 4 <= result.lookahead_horizon <= 8 or not 0 <= result.lookahead_weight <= 1:
            raise ValueError("lookahead parameters exceed bounded scenario policy")
        if not 0 <= result.joint_lookahead_seconds <= .5:
            raise ValueError("joint rollout budget exceeds half a second")
        for name in ("beam_width", "max_candidates", "cache_entries", "session_entries", "sell_batch", "telemetry_entries"):
            if getattr(result, name) < 1:
                raise ValueError(f"{name} must be positive")
        if result.beam_width > 512 or result.max_candidates > 256 or result.cache_entries > 128 or result.session_entries > 8:
            raise ValueError("strategy exceeds memory/search bounds")
        return result
