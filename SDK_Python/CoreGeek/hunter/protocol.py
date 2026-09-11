"""Strict output contract and tolerant, uncertainty-preserving input adapter."""
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json

MOBILE = frozenset({"worker", "pioneer"})
WEAPONS = frozenset({"gatling", "railgun", "rocket"})
KNOWN_UNIT_KINDS = MOBILE | WEAPONS | {"station", "wall", "smallRobot", "middleRobot", "largeRobot", "bossRobot"}
MINERALS = frozenset({"stone", "iron", "copper"})
Pos = tuple[int, int]


class ProtocolError(ValueError):
    pass


def integer(value, minimum=None):
    return type(value) is int and (minimum is None or value >= minimum)


def obj(value):
    return value if isinstance(value, dict) else {}


def array(value):
    return value if isinstance(value, list) else []


def position(value):
    value = obj(value)
    if integer(value.get("x")) and integer(value.get("y")):
        return value["x"], value["y"]
    return None


def pos_json(pos):
    return {"x": pos[0], "y": pos[1]}


def distance(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json(text):
    def invalid_constant(value):
        raise ProtocolError(f"nonfinite JSON number: {value}")
    return json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)


def fingerprint(raw):
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class Unit:
    id: str
    kind: str
    pos: Pos
    health: int | None
    level: int | None
    attack_range: int | None
    attack_power: int | None
    cooldown: int | None
    capacity: int | None
    backpack: tuple[str, ...] | None
    target_team: str | None
    abnormal: str

    @property
    def alive(self):
        return self.health is not None and self.health > 0

    @property
    def blocks(self):
        # Keep incomplete/unrecognized observations; only confirmed death of a
        # known unit releases its old cells. A predicted hit is not evidence.
        return self.health != 0 or self.kind not in KNOWN_UNIT_KINDS

    @property
    def inventory(self):
        return Counter(self.backpack or ())

    @property
    def cells(self):
        x, y = self.pos
        return {(x, y), (x+1, y), (x, y-1), (x+1, y-1)} if self.kind == "station" else {self.pos}


@dataclass
class World:
    raw: dict
    round: int
    width: int
    height: int
    side: str
    team_id: str
    gold: int | None
    score: int | None
    ours: dict[str, Unit]
    enemies: dict[str, Unit]
    robots: dict[str, Unit]
    zones: dict[str, set[Pos]]
    occupied: set[Pos]
    vendor: dict[str, int]
    shop: dict[str, int]
    tasks: list[dict]
    phase_task: str
    warnings: list[str]

    def inside(self, pos):
        return 0 <= pos[0] < self.width and 0 <= pos[1] < self.height

    @property
    def movers(self):
        return [u for u in self.ours.values() if u.alive and u.kind in MOBILE]

    @property
    def weapons(self):
        return [u for u in self.ours.values() if u.alive and u.kind in WEAPONS]

    @property
    def stations(self):
        return [u for u in self.ours.values() if u.alive and u.kind == "station"]

    def near_zone(self, pos, name):
        return any(distance(pos, p) <= 1 for p in self.zones.get(name, ()))

    def task_cells(self, task):
        anchor = position(task.get("taskPosition"))
        if anchor is None:
            return set()
        for kind, cells in self.zones.items():
            if kind.startswith(self.side + "TaskPoint") and anchor in cells:
                return cells
        # A task descriptor alone cannot establish ownership without the map.
        return set()


def parse_request(raw):
    if not isinstance(raw, dict):
        raise ProtocolError("request must be an object")
    map_info, team = obj(raw.get("mapInfo")), obj(raw.get("teamOur"))
    round_no, width, height = raw.get("roundNo"), map_info.get("width"), map_info.get("height")
    if not integer(round_no, 0):
        raise ProtocolError("missing or invalid roundNo")
    if not integer(width, 1) or not integer(height, 1) or width * height > 16384:
        raise ProtocolError("missing/invalid map dimensions or local safety bound exceeded")
    if team.get("type") not in {"challenger", "defender"}:
        raise ProtocolError("unknown team side")
    team_id = team.get("teamId")
    if not (isinstance(team_id, str) and team_id or integer(team_id, 0)):
        raise ProtocolError("missing team identity")
    occupied, warnings = set(), []

    def units(group):
        result, duplicates = {}, set()
        for value in array(obj(raw.get(group)).get("roles")):
            value = obj(value)
            pos = position(value.get("pos"))
            kind = value.get("roleType")
            if pos is None:
                warnings.append(f"{group}: missing position")
                continue
            cells = {pos}
            if kind == "station":
                cells.update({(pos[0]+1, pos[1]), (pos[0], pos[1]-1), (pos[0]+1, pos[1]-1)})
            identity = value.get("id")
            valid_identity = integer(identity, 0) or isinstance(identity, str) and identity.isdecimal()
            known_dead = (valid_identity and isinstance(kind, str) and kind in KNOWN_UNIT_KINDS
                          and integer(value.get("health"), 0) and value["health"] == 0)
            if not known_dead:
                occupied.update(cells)
            if not valid_identity:
                warnings.append(f"{group}: invalid id")
                continue
            identity = str(identity)
            if identity in result or identity in duplicates:
                # Conflicting records cannot establish a reliable dead location.
                previous = result.pop(identity, None)
                occupied.update(cells)
                if previous is not None:
                    occupied.update(previous.cells)
                duplicates.add(identity)
                warnings.append(f"{group}: duplicate id {identity}")
                continue
            if not isinstance(kind, str) or not (0 <= pos[0] < width and 0 <= pos[1] < height):
                warnings.append(f"{group}: invalid kind/position {identity}")
                continue
            def number(key):
                v = value.get(key)
                return v if integer(v, 0) else None
            bag = value.get("backpack")
            bag = tuple(bag) if isinstance(bag, list) and all(isinstance(x, str) for x in bag) else None
            result[identity] = Unit(identity, kind, pos, number("health"), number("level"),
                                    number("attackRange"), number("attackPower"), number("cooldown"),
                                    number("backPackCapability"), bag,
                                    value.get("targetTeam") if value.get("targetTeam") in {"challenger", "defender"} else None,
                                    value.get("abnormalState", ""))
        return dict(sorted(result.items()))

    ours, enemies, robots = (units(g) for g in ("teamOur", "teamEnemy", "robot"))
    zones = defaultdict(set)
    for value in array(map_info.get("zones")):
        value = obj(value)
        pos = position(value.get("pos"))
        if pos is not None:
            occupied.add(pos)
            if isinstance(value.get("neutralType"), str):
                zones[value["neutralType"]].add(pos)

    def prices(key):
        result, duplicates = {}, set()
        for value in array(raw.get(key)):
            value = obj(value)
            name, price = value.get("name"), value.get("price")
            if not isinstance(name, str) or not integer(price, 0):
                continue
            if name in result or name in duplicates:
                result.pop(name, None)
                duplicates.add(name)
            else:
                result[name] = price
        return result

    def team_number(key):
        v = team.get(key)
        return v if integer(v, 0) else None

    return World(raw, round_no, width, height, team["type"], str(team_id), team_number("goldNum"),
                 team_number("totalScore"), ours, enemies, robots, dict(zones), occupied,
                 prices("vendorShopList"), prices("weaponShopList"),
                 [t for t in array(team.get("playerTasks")) if isinstance(t, dict)],
                 raw.get("phaseTask") if isinstance(raw.get("phaseTask"), str) else "", warnings[:64])


FIELDS = {
    "move": ({"targetPos"}, set()), "attack": ({"controllerId", "targetPos"}, set()),
    "sell": ({"name"}, {"num"}), "buy": ({"name"}, {"num"}),
    "build": ({"name", "targetPos"}, set()), "remove": ({"targetPos"}, set()),
    "acceptTask": (set(), set()), "submitAnswer": ({"taskAnswer"}, set()),
    "summonTreasure": ({"targetPos", "item"}, set()), "use": ({"name"}, {"targetPos"}),
    "drop": ({"name"}, set()), "collect": ({"targetPos"}, set()),
}


def validate_command(command):
    if not isinstance(command, dict) or command.get("action") not in FIELDS:
        raise ProtocolError("unknown action")
    action = command["action"]
    required, optional = FIELDS[action]
    keys = set(command) - {"action"}
    if not required <= keys or not keys <= required | optional:
        raise ProtocolError("missing or extra command fields")
    for field in ("name", "taskAnswer", "controllerId"):
        if field in command and (not isinstance(command[field], str) or not command[field]):
            raise ProtocolError(f"invalid {field}")
    if "controllerId" in command and not command["controllerId"].isdecimal():
        raise ProtocolError("invalid controller ID")
    if "num" in command and not integer(command["num"], 1):
        raise ProtocolError("invalid quantity")
    if "targetPos" in command:
        targets = command["targetPos"]
        if not isinstance(targets, list) or not targets or len(targets) > 3:
            raise ProtocolError("targetPos must contain 1..3 coordinates")
        if action != "attack" and len(targets) != 1:
            raise ProtocolError("action requires one position")
        if any(position(p) is None or set(p) != {"x", "y"} for p in targets):
            raise ProtocolError("invalid coordinate")
    if "item" in command and (not isinstance(command["item"], list)
                               or not all(isinstance(x, str) and x for x in command["item"])):
        raise ProtocolError("invalid sacrifice list")


def empty_response():
    # Structurally valid. Official acceptance of an empty action map remains PRO-002.
    return {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}


def validate_response(response):
    if not isinstance(response, dict) or set(response) != {"roleCommandMap", "prompt", "executeCmd"}:
        raise ProtocolError("invalid response fields")
    if not isinstance(response["prompt"], str) or not isinstance(response["executeCmd"], str):
        raise ProtocolError("channel fields must be strings")
    if not isinstance(response["roleCommandMap"], dict):
        raise ProtocolError("roleCommandMap must be object")
    for identity, command in response["roleCommandMap"].items():
        if not isinstance(identity, str) or not identity.isdecimal():
            raise ProtocolError("invalid actor ID")
        validate_command(command)
    json.dumps(response, ensure_ascii=False, allow_nan=False)
