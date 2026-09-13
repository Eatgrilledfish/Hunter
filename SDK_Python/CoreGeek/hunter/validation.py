"""Game preconditions and resources, separate from JSON structure checks."""
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from .protocol import MOBILE, WEAPONS, MINERALS, distance, position, validate_command, ProtocolError


class Verdict(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN = "unknown"


@dataclass
class Resources:
    locks: set[str] = field(default_factory=set)
    cells: set[tuple[int, int]] = field(default_factory=set)
    gold: int = 0
    items: Counter = field(default_factory=Counter)
    weapon_slots: int = 0
    summons: int = 0
    wall_slots: int = 0


@dataclass
class Check:
    verdict: Verdict
    reason: str
    resources: Resources = field(default_factory=Resources)


def check_action(world, clock, rules, identity, command, *, task_actor=None, summon_remaining=0,
                 task_moves=(), allow_task_control=False):
    def invalid(reason):
        return Check(Verdict.INVALID, reason)
    def unknown(reason):
        return Check(Verdict.UNKNOWN, reason)
    try:
        validate_command(command)
    except (ProtocolError, TypeError, ValueError) as exc:
        return invalid(str(exc))
    actor = world.ours.get(identity)
    if actor is None:
        return invalid("actor not present")
    if actor.health is None:
        return unknown("actor health absent")
    if not actor.alive:
        return invalid("actor dead")
    action = command["action"]
    if action != "attack" and actor.kind not in MOBILE:
        return invalid("stationary entity cannot perform mobile action")
    targets = [position(p) for p in command.get("targetPos", [])]
    if any(not world.inside(p) for p in targets):
        return invalid("target outside map")
    target = targets[0] if targets else None
    r = Resources(locks={identity})
    if action == "move":
        if identity == task_actor and target not in task_moves:
            return invalid("active task actor is held by task engine")
        if distance(actor.pos, target) != 1 or target in world.occupied:
            return invalid("move distance or current occupancy")
        r.cells.add(target)
    elif action == "attack":
        if actor.kind not in WEAPONS:
            return invalid("only weapons attack")
        if clock.phases != {"night"}:
            return unknown("night boundary uncertain") if "night" in clock.phases else invalid("daytime attack")
        controller = world.ours.get(command["controllerId"])
        if controller is None or controller.kind not in MOBILE or not controller.alive:
            return invalid("controller missing or unavailable")
        if controller.id == task_actor and not allow_task_control:
            return invalid("controller reserved by active task")
        if distance(actor.pos, controller.pos) > 1:
            return invalid("controller is not adjacent")
        r.locks.add(controller.id)
        if actor.level not in {1, 2, 3} or actor.attack_range is None:
            return unknown("weapon level/range unavailable")
        if len(targets) != (1 if actor.kind == "railgun" else actor.level):
            return invalid("wrong target count")
        if any(distance(actor.pos, p) > actor.attack_range for p in targets):
            return invalid("target outside weapon range")
        if len(set(targets)) < len(targets) and rules.duplicate_targets is not True:
            return unknown("duplicate impact coordinates unverified")
        if actor.cooldown is not None and actor.cooldown > 0:
            return invalid("weapon cooling down")
        if actor.kind == "rocket" and actor.cooldown is None:
            return unknown("rocket cooldown unavailable")
        if actor.kind == "gatling":
            vectors = [(p[0]-actor.pos[0], p[1]-actor.pos[1]) for p in targets]
            if (0, 0) in vectors:
                return invalid("zero firing direction")
            if any(a[0]*b[0]+a[1]*b[1] < 0 for a in vectors for b in vectors):
                return invalid("gatling cone exceeds 90 degrees")
        if actor.kind == "railgun" and target == actor.pos:
            return invalid("zero firing direction")
        from .robot_targets import opposing, area_clear, line_clear
        if actor.kind == 'rocket':
            if not area_clear(world,targets):return invalid('would damage opponent-camp robot')
        elif any(r.alive and opposing(world,r) for r in world.robots.values()):
            from .combat import line_damage
            for endpoint in targets:
                damage=line_damage(world,actor,endpoint,rules)
                if not line_clear(world,actor,endpoint,rules,damage):
                    return invalid('would damage opponent-camp robot on firing line')
    elif action in {"collect", "build", "remove"}:
        if actor.kind != "worker":
            return invalid("worker required")
        if distance(actor.pos, target) > 1:
            return invalid("interaction too far")
        if action == "collect":
            if not any(target in world.zones.get(k, ()) for k in MINERALS):
                return invalid("not a current mineral cell")
            if actor.capacity is None or actor.backpack is None:
                return unknown("inventory/capacity unavailable")
            if len(actor.backpack) >= actor.capacity:
                return invalid("backpack full")
        elif action == "build":
            if clock.phases != {"day"}:
                return unknown("day boundary uncertain") if "day" in clock.phases else invalid("nighttime build")
            name = command["name"]
            if name not in WEAPONS | {"wall"}:
                return invalid("unknown building")
            rule = rules.build_rule(world, name)
            if rule is None:
                return unknown("verified map-bound costs and build zones unavailable")
            if target not in rule.cells or target in world.occupied:
                return invalid("invalid/occupied build cell; replacement disabled")
            if actor.backpack is None:
                return unknown("inventory unavailable")
            if any(actor.inventory[k] < n for k, n in rule.items.items()):
                return invalid("insufficient personal materials")
            r.gold = rule.gold
            r.items.update({(identity, k): n for k, n in rule.items.items()})
            r.cells.add(target)
            r.weapon_slots = int(name in WEAPONS)
            r.wall_slots = int(name == "wall")
            if len(world.weapons) + r.weapon_slots > rules.weapon_limit:
                return invalid("weapon cap")
            if r.wall_slots and rules.wall_count(world) + r.wall_slots > rules.wall_limit:
                return invalid("wall cap")
        else:
            walls = [u for u in world.ours.values() if u.alive and u.kind == "wall" and u.pos == target]
            if not walls:
                return invalid("no own wall at target")
            r.locks.add(walls[0].id)
            r.cells.add(target)
    elif action in {"sell", "buy"}:
        name, quantity = command["name"], command.get("num", 1)
        if not world.near_zone(actor.pos, "vendor" if action == "sell" else "weaponShop"):
            return invalid("not adjacent to shop")
        if actor.backpack is None:
            return unknown("inventory unavailable")
        if action == "sell":
            if name not in MINERALS or name not in world.vendor or actor.inventory[name] < quantity:
                return invalid("untradeable or insufficient minerals")
            r.items[(identity, name)] = quantity
        else:
            if name not in world.shop:
                return invalid("item absent from current shop")
            if actor.capacity is None:
                return unknown("capacity unavailable")
            if len(actor.backpack) + quantity > actor.capacity:
                return invalid("backpack capacity exceeded")
            r.gold = world.shop[name] * quantity
    elif action == "acceptTask":
        if actor.kind != "pioneer" or world.phase_task:
            return invalid("cannot accept task")
        if not any(t.get("isValid") is True and type(t.get("coldDownRounds")) is int
                   and t["coldDownRounds"] == 0
                   and any(distance(actor.pos, p) <= 1 for p in world.task_cells(t)) for t in world.tasks):
            return invalid("no available adjacent own task")
    elif action == "submitAnswer":
        if actor.kind != "pioneer" or not world.phase_task or identity != task_actor:
            return invalid("no confirmed task for actor")
    elif action in {"summonTreasure", "drop", "use"}:
        if actor.backpack is None:
            return unknown("inventory unavailable")
        if action == "summonTreasure":
            if actor.kind != "pioneer" or distance(actor.pos, target) > 1:
                return invalid("treasure requires adjacent pioneer")
            if task_actor == identity:
                return invalid("active task has priority over treasure attempt")
            needed = Counter(command["item"])
        else:
            needed = Counter({command["name"]: 1})
        if any(actor.inventory[k] < n for k, n in needed.items()):
            return invalid("insufficient personal items")
        r.items.update({(identity, k): n for k, n in needed.items()})
        if action == "use":
            name = command["name"]
            if name in {"Medicine", "SmallRobotSummonOrder", "MiddleRobotSummonOrder", "LargeRobotSummonOrder", "BossRobotSummonOrder"}:
                if targets:
                    return invalid("unexpected item target")
                if name.endswith("SummonOrder"):
                    if summon_remaining < 1:
                        return unknown("daily summon allowance unavailable or exhausted")
                    r.summons = 1
            elif name in {"DizzyWeapon", "Bomb"}:
                if target is None:
                    return invalid("item requires target")
                from .robot_targets import area_clear
                if not area_clear(world,targets):return invalid('would affect opponent-camp robot')
            elif name == "WallFixer" or name.startswith(("WeaponUpgradeVoucher", "WallUpgradeVoucher", "StationUpgradeVoucher")):
                if target is None or distance(actor.pos, target) > 1:
                    return invalid("building item requires adjacent target anchor")
                buildings = [u for u in world.ours.values() if u.pos == target and u.alive]
                kinds = WEAPONS if name.startswith("Weapon") else {"station"} if name.startswith("Station") else {"wall"}
                buildings = [u for u in buildings if u.kind in kinds]
                if not buildings:
                    return invalid("wrong building target")
                building = buildings[0]
                if name != "WallFixer":
                    prefix = "Weapon" if name.startswith("Weapon") else "Station" if name.startswith("Station") else "Wall"
                    if name not in {prefix+"UpgradeVoucher1", prefix+"UpgradeVoucher2"}:
                        return invalid("unknown upgrade item")
                    if building.level is None:
                        return unknown("building level unavailable")
                    if building.level != int(name[-1]):
                        return invalid("upgrade level mismatch")
                r.locks.add(building.id)
            else:
                return unknown("unverified item identifier/effect (case preserved)")
    if r.gold and world.gold is None:
        return unknown("gold unavailable")
    if r.gold > (world.gold or 0):
        return invalid("insufficient initial gold")
    return Check(Verdict.VALID, "known preconditions satisfied", r)


def merge_resources(world, rules, current, addition, summon_remaining=0):
    if current.locks & addition.locks or current.cells & addition.cells:
        return None
    gold = current.gold + addition.gold
    if gold > (world.gold or 0):
        return None
    slots, summons = current.weapon_slots + addition.weapon_slots, current.summons + addition.summons
    if (slots and len(world.weapons) + slots > rules.weapon_limit) or summons > summon_remaining:
        return None
    wall_slots = current.wall_slots + addition.wall_slots
    if wall_slots and rules.wall_count(world) + wall_slots > rules.wall_limit:
        return None
    items = current.items + addition.items
    if any(world.ours[uid].inventory[name] < count for (uid, name), count in items.items()):
        return None
    return Resources(current.locks | addition.locks, current.cells | addition.cells, gold, items, slots, summons, wall_slots)
