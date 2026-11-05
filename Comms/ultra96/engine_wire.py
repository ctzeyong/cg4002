"""Read and write the game engine's MQTT messages.

This is the only file that knows the messages' field names. It checks every field the
phones and the relay send before the engine uses one, and it builds what the engine sends.
A message that fails a check raises BadMessage, which names the field.
"""

import json
import math
from dataclasses import dataclass

COORD_MAX = 10.0  # lane units, enough for a player standing outside the arena
UNIT_TYPES = ("Warrior", "Rogue", "Mage", "Tank", "Hero")
SPELLS = ("lightning", "fireball")
# god_on and god_off are the demo's debug God mode for the player who sent them (spec 3.4). They get no game/event.
ACTIONS = ("start", "rematch", "spawn", "spell", "god_on", "god_off")
UNIT_STATES = ("Walking", "Attacking")
REQ_ID_MAX = 2**31 - 1  # the phone reads req_id into a C# int
# what each game/event action may name
EVENT_NAMES = {"spawn": UNIT_TYPES, "spell": SPELLS, "bubble": ("",), "none": ("",)}
CORE_REASONS = (
    "not_running", "no_mana", "bad_aim", "tower_out_of_arena", "unknown_gesture", "cooldown",
)


class BadMessage(ValueError):
    """A message with a field that fails its check."""

    def __init__(self, field, value):
        super().__init__(f"{field} is {_short(value)}")
        self.field = field


@dataclass(frozen=True)
class Lane:
    """One viz/lane sample. None where the phone has no lane, position or aim."""

    match_id: str
    lane_m: float | None
    position: tuple[float, float] | None
    aim: tuple[float, float] | None


@dataclass(frozen=True)
class Request:
    """One game/request. `name` is the unit or spell, "" for start and rematch."""

    match_id: str
    req_id: int
    action: str
    name: str
    aim: tuple[float, float] | None


@dataclass(frozen=True)
class GameEvent:
    """The result of one action, for game/event."""

    player: int
    input: str
    ok: bool
    reason: str = ""
    action: str = "none"
    what: str = ""
    req_id: int = -1
    gesture_event_id: str = ""
    gesture: str = ""
    unit_id: int = -1


@dataclass(frozen=True)
class MatchInfo:
    """What game/match says, without countdown_ms, which changes every tick."""

    match_id: str
    phase: str
    reason: str
    phones: tuple[bool, bool]
    gloves: tuple[bool, bool]
    starts: tuple[bool, bool]
    winner: str
    rules_version: str


# --- checks -------------------------------------------------------------------


def _short(value):
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _is_int(value):
    return type(value) is int  # not bool, which is an int in Python


def _is_number(value):
    # ints are always finite, and math.isfinite can't take a huge one
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _get(payload, name, ok):
    value = payload.get(name)
    if not ok(value):
        raise BadMessage(name, value)
    return value


def _flag(payload, name):
    return _get(payload, name, lambda v: type(v) is bool)


def _text(payload, name, allow_empty=True):
    return _get(payload, name, lambda v: type(v) is str and (allow_empty or v != ""))


def _coord(value):
    return _is_number(value) and abs(value) <= COORD_MAX


def _point(payload, along, lat):
    """Read a position in lane units, each part within COORD_MAX."""
    return (_get(payload, along, _coord), _get(payload, lat, _coord))


# --- what the engine reads ------------------------------------------------------


def read_envelope(raw):
    """Return (event_id, source, payload) from one MQTT message's bytes."""
    try:
        env = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise BadMessage("message", raw[:40]) from exc
    if not isinstance(env, dict):
        raise BadMessage("message", env)
    event_id = _text(env, "event_id", allow_empty=False)
    source = _text(env, "source", allow_empty=False)
    _get(env, "seq", lambda v: _is_int(v) and v >= 0)
    _get(env, "ts", _is_number)
    payload = _get(env, "payload", lambda v: isinstance(v, dict))
    return event_id, source, payload


def read_lane(payload):
    """Return a viz/lane payload as a Lane. A field whose flag is false isn't read."""
    match_id = _text(payload, "match_id")
    has_lane = _flag(payload, "hasLane")
    has_position = _flag(payload, "hasPosition")
    _flag(payload, "locked")
    has_aim = _flag(payload, "hasAim")
    # range checked by the engine, so a misread lane doesn't make the phone look gone
    lane_m = _get(payload, "lane_length_m", _is_number) if has_lane else None
    position = _point(payload, "along", "lat") if has_position else None
    # no aim before a position, since the phone has no frame yet
    aim = _point(payload, "aimAlong", "aimLat") if has_aim and has_position else None
    return Lane(match_id, lane_m, position, aim)


def read_request(payload):
    """Return a game/request payload as a Request."""
    match_id = _text(payload, "match_id", allow_empty=False)
    req_id = _get(payload, "req_id", lambda v: _is_int(v) and 0 <= v <= REQ_ID_MAX)
    action = _get(payload, "action", lambda v: v in ACTIONS)
    unit = _get(payload, "unitType", lambda v: v in (UNIT_TYPES if action == "spawn" else ("",)))
    spell = _get(payload, "spell", lambda v: v in (SPELLS if action == "spell" else ("",)))
    has_aim = _flag(payload, "hasAim")
    aim = _point(payload, "along", "lat") if has_aim else None
    return Request(match_id, req_id, action, unit or spell, aim)


def refusable_tap(payload):
    """Return (req_id, action) of a bad spawn or spell that can still be answered, else None.

    A phone waits for an answer to each card it taps. A bad START gets none.
    """
    action, req_id = payload.get("action"), payload.get("req_id")
    if action in ("spawn", "spell") and _is_int(req_id) and 0 <= req_id <= REQ_ID_MAX:
        return req_id, action
    return None


def read_bubble(payload, bubble_count):
    """Return (match_id, bubble_id) from a viz/bubble_captured payload."""
    match_id = _text(payload, "match_id", allow_empty=False)
    bubble_id = _get(payload, "bubble_id", lambda v: _is_int(v) and 0 <= v < bubble_count)
    return match_id, bubble_id


def read_gesture(payload, player):
    """Return the gesture name from the relay's AI event for `player`."""
    _get(payload, "type", lambda v: v == "AI_EVENT")
    _get(payload, "player", lambda v: _is_int(v) and v == player)
    _get(payload, "seq", lambda v: _is_int(v) and v >= 0)
    _get(payload, "timestamp", _is_number)
    return _text(payload, "gesture", allow_empty=False)


def read_gloves(payload):
    """Return {player: True if ready} from status/gloves. Any other state isn't ready."""
    return {player: payload.get(f"player{player}") == "ready" for player in (1, 2)}


# --- what the engine sends -------------------------------------------------------

# far wider than any tuning, and far inside the phone's C# float and int
TICK_MAX = 2**31 - 1
TIME_MAX = 3600
HP_MAX = 1_000_000
MANA_MAX = 1000
ID_MAX = 1_000_000  # the phone adds 1 to the last unit id, so not up to the int maximum
RANGE_MAX = 100
DELAY_MAX = 60


def _limit(value, low, high, name, integer=False):
    """Return `value` if it has the right type and is in range, else raise ValueError."""
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    right_type = _is_int(value) if integer else number
    if not right_type or not low <= value <= high:  # NaN fails the comparison
        raise ValueError(f"{name} {_short(value)} is outside {low} to {high}")
    return value


def _owner(player):
    if not _is_int(player) or player not in (1, 2):
        raise ValueError(f"owner {_short(player)} is not a player")
    return f"player{player}"


def _choice(value, choices, name):
    if value not in choices:
        raise ValueError(f"{name} {_short(value)} is not one of {choices}")
    return value


def _unit(u, ids):
    target = -1 if u.target is None else _limit(u.target, 0, ID_MAX, "unit target", integer=True)
    if target != -1 and target not in ids:
        raise ValueError(f"unit {u.id} targets {_short(target)}, which isn't in the snapshot")
    return {
        "id": u.id,
        "type": _choice(u.type, UNIT_TYPES, "unit type"),
        "owner": _owner(u.owner),
        "along": _limit(u.along, -COORD_MAX, COORD_MAX, "unit along"),
        "lat": _limit(u.lat, -COORD_MAX, COORD_MAX, "unit lat"),
        "hp": _limit(u.hp, 0, HP_MAX, "unit hp"),
        "maxHp": _limit(u.max_hp, 0, HP_MAX, "unit maxHp"),
        "state": _choice(u.state, UNIT_STATES, "unit state"),
        "target": target,
        "range": _limit(u.range, 0, RANGE_MAX, "unit range"),
        "faceAlong": _limit(u.face_along, -COORD_MAX, COORD_MAX, "unit faceAlong"),
        "faceLat": _limit(u.face_lat, -COORD_MAX, COORD_MAX, "unit faceLat"),
    }


def _strike(s):
    return {
        "id": s.id,
        "owner": _owner(s.owner),
        "kind": _choice(s.kind, SPELLS, "strike kind"),
        "along": _limit(s.along, -COORD_MAX, COORD_MAX, "strike along"),
        "lat": _limit(s.lat, -COORD_MAX, COORD_MAX, "strike lat"),
        "delay": _limit(s.delay, 0, DELAY_MAX, "strike delay"),
        "fromAlong": _limit(s.from_along, -COORD_MAX, COORD_MAX, "strike fromAlong"),
        "fromLat": _limit(s.from_lat, -COORD_MAX, COORD_MAX, "strike fromLat"),
        "burn": _limit(s.burn, 0, DELAY_MAX, "strike burn"),
    }


def _ids(items, name):
    ids = [_limit(item.id, 0, ID_MAX, f"{name} id", integer=True) for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError(f"two {name}s share an id")
    return set(ids)


def state_payload(snapshot, match_id):
    """Return the game/state payload for a core Snapshot.

    Raises ValueError or TypeError if any value can't be sent, so the engine ends the match
    instead of sending something the phones would reject.
    """
    unit_ids = _ids(snapshot.units, "unit")
    _ids(snapshot.strikes, "strike")
    payload = {
        "match_id": match_id,
        "tick": _limit(snapshot.tick, 0, TICK_MAX, "tick", integer=True),
        "matchTimeRemaining": _limit(snapshot.time_left, 0, TIME_MAX, "matchTimeRemaining"),
        "units": [_unit(u, unit_ids) for u in snapshot.units],
        "strikes": [_strike(s) for s in snapshot.strikes],
    }
    for i, player in enumerate((1, 2)):
        payload[f"towerHP{player}"] = _limit(snapshot.tower_hp[i], 0, HP_MAX, "towerHP")
        payload[f"towerMaxHP{player}"] = _limit(snapshot.tower_max_hp[i], 0, HP_MAX, "towerMaxHP")
        payload[f"mana{player}"] = _limit(snapshot.mana[i], 0, MANA_MAX, "mana")
    json.dumps(payload, allow_nan=False)  # raises here, before the ticker keeps it
    return payload


def match_payload(info, countdown_ms, tick_hz):
    """Return the game/match payload for a MatchInfo."""
    payload = {
        "match_id": info.match_id,
        "phase": info.phase,
        "reason": info.reason,
        "countdown_ms": countdown_ms,
        "winner": info.winner,
        "tick_hz": tick_hz,
        "rules_version": info.rules_version,
    }
    for i, player in enumerate((1, 2)):
        payload[f"phone{player}"] = info.phones[i]
        payload[f"glove{player}"] = info.gloves[i]
        payload[f"start{player}"] = info.starts[i]
    return payload


def offline_payload(reason, tick_hz, rules_version):
    """Return the game/match payload for an engine that isn't running. It belongs to no match."""
    info = MatchInfo("", "offline", reason, (False, False), (False, False), (False, False),
                     "none", rules_version)
    return match_payload(info, 0, tick_hz)


def event_payload(event, match_id):
    """Return the game/event payload for a GameEvent."""
    return {
        "match_id": match_id,
        "player": event.player,
        "input": event.input,
        "req_id": event.req_id,
        "gesture_event_id": event.gesture_event_id,
        "gesture": event.gesture,
        "action": event.action,
        "what": event.what,
        "ok": event.ok,
        "reason": event.reason,
        "unit_id": event.unit_id,
    }
