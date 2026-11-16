"""The fake phone that stands in for a player when testing the game engine.

Its messages must be the ones the engine expects, or an engine test would pass or fail for
the wrong reason. The MQTT parts run against a real Mosquitto.
"""

import asyncio
import contextlib
import json
import logging
import sys
import uuid
from pathlib import Path

import aiomqtt
import pytest

import mqtt_client
from helpers import BROKER_DEADLINE_S, on_selector, wait_until

import ai_events  # noqa: E402 - ultra96/, on sys.path via conftest
import envelope  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fake_phone  # noqa: E402

# The fields of each message the phone sends the engine
LANE_FIELDS = {
    "match_id": str, "hasLane": bool, "lane_length_m": float, "hasPosition": bool,
    "along": float, "lat": float, "locked": bool, "hasAim": bool, "aimAlong": float,
    "aimLat": float,
}
REQUEST_FIELDS = {
    "match_id": str, "req_id": int, "action": str, "unitType": str, "spell": str,
    "hasAim": bool, "along": float, "lat": float,
}


def check_fields(payload, fields):
    """Every field present, nothing extra, and each of exactly its type."""
    assert set(payload) == set(fields)
    for name, kind in fields.items():
        assert type(payload[name]) is kind, name


def match(phase, match_id="m-1", **flags):
    return {"match_id": match_id, "phase": phase, **flags}


# --- what it sends, without a broker ----------------------------------------


def test_a_lane_sample_has_the_spec_fields_and_types():
    phone = fake_phone.Phone(2)
    check_fields(phone.lane(), LANE_FIELDS)
    phone.on_match(match("running"))
    check_fields(phone.lane(), LANE_FIELDS)


def test_position_and_aim_only_once_this_match_has_run():
    """A real phone has no frame until its match starts, and a new match resets it."""
    phone = fake_phone.Phone(2)
    phone.on_match(match("waiting"))
    assert phone.lane()["hasPosition"] is False
    assert phone.lane()["hasAim"] is False

    phone.on_match(match("running"))
    sample = phone.lane()
    assert (sample["hasPosition"], sample["along"], sample["lat"]) == (True, 1.0, 0.0)
    assert (sample["hasAim"], sample["aimAlong"], sample["aimLat"]) == (True, 0.75, 0.0)

    phone.on_match(match("waiting", match_id="m-2"))
    assert phone.lane()["hasPosition"] is False
    assert phone.lane()["match_id"] == "m-2"


def test_start_is_sent_again_with_the_same_req_id_until_it_is_counted():
    phone = fake_phone.Phone(2, auto_start=True)
    phone.on_match(match("waiting", start2=False))

    first = phone.start_due(0.0)
    check_fields(first, REQUEST_FIELDS)
    assert (first["action"], first["match_id"], first["hasAim"]) == ("start", "m-1", False)
    assert phone.start_due(fake_phone.START_RESEND_S - 0.1) is None
    assert phone.start_due(fake_phone.START_RESEND_S) == first

    phone.on_match(match("waiting", start2=True))
    assert phone.start_due(10.0) is None


def test_a_new_match_gets_a_new_start():
    phone = fake_phone.Phone(2, auto_start=True)
    phone.on_match(match("waiting"))
    first = phone.start_due(0.0)

    phone.on_match(match("waiting", match_id="m-2"))
    second = phone.start_due(0.1)
    assert second["match_id"] == "m-2"
    assert second["req_id"] > first["req_id"]


def test_a_finished_match_gets_a_rematch():
    phone = fake_phone.Phone(2, auto_start=True)
    phone.on_match(match("ended", start2=False))
    assert phone.start_due(0.0)["action"] == "rematch"


def test_no_start_without_the_flag_or_a_match_id():
    not_asked = fake_phone.Phone(2)
    not_asked.on_match(match("waiting"))
    no_match_yet = fake_phone.Phone(2, auto_start=True)
    no_match_yet.on_match({"phase": "waiting"})

    assert not_asked.start_due(0.0) is None
    assert no_match_yet.start_due(0.0) is None


def test_taps_only_while_running_and_every_so_often():
    phone = fake_phone.Phone(1, tap="Warrior", every=3.0)
    phone.on_match(match("waiting"))
    assert phone.tap_due(0.0) is None

    phone.on_match(match("running"))
    tap = phone.tap_due(0.0)
    check_fields(tap, REQUEST_FIELDS)
    assert (tap["action"], tap["unitType"], tap["spell"]) == ("spawn", "Warrior", "")
    assert (tap["hasAim"], tap["along"], tap["lat"]) == (True, 0.25, 0.0)
    assert phone.tap_due(2.9) is None
    assert phone.tap_due(3.0)["req_id"] == tap["req_id"] + 1

    phone.on_match(match("paused"))
    assert phone.tap_due(10.0) is None


def test_a_spell_tap_is_a_spell_request():
    phone = fake_phone.Phone(2, tap="lightning")
    phone.on_match(match("running"))
    tap = phone.tap_due(0.0)
    assert (tap["action"], tap["unitType"], tap["spell"]) == ("spell", "", "lightning")


# --- against a real broker ----------------------------------------------------


def new_client(port):
    return aiomqtt.Client("127.0.0.1", port, identifier=f"test-{uuid.uuid4().hex[:6]}")


async def publish(port, topic, payload, retain=False):
    """Publish a payload in an envelope, as the engine would."""
    env = envelope.build_envelope(payload, 0, source="test-engine", session=uuid.uuid4().hex[:8])
    async with new_client(port) as client:
        await client.publish(topic, json.dumps(env), qos=1, retain=retain)


async def first_message(port, topic, wanted=lambda env: True):
    """Return (envelope, message) for the first message on `topic` that `wanted` accepts."""
    async with new_client(port) as client:
        await client.subscribe(topic, qos=1)
        async for message in client.messages:
            env = mqtt_client.parse_envelope(message.payload)
            if wanted(env):
                return env, message


@contextlib.asynccontextmanager
async def phone_running(phone, port):
    stop = asyncio.Event()
    task = asyncio.create_task(fake_phone.run(phone, "127.0.0.1", port, stop))
    try:
        yield task
    finally:
        stop.set()
        await asyncio.wait_for(task, BROKER_DEADLINE_S)


def test_the_lane_stream_follows_the_retained_match(broker):
    phone = fake_phone.Phone(2)

    async def scenario():
        await publish(broker.port, "game/match", match("running"), retain=True)
        async with phone_running(phone, broker.port):
            env, message = await first_message(
                broker.port, "viz/lane/2", lambda env: env["payload"]["match_id"] == "m-1"
            )
        assert env["source"] == phone.source
        assert env["payload"]["hasPosition"] is True
        assert message.qos == 0

    on_selector(scenario())


def test_start_goes_out_on_game_request(broker):
    phone = fake_phone.Phone(2, auto_start=True)

    async def scenario():
        await publish(broker.port, "game/match", match("waiting", start2=False), retain=True)
        async with phone_running(phone, broker.port):
            env, message = await first_message(broker.port, "game/request/2")
        check_fields(env["payload"], REQUEST_FIELDS)
        assert (env["payload"]["action"], env["payload"]["match_id"]) == ("start", "m-1")
        assert message.qos == 1

    on_selector(scenario())


def test_it_logs_each_event_and_skips_what_isnt_an_envelope(broker, caplog):
    phone = fake_phone.Phone(2)
    event = {"player": 2, "input": "tap", "action": "spawn", "what": "Warrior", "ok": True,
             "reason": "", "unit_id": 5}

    async def scenario():
        async with phone_running(phone, broker.port):
            await wait_until(lambda: "on mqtt://" in caplog.text, BROKER_DEADLINE_S)
            async with aiomqtt.Client("127.0.0.1", broker.port, identifier="test-raw") as client:
                await client.publish("game/event", "not json", qos=1)
            await publish(broker.port, "game/event", event)
            await wait_until(lambda: "unit 5" in caplog.text, BROKER_DEADLINE_S)

    with caplog.at_level(logging.INFO, logger="fake_phone"):
        on_selector(scenario())
    assert "isn't an envelope" in caplog.text
    assert "event player 2 tap spawn 'Warrior': ok, unit 5" in caplog.text


def test_a_lost_broker_ends_it(broker, caplog, monkeypatch):
    """It has no reconnect, so it must end rather than hang."""
    # Stop the broker only while nothing is sending. On Windows, aiomqtt 2.5.1 can crash the whole
    # event loop (WinError 10038) if the broker goes away in the middle of a send, and then this
    # test couldn't show whether the phone hangs. So the phone sends once every 5 s, and the
    # broker is stopped just after the first sample has arrived.
    monkeypatch.setattr(fake_phone, "LANE_HZ", 0.2)
    phone = fake_phone.Phone(2)

    async def scenario():
        async with aiomqtt.Client("127.0.0.1", broker.port, identifier="test-watch") as watch:
            await watch.subscribe("viz/lane/2")
            task = asyncio.create_task(fake_phone.run(phone, "127.0.0.1", broker.port))
            # A subscriber has the first sample, so that send is over, and the next is 5 s away
            await asyncio.wait_for(anext(watch.messages), BROKER_DEADLINE_S)
        broker.stop()  # after the subscriber has disconnected, so it isn't sending either
        with pytest.raises(aiomqtt.MqttError):
            await asyncio.wait_for(task, BROKER_DEADLINE_S)

    with caplog.at_level(logging.INFO, logger="fake_phone"):
        on_selector(scenario())


def test_a_gesture_is_the_relays_own_event(broker):
    """The same builders, source, topic and QoS as the board's relay."""

    async def scenario():
        async with aiomqtt.Client("127.0.0.1", broker.port, identifier="test-board") as board:
            await board.subscribe("game/gesture/1", qos=1)
            await fake_phone.send_gesture("127.0.0.1", broker.port, 1, "warrior")
            return await asyncio.wait_for(anext(board.messages), BROKER_DEADLINE_S)

    message = on_selector(scenario())
    env = mqtt_client.parse_envelope(message.payload)
    relay = envelope.build_envelope(ai_events._event(1, 0, "warrior"), 0)
    assert set(env) == set(relay)
    assert set(env["payload"]) == set(relay["payload"])
    assert env["source"] == "ultra96-relay"
    assert env["event_id"].startswith("ultra96-relay:")
    assert (env["payload"]["type"], env["payload"]["player"], env["payload"]["gesture"]) == (
        "AI_EVENT", 1, "warrior"
    )
    assert message.qos == 1
