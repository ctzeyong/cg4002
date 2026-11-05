"""The game engine's messages: every field read is checked, and every field sent is in range."""

import dataclasses
import json
import math
import sys
from pathlib import Path

import pytest

import ai_events  # noqa: E402 - ultra96/, on sys.path via conftest
import engine_wire as wire  # noqa: E402
import envelope  # noqa: E402
from stub_core import match as stub

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fake_phone  # noqa: E402

HUGE = 10**400  # an int json.loads accepts, which math.isfinite can't take
# values every number field must refuse
NOT_A_NUMBER = [math.nan, math.inf, -math.inf, "1", True, None, [1]]


def raw(env):
    """Encode as a phone could, NaN and infinity included."""
    return json.dumps(env).encode()


def env(body, **fields):
    base = {"event_id": "phone:abc:1", "source": "phone-abc", "seq": 1, "ts": 1.5, "payload": body}
    return dict(base, **fields)


LANE = {
    "match_id": "e1-1", "hasLane": True, "lane_length_m": 2.0, "hasPosition": True,
    "along": 0.0, "lat": 0.1, "locked": True, "hasAim": True, "aimAlong": 0.25, "aimLat": -0.1,
}
SPAWN = {
    "match_id": "e1-1", "req_id": 3, "action": "spawn", "unitType": "Warrior", "spell": "",
    "hasAim": True, "along": 0.25, "lat": 0.0,
}
START = dict(SPAWN, action="start", unitType="", hasAim=False, along=0.0, lat=0.0)


def changed(payload, **fields):
    return dict(payload, **fields)


# --- the envelope ------------------------------------------------------------------


def test_a_good_envelope_gives_its_id_source_and_payload():
    assert wire.read_envelope(raw(env({"a": 1}))) == ("phone:abc:1", "phone-abc", {"a": 1})


@pytest.mark.parametrize(
    "field, value",
    [("event_id", v) for v in ("", 5, None)]
    + [("source", v) for v in ("", 5, None)]
    + [("seq", v) for v in (-1, True, 1.5, "1", None)]
    + [("ts", v) for v in NOT_A_NUMBER]
    + [("payload", v) for v in ([], "x", None, 5)],
)
def test_each_envelope_field_is_checked(field, value):
    with pytest.raises(wire.BadMessage) as caught:
        wire.read_envelope(raw(env({}, **{field: value})))
    assert caught.value.field == field


@pytest.mark.parametrize("text", [b"not json", b"[1, 2]", b'"x"', b"\xff\xfe"])
def test_a_message_that_isnt_a_json_object_is_bad(text):
    with pytest.raises(wire.BadMessage):
        wire.read_envelope(text)


def test_a_message_nested_too_deep_to_read_is_bad():
    with pytest.raises(wire.BadMessage):
        wire.read_envelope(b"[" * 100_000)


def test_1e309_in_a_number_field_is_infinity_and_refused():
    text = raw(env({}, ts=0)).replace(b'"ts": 0', b'"ts": 1e309')
    with pytest.raises(wire.BadMessage):
        wire.read_envelope(text)


# --- viz/lane ----------------------------------------------------------------------


def test_a_lane_sample_gives_lane_position_and_aim():
    assert wire.read_lane(LANE) == wire.Lane("e1-1", 2.0, (0.0, 0.1), (0.25, -0.1))


def test_the_fake_phones_samples_read_before_and_after_the_match_runs():
    phone = fake_phone.Phone(2)
    phone.on_match({"match_id": "e1-1", "phase": "waiting"})
    assert wire.read_lane(phone.lane()) == wire.Lane("e1-1", 2.0, None, None)
    phone.on_match({"match_id": "e1-1", "phase": "running"})
    assert wire.read_lane(phone.lane()) == wire.Lane("e1-1", 2.0, (1.0, 0.0), (0.75, 0.0))


@pytest.mark.parametrize(
    "field, value",
    [("match_id", v) for v in (5, None, True)]
    + [(f, v) for f in ("hasLane", "hasPosition", "locked", "hasAim") for v in ("yes", 1, None)]
    + [("lane_length_m", v) for v in NOT_A_NUMBER]
    + [(f, v) for f in ("along", "lat", "aimAlong", "aimLat")
       for v in NOT_A_NUMBER + [10.5, -10.01, HUGE]],
)
def test_each_lane_field_is_checked(field, value):
    with pytest.raises(wire.BadMessage) as caught:
        wire.read_lane(changed(LANE, **{field: value}))
    assert caught.value.field == field


def test_a_lane_field_whose_flag_is_false_isnt_read():
    sample = changed(LANE, hasLane=False, lane_length_m="x", hasPosition=False, along="x",
                     lat=None, hasAim=False, aimAlong="x", aimLat=None)
    assert wire.read_lane(sample) == wire.Lane("e1-1", None, None, None)


def test_there_is_no_aim_without_a_position():
    assert wire.read_lane(changed(LANE, hasPosition=False)).aim is None


def test_a_lane_out_of_range_is_passed_on_for_the_engine_to_refuse():
    """Dropping the sample would make a phone that measures 12 m look gone, and pause the match."""
    assert wire.read_lane(changed(LANE, lane_length_m=12.0)).lane_m == 12.0


def test_coordinates_at_the_limit_are_accepted():
    sample = changed(LANE, along=10, lat=-10.0, aimAlong=-10, aimLat=10.0)
    assert wire.read_lane(sample).aim == (-10, 10.0)


# --- game/request --------------------------------------------------------------------


def test_spawn_spell_and_start_requests_read():
    assert wire.read_request(SPAWN) == wire.Request("e1-1", 3, "spawn", "Warrior", (0.25, 0.0))
    spell = changed(SPAWN, action="spell", unitType="", spell="fireball")
    assert wire.read_request(spell) == wire.Request("e1-1", 3, "spell", "fireball", (0.25, 0.0))
    assert wire.read_request(START) == wire.Request("e1-1", 3, "start", "", None)


def test_the_fake_phones_requests_read():
    phone = fake_phone.Phone(1, auto_start=True, tap="lightning")
    phone.on_match({"match_id": "e1-1", "phase": "waiting"})
    assert wire.read_request(phone.start_due(0.0)).action == "start"
    phone.on_match({"match_id": "e1-1", "phase": "running"})
    assert wire.read_request(phone.tap_due(0.0)).name == "lightning"


@pytest.mark.parametrize(
    "field, value",
    [("match_id", v) for v in ("", 5, None)]
    + [("req_id", v) for v in (-1, True, 1.5, "3", None, 2**31)]
    + [("action", v) for v in ("Start", "", None, ["spawn"])]
    + [("unitType", v) for v in ("warrior", "Dragon", "", None)]
    + [("spell", "fireball")]
    + [("hasAim", v) for v in ("yes", 1, None)]
    + [(f, v) for f in ("along", "lat") for v in NOT_A_NUMBER + [10.5, HUGE]],
)
def test_each_request_field_is_checked(field, value):
    with pytest.raises(wire.BadMessage) as caught:
        wire.read_request(changed(SPAWN, **{field: value}))
    assert caught.value.field == field


def test_a_spell_must_name_a_spell_and_no_unit():
    spell = changed(SPAWN, action="spell", unitType="", spell="fireball")
    for field, value in (("spell", "ice"), ("spell", ""), ("unitType", "Warrior")):
        with pytest.raises(wire.BadMessage):
            wire.read_request(changed(spell, **{field: value}))


def test_a_bad_tap_with_a_good_req_id_can_still_be_answered():
    assert wire.refusable_tap(changed(SPAWN, unitType="Dragon")) == (3, "spawn")
    assert wire.refusable_tap(changed(SPAWN, action="spell", along="x")) == (3, "spell")
    assert wire.refusable_tap(changed(SPAWN, req_id=True)) is None
    assert wire.refusable_tap(changed(SPAWN, req_id=-1)) is None
    assert wire.refusable_tap(changed(SPAWN, req_id=2**31)) is None


def test_the_largest_req_id_the_phone_can_hold_is_accepted():
    assert wire.read_request(changed(SPAWN, req_id=2**31 - 1)).req_id == 2**31 - 1


def test_a_bad_start_is_dropped_with_no_answer():
    bad = changed(START, hasAim="yes")
    with pytest.raises(wire.BadMessage):
        wire.read_request(bad)
    assert wire.refusable_tap(bad) is None


# --- viz/bubble_captured, game/gesture, status/gloves -----------------------------------


def test_a_bubble_reads_its_match_and_id_and_ignores_the_rest():
    bubble = {"match_id": "e1-1", "bubble_id": 1, "event": "x", "player": "nobody"}
    assert wire.read_bubble(bubble, bubble_count=2) == ("e1-1", 1)


@pytest.mark.parametrize(
    "field, value",
    [("match_id", ""), ("match_id", None)] + [("bubble_id", v) for v in (1, -1, True, 0.0, "0")],
)
def test_each_bubble_field_is_checked(field, value):
    bubble = changed({"match_id": "e1-1", "bubble_id": 0}, **{field: value})
    with pytest.raises(wire.BadMessage):
        wire.read_bubble(bubble, bubble_count=1)


def test_the_relays_ai_event_reads_as_its_gesture():
    assert wire.read_gesture(ai_events._event(2, 7, "warrior"), 2) == "warrior"


@pytest.mark.parametrize(
    "field, value",
    [("type", "SENSOR"), ("player", 1), ("player", True), ("player", "2"), ("seq", -1),
     ("seq", True), ("gesture", ""), ("gesture", None)]
    + [("timestamp", v) for v in NOT_A_NUMBER],
)
def test_each_gesture_field_is_checked(field, value):
    event = changed(ai_events._event(2, 7, "warrior"), **{field: value})
    with pytest.raises(wire.BadMessage) as caught:
        wire.read_gesture(event, 2)
    assert caught.value.field == field


def test_only_ready_is_a_ready_glove():
    assert wire.read_gloves({"player1": "ready", "player2": "down"}) == {1: True, 2: False}
    assert wire.read_gloves({"player1": "READY", "player2": True}) == {1: False, 2: False}
    assert wire.read_gloves({}) == {1: False, 2: False}


# --- what the engine sends --------------------------------------------------------------

STATE_FIELDS = {
    "match_id", "tick", "matchTimeRemaining", "towerHP1", "towerMaxHP1", "towerHP2",
    "towerMaxHP2", "mana1", "mana2", "units", "strikes",
}
UNIT_FIELDS = {"id", "type", "owner", "along", "lat", "hp", "maxHp", "state", "target", "range",
               "faceAlong", "faceLat"}
STRIKE_FIELDS = {"id", "owner", "along", "lat", "delay", "kind", "fromAlong", "fromLat", "burn"}
MATCH_FIELDS = {"match_id", "phase", "reason", "countdown_ms", "phone1", "phone2", "glove1",
                "glove2", "start1", "start2", "winner", "tick_hz", "rules_version"}
EVENT_FIELDS = {"match_id", "player", "input", "req_id", "gesture_event_id", "gesture", "action",
                "what", "ok", "reason", "unit_id"}


def unit(**fields):
    base = dict(id=1, type="Warrior", owner=1, along=0.3, lat=0.0, hp=150.0, max_hp=150.0,
                state="Walking", target=None, range=0.0, face_along=1.0, face_lat=0.0)
    return stub.UnitView(**dict(base, **fields))


def strike(**fields):
    base = dict(id=1, owner=2, kind="fireball", along=0.5, lat=0.1, delay=0.4, from_along=1.0,
                from_lat=0.0, burn=4.0)
    return stub.StrikeView(**dict(base, **fields))


def snapshot(**fields):
    base = dict(tick=12, time_left=178.8, tower_hp=(1000.0, 940.0), tower_max_hp=(1000.0, 1000.0),
                mana=(5.0, 3.5), units=(unit(), unit(id=2, owner=2, target=1, state="Attacking")),
                strikes=(strike(),))
    return stub.Snapshot(**dict(base, **fields))


def test_a_snapshot_has_every_field_for_each_player():
    state = wire.state_payload(snapshot(), "e1-1")
    assert set(state) == STATE_FIELDS
    assert (state["towerHP2"], state["mana2"], state["tick"]) == (940.0, 3.5, 12)
    assert [set(u) for u in state["units"]] == [UNIT_FIELDS, UNIT_FIELDS]
    assert set(state["strikes"][0]) == STRIKE_FIELDS
    assert [(u["owner"], u["target"]) for u in state["units"]] == [("player1", -1), ("player2", 1)]
    assert state["units"][0]["maxHp"] == 150.0 and state["strikes"][0]["fromAlong"] == 1.0


def test_the_values_at_each_limit_are_sent():
    state = wire.state_payload(
        snapshot(tick=2**31 - 1, time_left=3600, units=(unit(id=1_000_000, along=-10, hp=1e6),)),
        "e1-1",
    )
    assert state["units"][0]["id"] == 1_000_000


@pytest.mark.parametrize(
    "fields",
    [
        {"tick": 2**31}, {"tick": 1.0}, {"tick": True}, {"tick": -1},
        {"time_left": 3601}, {"time_left": math.nan}, {"time_left": math.inf},
        {"tower_hp": (1e6 + 1, 0.0)}, {"tower_max_hp": (0.0, -1.0)}, {"mana": (1001.0, 0.0)},
        {"mana": (math.nan, 0.0)},
        {"units": (unit(id=1_000_001),)}, {"units": (unit(id=3_000_000_000),)},
        {"units": (unit(id=1.0),)}, {"units": (unit(), unit())},
        {"units": (unit(target=7),)}, {"units": (unit(target=1.0),)},
        {"units": (unit(target=[1]),)},
        {"units": (unit(along=10.5),)}, {"units": (unit(hp=1e100),)}, {"units": (unit(range=101),)},
        {"units": (unit(state="Flying"),)}, {"units": (unit(type="Dragon"),)},
        {"units": (unit(owner=3),)}, {"units": (unit(owner=True),)},
        {"units": (unit(face_lat=math.nan),)},
        {"strikes": (strike(kind="ice"),)}, {"strikes": (strike(delay=61),)},
        {"strikes": (strike(burn=-1),)}, {"strikes": (strike(), strike())},
        {"strikes": (strike(from_lat=math.inf),)},
    ],
)
def test_a_snapshot_the_phones_would_reject_is_never_sent(fields):
    with pytest.raises((ValueError, TypeError)):
        wire.state_payload(snapshot(**fields), "e1-1")


def test_a_match_message_has_every_field():
    info = wire.MatchInfo("e1-2", "countdown", "", (True, True), (True, False), (False, True),
                          "none", "r-1")
    payload = wire.match_payload(info, 2400, 10)
    assert set(payload) == MATCH_FIELDS
    assert (payload["glove2"], payload["start1"], payload["countdown_ms"]) == (False, False, 2400)


def test_offline_belongs_to_no_match():
    payload = wire.offline_payload("game engine stopped", 10, "r-1")
    assert set(payload) == MATCH_FIELDS
    assert (payload["match_id"], payload["phase"], payload["winner"]) == ("", "offline", "none")
    assert not any(payload[f"{name}{p}"] for name in ("phone", "glove", "start") for p in (1, 2))


def test_an_event_message_has_every_field():
    event = wire.GameEvent(1, "glove", ok=True, action="spawn", what="Warrior",
                           gesture_event_id="ultra96-relay:ab:4", gesture="warrior", unit_id=5)
    payload = wire.event_payload(event, "e1-1")
    assert set(payload) == EVENT_FIELDS
    assert payload == dict(dataclasses.asdict(event), match_id="e1-1")


def test_every_message_the_engine_sends_survives_strict_json():
    state = wire.state_payload(snapshot(), "e1-1")
    wrapped = envelope.build_envelope(state, 0, source="ultra96-engine")
    assert json.loads(json.dumps(wrapped, allow_nan=False))["payload"] == state
