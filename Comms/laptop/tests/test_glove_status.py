"""How the board judges each glove ready and publishes it on status/gloves (configuration B).

The retained message, the will and the resend after a reconnect are tested against a real
Mosquitto, since a fake broker would only fake the behaviour being tested.
"""

import asyncio
import contextlib
import json
import logging
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import aiomqtt
import pytest

import mqtt_client
from framing import send_message
from helpers import BROKER_DEADLINE_S, DEADLINE_S, on_selector, serving, wait_until

import envelope as board_envelope  # noqa: E402 - ultra96/, on sys.path via conftest
import gesture  # noqa: E402
import mqtt_publisher  # noqa: E402
import tcp_client  # noqa: E402
import tcp_client_mqtt  # noqa: E402

TOOLS = Path(__file__).resolve().parents[2] / "scripts" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import latency_report  # noqa: E402

ULTRA96 = Path(__file__).resolve().parents[2] / "ultra96"
READY_DOWN = {"player1": "ready", "player2": "down"}
BOTH_OFFLINE = {"player1": "offline", "player2": "offline"}


def sensor(node, status=gesture.IMU_OK):
    imu = dict.fromkeys(gesture.CHANNELS, 0)
    return {"type": "SENSOR", "node": node, "seq": 0, "imu": imu, "reserved": [status, 0]}


def fill(gloves, node, count, now, status=gesture.IMU_OK):
    for _ in range(count):
        gloves.add(sensor(node, status), now)


# --------------------------------------------------------------------------
# Judging a glove ready
# --------------------------------------------------------------------------


def test_a_glove_is_ready_from_50_packets_in_the_last_second():
    gloves = tcp_client_mqtt.GloveStatus()

    fill(gloves, 1, tcp_client_mqtt.READY_MIN_PACKETS - 1, now=10.0)
    assert gloves.state(10.0)["player1"] == "down"

    fill(gloves, 1, 1, now=10.0)
    assert gloves.state(10.0) == READY_DOWN


def test_a_glove_that_goes_silent_is_down_a_second_later():
    gloves = tcp_client_mqtt.GloveStatus()
    fill(gloves, 1, 100, now=10.0)

    assert gloves.state(10.0 + tcp_client_mqtt.READY_WINDOW_S)["player1"] == "ready"
    assert gloves.state(10.0 + tcp_client_mqtt.READY_WINDOW_S + 0.01)["player1"] == "down"


def test_a_ready_glove_stays_ready_until_it_falls_below_25():
    gloves = tcp_client_mqtt.GloveStatus()
    fill(gloves, 1, 70, now=9.5)
    fill(gloves, 1, 30, now=10.0)
    assert gloves.state(10.0)["player1"] == "ready"

    # the 70 at 9.5 age out, which leaves 30, between the thresholds
    assert gloves.state(10.6)["player1"] == "ready"

    # the 30 at 10.0 age out too, which leaves 24, below DOWN_BELOW_PACKETS
    fill(gloves, 1, tcp_client_mqtt.DOWN_BELOW_PACKETS - 1, now=11.5)
    assert gloves.state(11.5)["player1"] == "down"

    # once down, 30 is not enough to come back
    fill(gloves, 1, 30 - (tcp_client_mqtt.DOWN_BELOW_PACKETS - 1), now=11.5)
    assert gloves.state(11.5)["player1"] == "down"


def test_a_glove_streaming_near_50_hz_changes_state_once():
    """With one threshold, a glove wobbling between 47 and 52 Hz flipped at most checks."""
    arrivals = []
    for second in range(10):
        rate = 52 if second % 2 == 0 else 47
        arrivals += [second + i / rate for i in range(rate)]

    gloves = tcp_client_mqtt.GloveStatus()
    counts, states = [], []
    pending = iter(arrivals)
    upcoming = next(pending)
    for step in range(4, 41):  # a check every GLOVE_CHECK_S, from 1 s to 10 s
        now = step * tcp_client_mqtt.GLOVE_CHECK_S
        while upcoming is not None and upcoming <= now:
            gloves.add(sensor(2), upcoming)
            upcoming = next(pending, None)
        counts.append(sum(1 for t in arrivals if now - tcp_client_mqtt.READY_WINDOW_S <= t <= now))
        states.append(gloves.state(now)["player2"])

    # the stream must cross 50 both ways, or this test proves nothing
    assert min(counts) < tcp_client_mqtt.READY_MIN_PACKETS <= max(counts)
    changes = sum(1 for before, after in zip(states, states[1:]) if before != after)
    assert states[-1] == "ready"
    assert changes <= 1, states


@pytest.mark.parametrize("status", [0, 2, 3, None], ids=["dummy", "offline", "recovering", "none"])
def test_only_packets_with_an_imu_reading_count(status):
    """Status 2 or 3 means a glove streaming zeros: connected, but it can't make a gesture."""
    gloves = tcp_client_mqtt.GloveStatus()
    msg = sensor(2, status)
    if status is None:
        del msg["reserved"]

    for _ in range(100):
        gloves.add(msg, 10.0)

    assert gloves.state(10.0)["player2"] == "down"


@pytest.mark.parametrize("node", [3, 0, True, "1", [1], None])
def test_packets_from_an_unknown_or_malformed_node_are_ignored(node):
    """True would otherwise count as player 1, and a list would crash the receiver."""
    gloves = tcp_client_mqtt.GloveStatus()

    fill(gloves, node, 100, now=10.0)

    assert gloves.state(10.0) == {"player1": "down", "player2": "down"}


# --------------------------------------------------------------------------
# The feed from receiver
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_board_counts_each_sensor_packet_it_receives(monkeypatch):
    """Over a real TCP session, which also checks `receiver` passes the monotonic clock."""
    monkeypatch.setattr(tcp_client_mqtt, "READY_WINDOW_S", 0.2)
    gloves = tcp_client_mqtt.GloveStatus()

    async def laptop(_reader, writer):
        await send_message(writer, {"type": "ECHO", "node": 1, "seq": 0, "fields": [-1]})
        for _ in range(tcp_client_mqtt.READY_MIN_PACKETS):
            await send_message(writer, sensor(2))
        await asyncio.sleep(DEADLINE_S)

    events = tcp_client_mqtt.MqttEventSource(asyncio.Queue(), mqtt_publisher.PublishStats())
    async with serving(laptop) as port:
        session = asyncio.create_task(
            tcp_client_mqtt.one_session("127.0.0.1", port, events, gloves=gloves)
        )
        try:
            await wait_until(lambda: gloves.state(time.monotonic())["player2"] == "ready")
            assert gloves.state(time.monotonic())["player1"] == "down", "an ECHO is not a SENSOR packet"
            await wait_until(lambda: gloves.state(time.monotonic())["player2"] == "down")
        finally:
            session.cancel()
            await asyncio.gather(session, return_exceptions=True)


@pytest.mark.asyncio
async def test_configuration_a_passes_no_gloves(monkeypatch):
    """A has no MQTT client on the board, so it must not count anything."""
    calls = []

    async def receiver(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(tcp_client, "receiver", receiver)

    async def laptop(_reader, _writer):
        await asyncio.sleep(DEADLINE_S)

    async with serving(laptop) as port:
        await asyncio.wait_for(tcp_client.one_session("127.0.0.1", port), timeout=DEADLINE_S)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert len(args) == 5 and "gloves" not in kwargs


@pytest.mark.asyncio
async def test_one_count_lasts_across_tcp_reconnects(monkeypatch):
    """A new count per session would forget a glove that is still streaming."""
    seen = []

    async def failing_session(*_args, gloves=None, **_kwargs):
        seen.append(gloves)
        raise OSError("refused")

    monkeypatch.setattr(tcp_client_mqtt, "one_session", failing_session)
    monkeypatch.setattr(tcp_client_mqtt, "RECONNECT_BACKOFF_S", 0.01)
    gloves = tcp_client_mqtt.GloveStatus()
    task = asyncio.create_task(
        tcp_client_mqtt.run_tcp(
            "127.0.0.1", 1, asyncio.Queue(), mqtt_publisher.PublishStats(), gloves=gloves
        )
    )
    try:
        await wait_until(lambda: len(seen) >= 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert all(g is gloves for g in seen)


@pytest.mark.asyncio
async def test_the_watcher_hands_over_each_change_once(monkeypatch, caplog):
    states = iter([READY_DOWN, READY_DOWN, BOTH_OFFLINE])

    class ScriptedGloves:
        def state(self, _now):
            return next(states, BOTH_OFFLINE)

    class RecordingSlot(mqtt_publisher.GloveSlot):
        def __init__(self):
            super().__init__()
            self.handed = []

        def set(self, state):
            self.handed.append(state)
            super().set(state)

    slot = RecordingSlot()
    monkeypatch.setattr(tcp_client_mqtt, "GLOVE_CHECK_S", 0.001)

    with caplog.at_level(logging.INFO, logger="tcp_client_mqtt"):
        task = asyncio.create_task(tcp_client_mqtt.watch_gloves(ScriptedGloves(), slot))
        try:
            await wait_until(lambda: len(slot.handed) >= 2)
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert slot.handed == [READY_DOWN, BOTH_OFFLINE]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("gloves:")]
    assert lines == ["gloves: player1=ready player2=down", "gloves: player1=offline player2=offline"]


# --------------------------------------------------------------------------
# The envelope
# --------------------------------------------------------------------------


def test_glove_envelopes_pass_the_laptop_validator_and_never_reuse_an_event_id():
    slot = mqtt_publisher.GloveSlot()
    statuses = [slot.envelope(READY_DOWN), json.loads(slot.offline_payload())]
    event = board_envelope.build_envelope({"gesture": "warrior"}, 0)

    for env in statuses:
        parsed = mqtt_client.parse_envelope(json.dumps(env).encode("utf-8"))
        assert parsed["source"] == board_envelope.GLOVES_SOURCE

    assert statuses[1]["payload"] == BOTH_OFFLINE
    ids = [env["event_id"] for env in [*statuses, event]]
    assert len(set(ids)) == 3, ids


def test_the_phone_parser_reads_the_topic_and_states_the_board_sends():
    """The C# parser has its own copy of the contract, since the phone can't import Python."""
    parser = (ULTRA96.parent / "phone" / "comms-visualiser" / "Hook" / "GloveStatusParser.cs").read_text()

    assert f'public const string Topic = "{board_envelope.GLOVES_TOPIC}";' in parser
    states = (board_envelope.READY, board_envelope.DOWN, board_envelope.OFFLINE)
    assert "States = { " + ", ".join(f'"{s}"' for s in states) + " };" in parser
    assert f"private const int Players = {len(board_envelope.PLAYERS)};" in parser


def test_the_phone_parser_reads_the_payload_the_app_hands_it():
    """The app's MqttConnection removes the envelope itself, so the parser must not look for one.

    If the app's half fails, its Dispatch changed, so read it again before changing the parser.
    """
    app = ULTRA96.parents[1] / "SW_Visualiser" / "script" / "Networking" / "MqttConnection.cs"
    dispatch = app.read_text()
    assert 'string payload = envelope["payload"].ToString(' in dispatch
    assert "entry.Value.Handler(topic, payload)" in dispatch

    parser = (ULTRA96.parent / "phone" / "comms-visualiser" / "Hook" / "GloveStatusParser.cs").read_text()
    assert "Envelope.Parse" not in parser
    assert "JToken.Parse(" in parser


# --------------------------------------------------------------------------
# Publishing, against a fake client
# --------------------------------------------------------------------------


class HoldingClient:
    """Records publishes. A held kind (retained or not) waits until `release` is set."""

    def __init__(self, hold_retained=False, hold_events=False):
        self.events, self.statuses = [], []
        self.hold_retained, self.hold_events = hold_retained, hold_events
        self.release = asyncio.Event()

    async def publish(self, topic, payload, qos, retain=False, timeout=None):
        if (self.hold_retained if retain else self.hold_events):
            await self.release.wait()
        (self.statuses if retain else self.events).append((topic, json.loads(payload)))

    @property
    async def messages(self):
        await asyncio.Event().wait()
        yield


@contextlib.asynccontextmanager
async def publishing(client, monkeypatch, outbound, slot, stats=None):
    """Run the real one_session on `client` for the block, then stop it cleanly."""

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return client

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(mqtt_publisher.aiomqtt, "Client", Session)
    stop = asyncio.Event()
    session = asyncio.create_task(
        mqtt_publisher.one_session(
            "127.0.0.1", 1883, outbound, [], stats or mqtt_publisher.PublishStats(), stop, slot
        )
    )
    try:
        yield
    finally:
        stop.set()
        client.release.set()
        await asyncio.wait_for(session, timeout=DEADLINE_S)


@pytest.mark.asyncio
async def test_the_status_never_waits_behind_an_unacknowledged_event(monkeypatch):
    client = HoldingClient(hold_events=True)
    outbound, slot = asyncio.Queue(), mqtt_publisher.GloveSlot()
    mqtt_publisher.enqueue_event(outbound, "game/gesture/1", {"seq": 0}, mqtt_publisher.PublishStats())

    async with publishing(client, monkeypatch, outbound, slot):
        await asyncio.sleep(0.05)  # the event is now stuck waiting for its PUBACK
        slot.set(READY_DOWN)
        await wait_until(lambda: client.statuses)
        assert client.events == [], "the event is still waiting"

    topic, env = client.statuses[0]
    assert (topic, env["payload"]) == ("status/gloves", READY_DOWN)


@pytest.mark.asyncio
async def test_an_event_never_waits_behind_an_unacknowledged_status(monkeypatch):
    client = HoldingClient(hold_retained=True)
    outbound, slot = asyncio.Queue(), mqtt_publisher.GloveSlot()
    slot.set(READY_DOWN)

    async with publishing(client, monkeypatch, outbound, slot):
        await asyncio.sleep(0.05)  # the status is now stuck waiting for its PUBACK
        mqtt_publisher.enqueue_event(outbound, "game/gesture/2", {"seq": 0}, mqtt_publisher.PublishStats())
        await wait_until(lambda: client.events)

    assert client.events == [("game/gesture/2", {"seq": 0})]


@pytest.mark.asyncio
async def test_only_the_newest_state_is_sent_after_one_in_flight(monkeypatch):
    client = HoldingClient(hold_retained=True)
    slot = mqtt_publisher.GloveSlot()
    slot.set({"player1": "down", "player2": "down"})

    async with publishing(client, monkeypatch, asyncio.Queue(), slot):
        await asyncio.sleep(0.05)  # the first state is in flight
        slot.set(READY_DOWN)
        slot.set({"player1": "ready", "player2": "ready"})
        client.release.set()
        await wait_until(lambda: len(client.statuses) >= 2)
        await asyncio.sleep(0.05)

    sent = [env["payload"] for _, env in client.statuses]
    # the in-flight one, the newest, then offline from the clean stop
    assert sent == [
        {"player1": "down", "player2": "down"},
        {"player1": "ready", "player2": "ready"},
        BOTH_OFFLINE,
    ]


@pytest.mark.asyncio
async def test_status_publishes_stay_out_of_the_latency_measurement(monkeypatch, caplog):
    """The latency report reads every "published" line, and published= counts only AI events."""
    client, slot, stats = HoldingClient(), mqtt_publisher.GloveSlot(), mqtt_publisher.PublishStats()
    slot.set(READY_DOWN)

    with caplog.at_level(logging.DEBUG, logger="mqtt_publisher"):
        async with publishing(client, monkeypatch, asyncio.Queue(), slot, stats):
            await wait_until(lambda: client.statuses)

    assert len(client.statuses) == 2  # the state, then offline
    assert stats.published == 0
    assert not [r for r in caplog.records if latency_report.PUBLISHED.search(r.getMessage())]


# --------------------------------------------------------------------------
# Ctrl-C
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_ctrl_c_force_quits():
    stop = asyncio.Event()
    original = signal.getsignal(signal.SIGINT)
    restore = tcp_client_mqtt.stop_on_interrupt(stop)
    try:
        signal.raise_signal(signal.SIGINT)
        await wait_until(stop.is_set)
        assert signal.getsignal(signal.SIGINT) is original
    finally:
        restore()


@pytest.mark.asyncio
async def test_the_ctrl_c_handler_is_restored_when_the_relay_ends(monkeypatch):
    """Left installed, a later Ctrl-C would call into a closed loop."""

    async def idle(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(tcp_client_mqtt, "run_tcp", idle)
    monkeypatch.setattr(mqtt_publisher, "run_publisher", idle)
    original = signal.getsignal(signal.SIGINT)
    relay = asyncio.create_task(tcp_client_mqtt.run("127.0.0.1", 1, "127.0.0.1", 1883))
    await wait_until(lambda: signal.getsignal(signal.SIGINT) is not original)

    relay.cancel()
    await asyncio.gather(relay, return_exceptions=True)

    assert signal.getsignal(signal.SIGINT) is original


# --------------------------------------------------------------------------
# Against a real broker
# --------------------------------------------------------------------------

async def retained_status(port):
    """Return the retained payload a phone connecting now is told, or None if there is none.

    A message published after the phone subscribed arrives live, not retained, so it is None.
    """
    async with aiomqtt.Client("127.0.0.1", port, identifier=f"test-{uuid.uuid4().hex[:6]}") as phone:
        await phone.subscribe(board_envelope.GLOVES_TOPIC, qos=1)
        try:
            message = await asyncio.wait_for(anext(phone.messages), timeout=0.5)
        except asyncio.TimeoutError:
            return None
    return json.loads(message.payload)["payload"] if message.retain else None


async def until_retained(port, expected):
    deadline = time.monotonic() + BROKER_DEADLINE_S
    while (found := await retained_status(port)) != expected:
        assert time.monotonic() < deadline, f"retained {found}, expected {expected}"


@contextlib.asynccontextmanager
async def board_publisher(port, state=READY_DOWN):
    """The real publisher, in this process, with the gloves' state already set."""
    stop, slot = asyncio.Event(), mqtt_publisher.GloveSlot()
    slot.set(state)
    task = asyncio.create_task(
        mqtt_publisher.run_publisher(
            "127.0.0.1", port, asyncio.Queue(), stop, mqtt_publisher.PublishStats(), slot
        )
    )
    try:
        yield stop
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=BROKER_DEADLINE_S)


def test_a_phone_that_connects_later_is_told_the_current_state(broker):
    async def scenario():
        async with board_publisher(broker.port):
            await until_retained(broker.port, READY_DOWN)

    on_selector(scenario())


def test_a_clean_stop_marks_both_gloves_offline(broker):
    """aiomqtt always disconnects cleanly, which suppresses the will, so this is explicit."""

    async def scenario():
        async with board_publisher(broker.port):
            await until_retained(broker.port, READY_DOWN)
        assert await retained_status(broker.port) == BOTH_OFFLINE

    on_selector(scenario())


def test_a_killed_client_leaves_both_gloves_offline(broker):
    """The board client is stopped with pkill, which gives it no chance to say anything."""
    script = (
        "import asyncio, sys\n"
        f"sys.path.insert(0, {str(ULTRA96)!r})\n"
        "import mqtt_publisher\n"
        "slot = mqtt_publisher.GloveSlot()\n"
        f"slot.set({READY_DOWN!r})\n"
        "loop = asyncio.SelectorEventLoop()\n"
        "loop.run_until_complete(mqtt_publisher.run_publisher(\n"
        f"    '127.0.0.1', {broker.port}, asyncio.Queue(), asyncio.Event(),\n"
        "    mqtt_publisher.PublishStats(), slot))\n"
    )
    board = subprocess.Popen([sys.executable, "-c", script])
    try:
        on_selector(until_retained(broker.port, READY_DOWN))
    finally:
        board.kill()
        board.wait()

    on_selector(until_retained(broker.port, BOTH_OFFLINE))


def test_the_state_is_sent_again_after_the_broker_restarts(broker, monkeypatch):
    """With nothing to publish, the client must still notice the drop and reconnect."""
    monkeypatch.setattr(mqtt_publisher, "MIN_BACKOFF_S", 0.2)

    async def scenario():
        async with board_publisher(broker.port):
            await until_retained(broker.port, READY_DOWN)
            broker.stop()  # its retained messages go with it
            broker.start()
            await until_retained(broker.port, READY_DOWN)

    on_selector(scenario())


def mqtt_packet_size(data):
    """Return the size of the MQTT packet at the start of `data`, or None if not yet known."""
    remaining, shift = 0, 0
    for header_size, byte in enumerate(data[1:5], start=2):
        remaining += (byte & 0x7F) << shift
        if not byte & 0x80:
            return header_size + remaining
        shift += 7
    return None


class Middleman:
    """Pass bytes between a client and the broker.

    It can swallow what the client sends, or cut the link.
    """

    def __init__(self, broker_port):
        self.broker_port = broker_port
        self.swallow = False
        self.held = b""
        self.swallowed = asyncio.Event()  # set once a whole packet has been swallowed
        self.writers = []

    async def handle(self, client_reader, client_writer):
        broker_reader, broker_writer = await asyncio.open_connection("127.0.0.1", self.broker_port)
        self.writers += [client_writer, broker_writer]
        await asyncio.gather(
            self._pipe(client_reader, broker_writer, from_client=True),
            self._pipe(broker_reader, client_writer, from_client=False),
            return_exceptions=True,
        )

    async def _pipe(self, reader, writer, from_client):
        try:
            while data := await reader.read(4096):
                if from_client and self.swallow:
                    self.held += data
                    size = mqtt_packet_size(self.held)
                    if size is not None and len(self.held) >= size:
                        self.swallowed.set()
                    continue
                writer.write(data)
        finally:
            writer.close()  # even on a reset, so the other direction sees the end and stops too

    def cut(self):
        for writer in self.writers:
            writer.close()
        self.writers.clear()


def test_a_dropped_link_with_an_event_waiting_for_its_ack_reconnects_at_once(broker, monkeypatch):
    """After a drop, aiomqtt keeps a publish waiting 10 s for its ack, so the session cancels it."""
    monkeypatch.setattr(mqtt_publisher, "MIN_BACKOFF_S", 0.2)
    topic = board_envelope.gesture_topic(1)
    env = board_envelope.build_envelope({}, 0)
    outbound, stop, stats = asyncio.Queue(), asyncio.Event(), mqtt_publisher.PublishStats()
    middleman = Middleman(broker.port)

    async def scenario():
        async with aiomqtt.Client("127.0.0.1", broker.port, identifier="test-phone") as phone:
            await phone.subscribe(topic, qos=1)
            async with serving(middleman.handle) as port:
                publisher = asyncio.create_task(
                    mqtt_publisher.run_publisher(
                        "127.0.0.1", port, outbound, stop, stats, mqtt_publisher.GloveSlot()
                    )
                )
                try:
                    await wait_until(lambda: stats.sessions == 1)
                    middleman.swallow = True  # so the broker never sees the event or acks it
                    mqtt_publisher.enqueue_event(outbound, topic, env, stats)
                    await asyncio.wait_for(middleman.swallowed.wait(), DEADLINE_S)
                    # the event is fully sent, so no write is in flight when the link is cut
                    middleman.swallow = False
                    middleman.cut()
                    await wait_until(lambda: stats.sessions == 2)
                    message = await asyncio.wait_for(anext(phone.messages), DEADLINE_S)
                finally:
                    stop.set()
                    stopped, _ = await asyncio.wait({publisher}, timeout=BROKER_DEADLINE_S)
                    publisher.cancel()  # in case stop didn't end it, so the test's own error shows
                    await asyncio.gather(publisher, return_exceptions=True)

        assert stopped, "the publisher didn't stop"
        publisher.result()  # re-raise anything it failed with
        assert json.loads(message.payload)["event_id"] == env["event_id"]
        assert stats.published == 1 and stats.stale == 0

    on_selector(scenario())


def test_ctrl_c_stops_the_relay_and_marks_the_gloves_offline(broker):
    """Through the real run(), since a cancelled publisher would leave the last state retained."""

    async def scenario():
        original = signal.getsignal(signal.SIGINT)
        relay = asyncio.create_task(
            tcp_client_mqtt.run("127.0.0.1", 1, "127.0.0.1", broker.port)
        )
        await until_retained(broker.port, {"player1": "down", "player2": "down"})
        signal.raise_signal(signal.SIGINT)
        await asyncio.wait_for(relay, timeout=BROKER_DEADLINE_S)

        assert signal.getsignal(signal.SIGINT) is original
        assert await retained_status(broker.port) == BOTH_OFFLINE

    on_selector(scenario())
