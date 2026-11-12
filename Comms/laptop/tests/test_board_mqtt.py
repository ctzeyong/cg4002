"""Configuration B: the Ultra96 as an MQTT publisher.

Under configuration A the laptop publishes game events; under B the board does,
straight into a broker on its own loopback. That moves the envelope contract's
producer onto a second machine with its own copy of the code, so the first job
here is making sure the two producers agree.

`ultra96/envelope.py` is a copy of the laptop's envelope contract for the same
reason `ultra96/framing.py` is a copy of `laptop/framing.py` -- separate machines,
separate venvs, no shared import. The two `framing.py` copies are kept honest by
the session tests rather than by being byte-identical, and the same applies here:
these assert the board's envelopes are **accepted by the laptop's own validator**,
which is the property that actually matters.

The rest covers the publish leg's two paid-for rules -- drop what cannot succeed
on retry, hold what can -- and the event source that feeds it.
"""

import asyncio
import contextlib
import json
import logging
import time

import aiomqtt
import pytest

import mqtt_client
import mqtt_config
from framing import send_message
from helpers import DEADLINE_S, serving

# conftest puts comms/ultra96 on sys.path.
import envelope as board_envelope  # noqa: E402
import mqtt_publisher  # noqa: E402
import tcp_client_mqtt  # noqa: E402


# --------------------------------------------------------------------------
# The contract: two producers, one envelope
# --------------------------------------------------------------------------


def test_board_envelope_is_accepted_by_the_laptop_validator():
    """The property that matters: the phone's relay would not drop it.

    parse_envelope validates by *type* and silently drops what fails, so a board
    envelope with a field of the wrong JSON type would vanish with only a log line
    at the far end -- the exact failure the Unity contract check exists for too.
    """
    env = board_envelope.build_envelope({"gesture": "circle"}, 7)
    raw = json.dumps(env).encode("utf-8")

    parsed = mqtt_client.parse_envelope(raw)

    assert parsed["seq"] == 7
    assert parsed["payload"] == {"gesture": "circle"}


def test_board_and_laptop_envelopes_have_the_same_shape():
    """Same fields, same types. Only `source` may differ."""
    board = board_envelope.build_envelope({"x": 1}, 3)
    laptop = mqtt_client.build_envelope({"x": 1}, 3)

    assert sorted(board) == sorted(laptop)
    for field in board:
        assert type(board[field]) is type(laptop[field]), field


def test_board_source_is_distinct_from_the_laptop():
    """event_id must be unique per *publisher*, and the publisher has moved.

    If both ends used "laptop-relay", a board restart could mint an event_id a
    phone had already seen from the laptop and the phone would discard a real
    event as a duplicate.
    """
    board = board_envelope.build_envelope({"x": 1}, 0)
    laptop = mqtt_client.build_envelope({"x": 1}, 0)

    assert board["source"] != laptop["source"]
    assert board["event_id"] != laptop["event_id"]
    assert board["event_id"].startswith(board["source"] + ":")


def test_board_topics_match_the_laptop():
    """`notes/mqtt_topics.md` is the single source; two producers must not diverge."""
    for player in (0, 1, 2):
        assert board_envelope.gesture_topic(player) == mqtt_config.gesture_topic(player)


def test_board_broker_port_matches_the_contract():
    assert board_envelope.PLAINTEXT_PORT == mqtt_config.PLAINTEXT_PORT


# --------------------------------------------------------------------------
# The publish leg
# --------------------------------------------------------------------------


class FakeClient:
    """Records publishes, retained ones (the gloves' status) kept apart. Can fail the first few."""

    def __init__(self, fail_times=0):
        self.published = []
        self.retained = []
        self.fail_times = fail_times

    async def publish(self, topic, payload, qos, retain=False, timeout=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise aiomqtt.MqttError("link down")
        (self.retained if retain else self.published).append((topic, payload, qos))

    @property
    async def messages(self):
        """Never yield and never end, like a live link with nothing subscribed."""
        await asyncio.Event().wait()
        yield


def test_enqueue_drops_the_oldest_when_full():
    """Drop-oldest, matching the laptop's outbound policy.

    A reconnecting visualiser wants current state, not a backlog. The BLE downlink
    does the opposite on purpose; don't make them match.
    """
    outbound = asyncio.Queue(maxsize=2)
    stats = mqtt_publisher.PublishStats()

    for seq in range(3):
        mqtt_publisher.enqueue_event(outbound, "game/gesture/1", {"seq": seq}, stats)

    assert stats.dropped == 1
    assert [outbound.get_nowait()[1]["seq"] for _ in range(2)] == [1, 2]


@pytest.mark.asyncio
async def test_unserialisable_event_is_dropped_not_retried():
    """The poison-pill rule: retry only what can succeed on retry.

    A json.dumps failure is deterministic. Left in `pending` it blocks the queue
    forever, and every reconnect re-fails on the same message.
    """
    outbound = asyncio.Queue()
    stats = mqtt_publisher.PublishStats()
    stop = asyncio.Event()
    client = FakeClient()

    mqtt_publisher.enqueue_event(
        outbound, "game/gesture/1", {"payload": object()}, stats  # not serialisable
    )
    mqtt_publisher.enqueue_event(outbound, "game/gesture/1", {"payload": {"ok": True}}, stats)

    async def stop_when_drained():
        while outbound.qsize() or stats.published == 0:
            await asyncio.sleep(0)
        stop.set()

    pending = []
    await asyncio.wait_for(
        asyncio.gather(
            mqtt_publisher._publish_loop(client, outbound, pending, stats, stop),
            stop_when_drained(),
        ),
        timeout=DEADLINE_S,
    )

    assert stats.undeliverable == 1
    assert stats.published == 1, "the good event must still get through"
    assert pending == [], "the poison pill must not be left blocking the queue"


@pytest.mark.asyncio
async def test_failed_publish_stays_in_pending():
    """The opposite call: an MqttError *can* succeed on retry, so keep the payload.

    Held in `pending` rather than requeued, because requeuing puts it behind
    everything queued since and reorders the stream. See shutdown_and_ordering.md.
    """
    outbound = asyncio.Queue()
    stats = mqtt_publisher.PublishStats()
    stop = asyncio.Event()
    client = FakeClient(fail_times=1)

    mqtt_publisher.enqueue_event(outbound, "game/gesture/2", {"payload": {"n": 1}}, stats)
    pending = []

    with pytest.raises(aiomqtt.MqttError):
        await asyncio.wait_for(
            mqtt_publisher._publish_loop(client, outbound, pending, stats, stop),
            timeout=DEADLINE_S,
        )

    assert len(pending) == 1, "the in-flight envelope must survive for the next session"
    assert stats.published == 0


# --------------------------------------------------------------------------
# The TTL: events older than 2 s are thrown away
# --------------------------------------------------------------------------


async def _publish_until(client, outbound, pending, stats, stop, done):
    """Run one publish session until `done()` holds, then stop it via `stop`."""

    async def stop_when_done():
        while not done():
            await asyncio.sleep(0)
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(
            mqtt_publisher._publish_loop(client, outbound, pending, stats, stop),
            stop_when_done(),
        ),
        timeout=DEADLINE_S,
    )


@pytest.mark.asyncio
async def test_stale_event_is_discarded_and_counted_apart_from_dropped(caplog):
    """An old event is thrown away and counted as stale; the fresh one behind it is published."""
    outbound = asyncio.Queue()
    stats = mqtt_publisher.PublishStats()
    client = FakeClient()
    old = board_envelope.build_envelope({"gesture": "arc"}, 0)
    fresh = board_envelope.build_envelope({"gesture": "circle"}, 1)

    queued_long_ago = time.monotonic() - (mqtt_publisher.EVENT_TTL_S + 1.0)
    outbound.put_nowait(("game/gesture/1", old, queued_long_ago))
    mqtt_publisher.enqueue_event(outbound, "game/gesture/1", fresh, stats)

    with caplog.at_level(logging.WARNING, logger="mqtt_publisher"):
        await _publish_until(
            client, outbound, [], stats, asyncio.Event(), lambda: stats.published == 1
        )

    assert [json.loads(p)["event_id"] for _, p, _ in client.published] == [fresh["event_id"]]
    assert stats.stale == 1
    assert stats.dropped == 0, "a stale discard is not a queue overflow"
    assert "stale=1" in stats.describe() and "dropped=0" in stats.describe()

    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1
    assert old["event_id"] in warned[0], "the discard must name the event"
    assert "s old" in warned[0], "the discard must give the event's age"


@pytest.mark.asyncio
async def test_held_event_expires_before_its_retry(monkeypatch):
    """A failed publish that goes stale during the reconnect is not retried."""
    monkeypatch.setattr(mqtt_publisher, "EVENT_TTL_S", 0.05)
    outbound = asyncio.Queue()
    stats = mqtt_publisher.PublishStats()
    pending = []
    mqtt_publisher.enqueue_event(
        outbound, "game/gesture/1", board_envelope.build_envelope({}, 0), stats
    )

    with pytest.raises(aiomqtt.MqttError):
        await asyncio.wait_for(
            mqtt_publisher._publish_loop(
                FakeClient(fail_times=1), outbound, pending, stats, asyncio.Event()
            ),
            timeout=DEADLINE_S,
        )
    assert len(pending) == 1

    await asyncio.sleep(0.1)  # longer than the shortened TTL

    client = FakeClient()
    await _publish_until(
        client, outbound, pending, stats, asyncio.Event(), lambda: stats.stale == 1
    )

    assert client.published == [], "an expired retry must not reach the broker"
    assert pending == [], "an expired envelope must not stay blocking the queue"


@pytest.mark.asyncio
async def test_fresh_event_is_still_retried_at_qos_1():
    """A failed publish that is still fresh is retried as normal, at QoS 1."""
    outbound = asyncio.Queue()
    stats = mqtt_publisher.PublishStats()
    pending = []
    env = board_envelope.build_envelope({}, 0)
    mqtt_publisher.enqueue_event(outbound, "game/gesture/1", env, stats)

    with pytest.raises(aiomqtt.MqttError):
        await asyncio.wait_for(
            mqtt_publisher._publish_loop(
                FakeClient(fail_times=1), outbound, pending, stats, asyncio.Event()
            ),
            timeout=DEADLINE_S,
        )

    client = FakeClient()
    await _publish_until(
        client, outbound, pending, stats, asyncio.Event(), lambda: stats.published == 1
    )

    [(_, payload, qos)] = client.published
    assert json.loads(payload)["event_id"] == env["event_id"]
    assert qos == 1
    assert stats.stale == 0


@pytest.mark.asyncio
async def test_one_quick_reconnect_publishes_before_the_event_expires(monkeypatch):
    """Real backoff and real TTL: after one failed publish, the retry lands well inside the TTL."""
    margin_s = 0.75  # time the retry must leave, for connecting on a slow board

    class TimedClient(FakeClient):
        async def publish(self, topic, payload, qos, retain=False, timeout=None):
            await super().publish(topic, payload, qos, retain, timeout)
            if not retain:
                self.published_at = time.monotonic()

    client = TimedClient(fail_times=1)

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return client

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(mqtt_publisher.aiomqtt, "Client", FakeSession)

    outbound = asyncio.Queue()
    stats = mqtt_publisher.PublishStats()
    stop = asyncio.Event()
    env = board_envelope.build_envelope({}, 0)
    queued_at = time.monotonic()
    mqtt_publisher.enqueue_event(outbound, "game/gesture/1", env, stats)

    async def settled():
        while not (stats.published or stats.stale):
            await asyncio.sleep(0.01)

    publisher = asyncio.create_task(
        mqtt_publisher.run_publisher(
            "127.0.0.1", 1883, outbound, stop, stats, mqtt_publisher.GloveSlot()
        )
    )
    try:
        # Not DEADLINE_S: the real 1 s backoff alone takes half of that.
        await asyncio.wait_for(settled(), timeout=10.0)
    finally:
        stop.set()
        await asyncio.wait_for(publisher, timeout=10.0)

    assert stats.reconnects == 1
    assert stats.stale == 0, "the reconnect took longer than the TTL"
    assert [json.loads(p)["event_id"] for _, p, _ in client.published] == [env["event_id"]]
    age = client.published_at - queued_at
    assert age <= mqtt_publisher.EVENT_TTL_S - margin_s, f"retried at {age:.2f}s, too close to the TTL"


# --------------------------------------------------------------------------
# The event source that replaces the TCP one
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_source_publishes_instead_of_writing():
    """MqttEventSource must be duck-compatible with EventSource and ignore `writer`.

    That compatibility is what lets tcp_client.receiver and event_ticker be reused
    verbatim; if the signature drifts, configuration B silently stops emitting.
    """
    outbound = asyncio.Queue()
    stats = mqtt_publisher.PublishStats()
    source = tcp_client_mqtt.MqttEventSource(outbound, stats)

    sentinel = object()  # a writer that would explode if touched
    event = await asyncio.wait_for(source.emit(sentinel, 2, "w"), timeout=DEADLINE_S)

    topic, env, _ = outbound.get_nowait()
    assert topic == "game/gesture/2", "attribution rides in the topic"
    assert env["payload"] == event
    assert event["gesture"] == "warrior", "the pressed key still picks the gesture"
    assert source.seq == 1 and source.sent == 1


@pytest.mark.asyncio
async def test_event_source_numbers_one_ordered_stream():
    """One counter for the ECHO path and the ticker, as on the TCP source."""
    outbound = asyncio.Queue()
    stats = mqtt_publisher.PublishStats()
    source = tcp_client_mqtt.MqttEventSource(outbound, stats)

    for _ in range(3):
        await asyncio.wait_for(source.emit(None, 1), timeout=DEADLINE_S)

    seqs = [outbound.get_nowait()[1]["seq"] for _ in range(3)]
    assert seqs == [0, 1, 2]


@pytest.mark.asyncio
async def test_unattributed_event_still_gets_a_real_topic():
    """`player=None` must not produce `game/gesture/None`.

    The ticker has no packet to attribute, and a malformed topic would be
    subscribed by nobody -- a silent loss rather than an error.
    """
    outbound = asyncio.Queue()
    stats = mqtt_publisher.PublishStats()
    source = tcp_client_mqtt.MqttEventSource(outbound, stats)

    await asyncio.wait_for(source.emit(None, None), timeout=DEADLINE_S)

    topic, _, _ = outbound.get_nowait()
    assert topic == f"game/gesture/{tcp_client_mqtt.TICKER_PLAYER}"
    assert "None" not in topic


@pytest.mark.asyncio
async def test_event_ids_stay_unique_across_tcp_reconnects(monkeypatch):
    """A reconnect must not reissue an event_id, or the phone drops it as a duplicate."""
    monkeypatch.setattr(tcp_client_mqtt, "RECONNECT_BACKOFF_S", 0.01)

    async def one_echo_then_hang_up(_reader, writer):
        await send_message(writer, {"type": "ECHO", "node": 1, "seq": 0, "fields": [-99]})
        writer.close()

    outbound = asyncio.Queue()
    async with serving(one_echo_then_hang_up) as port:
        relay = asyncio.create_task(
            tcp_client_mqtt.run_tcp("127.0.0.1", port, outbound, mqtt_publisher.PublishStats())
        )
        try:
            # One event per TCP session, so two events span a reconnect.
            envelopes = [(await asyncio.wait_for(outbound.get(), DEADLINE_S))[1] for _ in range(2)]
        finally:
            relay.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await relay

    ids = [env["event_id"] for env in envelopes]
    assert len(set(ids)) == 2, ids


class FrameWriter:
    """Fake StreamWriter that keeps every frame written to it."""

    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def frames(self):
        out, rest = [], self.data
        while rest:
            length = int.from_bytes(rest[:4], "big")
            out.append(json.loads(rest[4 : 4 + length]))
            rest = rest[4 + length :]
        return out


@pytest.mark.asyncio
async def test_a_classified_gesture_also_sends_a_vibrate_for_that_player():
    import gesture

    source = tcp_client_mqtt.MqttEventSource(asyncio.Queue(), mqtt_publisher.PublishStats())
    writer = FrameWriter()

    await asyncio.wait_for(
        source.emit(writer, 2, None, gesture.Result("circle", 0.9, 1.0, 0)), timeout=DEADLINE_S
    )

    assert writer.frames() == [{"type": "VIBRATE", "node": 2, "pattern": 1}]


@pytest.mark.asyncio
async def test_a_dummy_event_sends_no_vibrate():
    """Dummy events are not gestures, so the glove must not buzz."""
    source = tcp_client_mqtt.MqttEventSource(asyncio.Queue(), mqtt_publisher.PublishStats())
    writer = FrameWriter()

    await asyncio.wait_for(source.emit(writer, 1, "c"), timeout=DEADLINE_S)
    await asyncio.wait_for(source.emit(writer, None), timeout=DEADLINE_S)

    assert writer.frames() == []


def test_board_and_laptop_agree_on_the_vibrate_pattern():
    """Check the board's and the laptop's copies of the pattern match."""
    import ble_packet

    assert tcp_client_mqtt.VIBRATE_RECOGNISED == ble_packet.VIBRATE_RECOGNISED


# --------------------------------------------------------------------------
# The laptop half of configuration B
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_laptop_warns_when_the_board_is_in_the_wrong_configuration(caplog):
    """A board still running tcp_client.py sends AI events down the TCP leg.

    Under B nothing consumes them, so without this warning the symptom is a phone
    that shows nothing while every log on both machines looks healthy.
    """
    import pipeline_board_broker

    downlink = asyncio.Queue()
    stop = asyncio.Event()
    downlink.put_nowait({"type": "AI_EVENT", "player": 1})

    async def stop_when_drained():
        while downlink.qsize():
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        stop.set()

    with caplog.at_level(logging.WARNING, logger="pipeline_board_broker"):
        await asyncio.wait_for(
            asyncio.gather(
                pipeline_board_broker.drain_downlink(downlink, {}, stop),
                stop_when_drained(),
            ),
            timeout=DEADLINE_S,
        )

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "tcp_client_mqtt" in messages


@pytest.mark.asyncio
async def test_a_held_event_loop_is_reported_with_how_late_it_was(caplog):
    """A callback that blocks the loop for 300 ms gives a warning with how late it was."""
    import pipeline_board_broker

    stop = asyncio.Event()
    with caplog.at_level(logging.WARNING, logger="pipeline_board_broker"):
        watch = asyncio.create_task(pipeline_board_broker.watch_loop_lag(stop))
        await asyncio.sleep(0.05)
        time.sleep(0.3)  # holds the loop, as a stuck callback would
        await asyncio.sleep(0.25)
        stop.set()
        await asyncio.wait_for(watch, timeout=DEADLINE_S)

    warnings = [r.getMessage() for r in caplog.records if "slow event loop" in r.getMessage()]
    assert warnings
    worst_ms = int(warnings[0].rsplit("worst ", 1)[1].split(" ms")[0])
    assert worst_ms >= 150


@pytest.mark.asyncio
async def test_the_loop_watch_runs_until_stop_and_then_returns_by_itself():
    """The relay stops when any task ends, so this one must only end on stop."""
    import pipeline_board_broker

    stop = asyncio.Event()
    watch = asyncio.create_task(pipeline_board_broker.watch_loop_lag(stop))
    await asyncio.sleep(0.35)
    assert not watch.done()
    stop.set()
    await asyncio.wait_for(watch, timeout=1.0)


async def drain(downlink, outboxes):
    """Run drain_downlink until it has taken every message off `downlink`."""
    import pipeline_board_broker

    stop = asyncio.Event()

    async def stop_when_drained():
        while downlink.qsize():
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(
            pipeline_board_broker.drain_downlink(downlink, outboxes, stop),
            stop_when_drained(),
        ),
        timeout=DEADLINE_S,
    )


@pytest.mark.asyncio
async def test_laptop_sends_a_vibrate_to_that_players_glove_only(caplog):
    import ble_client
    import ble_packet

    outboxes = ble_client.make_outboxes(ble_client.NODES)
    downlink = asyncio.Queue()
    for _ in range(2):
        downlink.put_nowait({"type": "VIBRATE", "node": 2, "pattern": 1})

    with caplog.at_level(logging.WARNING, logger="pipeline_board_broker"):
        await drain(downlink, outboxes)

    assert outboxes[1].empty()
    packets = [outboxes[2].get_nowait() for _ in range(2)]
    decoded = [ble_packet.decode(packet) for packet in packets]
    assert [(t, seq) for t, seq, _ in decoded] == [(ble_packet.TYPE_CMD, 0), (ble_packet.TYPE_CMD, 1)]
    fields = ble_packet.unpack_fields(decoded[0][2])
    assert fields[0] == ble_packet.VIBRATE_RECOGNISED
    assert fields[ble_packet.CMD_KIND_FIELD] == ble_packet.CMD_VIBRATE
    assert not caplog.records, "a VIBRATE is not a sign of the wrong configuration"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "msg",
    [
        {"type": "VIBRATE", "node": 3, "pattern": 1},  # no such glove
        {"type": "VIBRATE", "node": True, "pattern": 1},
        {"type": "VIBRATE", "node": 1, "pattern": "1"},
        {"type": "VIBRATE", "node": 1},
        {"type": "VIBRATE", "node": 1, "pattern": 40000},  # not an int16
    ],
)
async def test_laptop_drops_a_malformed_vibrate_and_carries_on(msg, caplog):
    import ble_client

    outboxes = ble_client.make_outboxes(ble_client.NODES)
    downlink = asyncio.Queue()
    downlink.put_nowait(msg)
    downlink.put_nowait({"type": "VIBRATE", "node": 1, "pattern": 1})

    with caplog.at_level(logging.WARNING, logger="pipeline_board_broker"):
        await drain(downlink, outboxes)

    assert outboxes[1].qsize() == 1, "the valid one still gets through after the bad one"
    assert outboxes[2].empty()
    assert any("dropping VIBRATE" in record.getMessage() for record in caplog.records)


def test_laptop_configuration_b_has_no_mqtt_leg():
    """The point of configuration B on this side: the laptop publishes nothing.

    If an MQTT leg reappears here, B has quietly become B1 -- the board's own
    events routed down the tunnel and straight back up to a broker on the board.
    """
    import ast
    import pathlib

    import pipeline_board_broker

    tree = ast.parse(pathlib.Path(pipeline_board_broker.__file__).read_text(encoding="utf-8"))

    # The imports, not the text: the module's comments discuss aiomqtt at length
    # to explain why it is absent, and a substring search would fail on the
    # explanation rather than on the thing it explains.
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            imported.update(alias.name for alias in node.names)

    assert "aiomqtt" not in imported
    assert "mqtt_client" not in imported
    assert "resolve_broker" not in imported
