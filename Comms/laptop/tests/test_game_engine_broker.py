"""The game engine over MQTT: its tasks, then a real Mosquitto, then main() as the board runs it.

Retained messages, the will and reconnects are tested against a real broker, since a fake one
would only fake the behaviour being tested.
"""

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import aiomqtt
import pytest

from helpers import BROKER_DEADLINE_S, on_selector, wait_until

import envelope  # noqa: E402 - ultra96/, on sys.path via conftest
import game_engine as ge  # noqa: E402
from stub_core import match as stub

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import fake_phone  # noqa: E402

ENGINE_PY = Path(__file__).resolve().parents[2] / "ultra96" / "game_engine.py"
RULES = {"version": "test-1", "BubbleCaptureManager": {"bubbleCount": 1}}
GLOVES_READY = {"player1": "ready", "player2": "ready"}


def new_engine(rules=RULES):
    session = ge.Session(stub, dict(rules), ge.wire.state_payload, "e1", time.monotonic())
    return ge.Engine(session)


# --- the tasks, on a fake client ---------------------------------------------------------


class FakeClient:
    """Record every publish, as a client would send it.

    With `stall_acks`, a QoS 1 publish never gets its ack. With `lose_link`, it fails.
    """

    def __init__(self, stall_acks=False, lose_link=False):
        self.stall_acks = stall_acks
        self.lose_link = lose_link
        self.sent = []  # (topic, payload, time)

    async def publish(self, topic, payload, qos=0, retain=False, timeout=None):
        await asyncio.sleep(0)  # a real client yields to the loop here
        if qos == 1 and self.stall_acks:
            await asyncio.Event().wait()
        if qos == 1 and self.lose_link:
            raise aiomqtt.MqttError("The client is not currently connected.")
        self.sent.append((topic, json.loads(payload), time.monotonic()))

    def on(self, topic):
        return [payload for t, payload, _ in self.sent if t == topic]


def run_briefly(coro):
    """Run `coro` with a deadline, so a loop that never stops fails instead of hanging."""
    asyncio.run(asyncio.wait_for(coro, timeout=5.0))


async def run_loops(client, engine, seconds):
    ended = asyncio.Event()
    tasks = [
        asyncio.create_task(ge._tick(client, engine, ended)),
        asyncio.create_task(ge._publish_events(client, engine, ended)),
        asyncio.create_task(ge._publish_match(client, engine, ended, engine.session.version)),
    ]
    await asyncio.sleep(seconds)
    ended.set()
    engine.changed.set()
    for task in tasks:
        task.cancel()  # the stalled publish never returns
    await asyncio.gather(*tasks, return_exceptions=True)


def test_game_state_goes_out_once_per_tick_and_never_twice_in_one():
    """asyncio can wake a little early, and two states in one tick would make the phones jump."""
    client, engine = FakeClient(), new_engine()
    session, ticks = engine.session, []
    real_on_tick = session.on_tick

    def counting_on_tick(now):
        before = session.next_tick
        state = real_on_tick(now)
        ticks.append(session.next_tick != before)  # whether this call used up a tick
        return state

    session.on_tick = counting_on_tick
    run_briefly(run_loops(client, engine, 1.05))
    states = client.on(ge.STATE_TOPIC)
    assert states and all(ticks)  # never called before a tick was due
    # one state per tick, though the teardown may cut off the last send
    assert len(ticks) - 1 <= len(states) <= len(ticks)
    assert len({s["event_id"] for s in states}) == len(states)  # a new envelope for each


def test_game_state_never_waits_behind_an_unacknowledged_event():
    client, engine = FakeClient(stall_acks=True), new_engine()
    engine.queue_event(ge.GameEvent(1, "tap", ok=True), time.monotonic())
    run_briefly(run_loops(client, engine, 0.55))
    assert len(client.on(ge.STATE_TOPIC)) >= 4
    assert engine.events.empty() and engine.pending  # taken, and still waiting for its ack


def test_an_unacknowledged_event_is_sent_again_after_a_reconnect_in_the_same_envelope():
    """The core has already applied the action, and a repeated request is deduped."""
    engine = new_engine()
    engine.queue_event(ge.GameEvent(2, "tap", ok=True, unit_id=4), time.monotonic())
    queued = json.loads(engine.events._queue[0][0])["event_id"]
    with pytest.raises(aiomqtt.MqttError):
        run_briefly(ge._publish_events(FakeClient(lose_link=True), engine, asyncio.Event()))
    client = FakeClient()
    run_briefly(run_loops(client, engine, 0.1))
    [event] = client.on(ge.EVENT_TOPIC)
    assert event["event_id"] == queued and event["payload"]["unit_id"] == 4


class EndingQueue(asyncio.Queue):
    """A queue whose get ends the session as it returns, as a swallowed cancel would."""

    def __init__(self, ended):
        super().__init__()
        self.ended = ended

    async def get(self):
        item = await super().get()
        self.ended.set()
        return item


def test_an_event_taken_just_as_the_session_ends_waits_for_the_next_session():
    """wait_for can hand back the item and swallow the cancel, so ended is checked after it."""
    engine, ended, client = new_engine(), asyncio.Event(), FakeClient()

    async def scenario():
        engine.events = EndingQueue(ended)
        engine.queue_event(ge.GameEvent(2, "tap", ok=True), time.monotonic())
        await ge._publish_events(client, engine, ended)

    run_briefly(scenario())
    assert client.on(ge.EVENT_TOPIC) == []
    assert engine.pending  # sent after the reconnect


def test_an_event_older_than_its_ttl_is_dropped_after_a_reconnect():
    """By then the phone has stopped waiting for the result."""
    engine = new_engine()
    engine.queue_event(ge.GameEvent(2, "tap", ok=True), time.monotonic() - ge.EVENT_TTL_S - 0.1)
    client = FakeClient()
    run_briefly(run_loops(client, engine, 0.1))
    assert client.on(ge.EVENT_TOPIC) == []
    assert engine.session.counts["event_stale"] == 1


class LinkLostClient(FakeClient):
    """Never ack a QoS 1 publish after the first, and drop the link when `lost` is set."""

    def __init__(self):
        super().__init__()
        self.lost = asyncio.Event()
        self.qos1 = 0

    async def publish(self, topic, payload, qos=0, retain=False, timeout=None):
        if qos == 1:
            self.qos1 += 1
            self.stall_acks = self.qos1 > 1
        await super().publish(topic, payload, qos, retain, timeout)

    async def subscribe(self, topic, qos=0):
        pass

    @property
    def messages(self):
        return self._messages()

    async def _messages(self):
        await self.lost.wait()
        raise aiomqtt.MqttError("Unexpected disconnection")
        yield


def test_a_lost_link_ends_the_session_at_once_even_with_a_publish_waiting_for_its_ack():
    """aiomqtt doesn't fail a waiting publish when the link drops, so the session must not wait."""
    client, engine = LinkLostClient(), new_engine()

    async def scenario():
        serve = asyncio.create_task(ge._serve(client, engine, asyncio.Event()))
        await asyncio.sleep(0.1)
        engine.queue_event(ge.GameEvent(2, "tap", ok=True), time.monotonic())
        await wait_until(lambda: engine.pending)  # its publish is waiting for an ack
        client.lost.set()
        with pytest.raises(aiomqtt.MqttError):
            await asyncio.wait_for(serve, timeout=1.0)

    run_briefly(scenario())
    assert engine.pending  # sent again after the reconnect


def test_game_match_goes_out_when_it_changes_and_only_then():
    client, engine = FakeClient(), new_engine()

    async def scenario():
        loops = asyncio.create_task(run_loops(client, engine, 0.6))
        await asyncio.sleep(0.3)
        engine.session.on_gloves({1: True, 2: False}, time.monotonic())
        engine.changed.set()
        await loops

    run_briefly(scenario())
    [match] = client.on(ge.MATCH_TOPIC)
    assert (match["payload"]["glove1"], match["payload"]["glove2"]) == (True, False)


# --- against a real broker -------------------------------------------------------------------


async def publish(port, topic, payload, retain=False, source="test"):
    async with aiomqtt.Client("127.0.0.1", port, identifier=f"test-{uuid.uuid4().hex[:6]}") as c:
        env = envelope.build_envelope(payload, 0, source=source, session=uuid.uuid4().hex[:8])
        await c.publish(topic, json.dumps(env), qos=1, retain=retain)


async def retained_match(port):
    """Return the retained game/match envelope a phone connecting now is told, or None."""
    async with aiomqtt.Client("127.0.0.1", port, identifier=f"test-{uuid.uuid4().hex[:6]}") as c:
        await c.subscribe(ge.MATCH_TOPIC, qos=1)
        try:
            message = await asyncio.wait_for(anext(c.messages), timeout=0.5)
        except asyncio.TimeoutError:
            return None
    return json.loads(message.payload) if message.retain else None


async def until_retained(port, check):
    deadline = time.monotonic() + BROKER_DEADLINE_S
    while True:
        env = await retained_match(port)
        if env is not None and check(env["payload"]):
            return env
        assert time.monotonic() < deadline, f"retained game/match is {env}"


@contextlib.asynccontextmanager
async def engine_running(port, engine=None):
    """The real run(), in this process, stopped cleanly at the end."""
    engine = engine or new_engine()
    stop = asyncio.Event()
    task = asyncio.create_task(ge.run("127.0.0.1", port, engine, stop))
    try:
        yield engine, task
    finally:
        stop.set()
        with contextlib.suppress(ge.EngineFatal):
            await asyncio.wait_for(task, timeout=BROKER_DEADLINE_S)


def test_a_match_runs_with_two_fake_phones_a_tap_and_a_glove_gesture(broker, monkeypatch):
    monkeypatch.setattr(ge, "COUNTDOWN_S", 0.3)

    async def scenario():
        await publish(broker.port, envelope.GLOVES_TOPIC, GLOVES_READY, retain=True)
        phones = [fake_phone.Phone(1, auto_start=True), fake_phone.Phone(2, auto_start=True,
                                                                         tap="Mage", every=0.5)]
        stop = asyncio.Event()
        seen = []
        async with aiomqtt.Client("127.0.0.1", broker.port, identifier="test-watch") as watch:
            await watch.subscribe("game/#", qos=1)
            async with engine_running(broker.port):
                tasks = [asyncio.create_task(fake_phone.run(p, "127.0.0.1", broker.port, stop))
                         for p in phones]
                deadline = time.monotonic() + BROKER_DEADLINE_S
                sent_gesture = False
                async for message in watch.messages:
                    assert time.monotonic() < deadline, seen
                    payload = json.loads(message.payload)["payload"]
                    seen.append((message.topic.value, payload))
                    if payload.get("phase") == "running" and not sent_gesture:
                        sent_gesture = True
                        # an aim counts only after RESUME_S of streaming, longer than this countdown
                        await asyncio.sleep(ge.RESUME_S)
                        await fake_phone.send_gesture("127.0.0.1", broker.port, 1, "warrior")
                    events = [p for t, p in seen if t == ge.EVENT_TOPIC]
                    if {e["input"] for e in events} >= {"tap", "glove"}:
                        break
                stop.set()
                await asyncio.gather(*tasks, return_exceptions=True)
        tap = next(p for t, p in seen if t == ge.EVENT_TOPIC and p["input"] == "tap")
        glove = next(p for t, p in seen if t == ge.EVENT_TOPIC and p["input"] == "glove")
        assert (tap["player"], tap["what"], tap["ok"]) == (2, "Mage", True)
        assert glove["player"] == 1 and glove["ok"] is True
        assert glove["gesture_event_id"].startswith("ultra96-relay:")
        state = next(p for t, p in seen if t == ge.STATE_TOPIC and p["tick"] > 0)
        assert state["match_id"] == tap["match_id"] == glove["match_id"]
        match = next(p for t, p in seen if t == ge.MATCH_TOPIC)
        assert match["rules_version"] == RULES["version"]

    on_selector(scenario())


def test_a_phone_that_connects_later_is_told_the_match(broker):
    async def scenario():
        async with engine_running(broker.port):
            env = await until_retained(broker.port, lambda m: m["phase"] == "waiting")
        assert env["source"] == ge.CLIENT_ID and env["payload"]["match_id"] == "e1-1"

    on_selector(scenario())


def test_a_clean_stop_leaves_the_engine_offline(broker):
    """aiomqtt always disconnects cleanly, which suppresses the will, so this is explicit."""

    async def scenario():
        async with engine_running(broker.port):
            await until_retained(broker.port, lambda m: m["phase"] == "waiting")
        env = await retained_match(broker.port)
        assert env["payload"]["phase"] == "offline"
        assert (env["payload"]["match_id"], env["payload"]["reason"]) == ("", ge.STOPPED)

    on_selector(scenario())


def test_a_fatal_error_marks_the_engine_offline_and_ends_run(broker):
    engine = new_engine()

    def fail(now):
        raise ge.EngineFatal("the next match can't open")

    async def scenario():
        async with engine_running(broker.port, engine) as (_, task):
            await until_retained(broker.port, lambda m: m["phase"] == "waiting")
            engine.session.on_tick = fail
            with pytest.raises(ge.EngineFatal):
                await asyncio.wait_for(task, timeout=BROKER_DEADLINE_S)
        env = await retained_match(broker.port)
        assert (env["payload"]["phase"], env["payload"]["reason"]) == ("offline", ge.ENGINE_ERROR)

    on_selector(scenario())


@pytest.mark.parametrize("gloves", [True, False])
def test_after_a_broker_restart_the_match_is_sent_again_and_the_gloves_forgotten(
    broker, monkeypatch, gloves
):
    """Without gloves nothing in game/match changes, so only sending it first brings it back."""
    # nothing is sent when the broker stops, since a drop mid-send can crash the loop on Windows
    monkeypatch.setattr(ge, "TICK_S", 3600.0)
    monkeypatch.setattr(ge, "MIN_BACKOFF_S", 0.2)

    engine = new_engine()

    async def scenario():
        if gloves:
            await publish(broker.port, envelope.GLOVES_TOPIC, GLOVES_READY, retain=True)
        async with engine_running(broker.port, engine):
            before = await until_retained(broker.port, lambda m: m["glove1"] is gloves)
            # a bad lane comes back as a count only once the engine is subscribed and reading
            await publish(broker.port, "viz/lane/1", {"not": "a lane"})
            await wait_until(lambda: engine.session.counts["bad_lane"] == 1, BROKER_DEADLINE_S)
            broker.stop()  # its retained messages go with it, the gloves' included
            broker.start()
            after = await until_retained(broker.port, lambda m: m["phase"] == "waiting")
        assert after["event_id"] != before["event_id"]
        assert after["payload"]["match_id"] == before["payload"]["match_id"]
        assert (after["payload"]["glove1"], after["payload"]["glove2"]) == (False, False)

    on_selector(scenario())


# --- main(), as run_engine.sh starts it ------------------------------------------------------


def engine_process(tmp_path, port, rules=RULES):
    """Start game_engine.py with the stub core. Its logs go under tmp_path, not the real home."""
    core = tmp_path / "core"
    core.mkdir()
    (core / "match.py").write_text((Path(stub.__file__)).read_text())
    (core / "rules.json").write_text(json.dumps(rules))
    env = dict(os.environ, USERPROFILE=str(tmp_path), HOME=str(tmp_path))
    return subprocess.Popen(
        [sys.executable, str(ENGINE_PY), "--port", str(port), "--core-dir", str(core)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def test_a_killed_engine_leaves_it_offline_through_its_will(broker, tmp_path):
    """run_board.py stops the engine with pkill, which gives it no chance to say anything."""
    engine = engine_process(tmp_path, broker.port)
    try:
        on_selector(until_retained(broker.port, lambda m: m["phase"] == "waiting"))
    finally:
        engine.kill()
        engine.wait()
    env = on_selector(until_retained(broker.port, lambda m: m["phase"] == "offline"))
    assert (env["payload"]["match_id"], env["source"]) == ("", ge.CLIENT_ID)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.parametrize(
    "rules, why",
    [
        (dict(RULES, nan=True), "can't open"),  # the stub's opening state is NaN
        (dict(RULES, TowerController={"TowerRadius": None}), "TowerController.TowerRadius"),
    ],
)
def test_a_core_that_fails_on_every_start_exits_3_before_connecting(tmp_path, rules, why):
    """Nothing listens on the port, so an engine that tried to connect would retry forever."""
    engine = engine_process(tmp_path, free_port(), rules)
    output, _ = engine.communicate(timeout=20)
    assert engine.returncode == ge.EXIT_FATAL, output
    assert why in output and "connected" not in output
    assert list(tmp_path.glob("comms/logs/*/game_engine_*.log"))
