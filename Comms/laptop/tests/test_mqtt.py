"""Envelope, dedup, queue and config tests for the MQTT leg.

No broker involved. These cover the parts that decide whether a message reaches
the visualiser intact — the envelope codec, QoS 1 duplicate suppression, and the
queue-full policies — plus the address resolution that the undecided broker
location rides on.

The lesson from the 2026-09-09 BLE defects applies here too: a suite like this
cannot see the broker's behaviour. It says the envelope and the counters are
right, not that the leg works. mqtt_ping.py is what says that.
"""

import argparse
import asyncio
import json

import aiomqtt
import pytest

import mqtt_client
import mqtt_config
import mqtt_ping
from mqtt_client import (
    EnvelopeError,
    MqttStats,
    SeenEvents,
    _publish_loop,
    build_envelope,
    enqueue_event,
    parse_envelope,
)
from mqtt_config import (
    DEFAULT_HOST,
    PLAINTEXT_PORT,
    TLS_PORT,
    BrokerConfig,
    resolve_broker,
)


def encode(obj):
    """Serialise an envelope the way _publish_loop puts it on the wire."""
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


# --- envelope codec --------------------------------------------------------


def test_envelope_roundtrip():
    envelope = build_envelope({"kind": "demo", "player": 1}, seq=7)

    decoded = parse_envelope(encode(envelope))

    assert decoded["payload"] == {"kind": "demo", "player": 1}
    assert decoded["seq"] == 7


def test_event_id_encodes_source_session_and_seq():
    """The readable-id contract: the log line has to identify the message on sight."""
    envelope = build_envelope({}, seq=17, source="laptop-relay", session="3f2a1b9c")

    assert envelope["event_id"] == "laptop-relay:3f2a1b9c:17"


def test_event_ids_differ_across_sessions_at_the_same_seq():
    """A relay restart resets seq to 0; those ids must not look like duplicates.

    Without the session component the phone would discard the first events after
    every restart as already-seen, and the visualiser would sit blank.
    """
    seen = SeenEvents()
    before = build_envelope({}, seq=0, session="aaaaaaaa")
    after = build_envelope({}, seq=0, session="bbbbbbbb")

    assert seen.check(before["event_id"])
    assert seen.check(after["event_id"])


def test_parse_rejects_non_json():
    with pytest.raises(EnvelopeError, match="undecodable payload"):
        parse_envelope(b"\xff\xfe not json")


def test_parse_rejects_json_that_is_not_an_object():
    with pytest.raises(EnvelopeError, match="must be a JSON object"):
        parse_envelope(b"[1, 2, 3]")


@pytest.mark.parametrize("missing", ["event_id", "source", "seq", "ts", "payload"])
def test_parse_rejects_missing_field(missing):
    envelope = build_envelope({"kind": "demo"}, seq=1)
    del envelope[missing]

    with pytest.raises(EnvelopeError, match=f"missing '{missing}'"):
        parse_envelope(encode(envelope))


@pytest.mark.parametrize(
    ("field", "bad_value", "expected"),
    [
        ("event_id", 12, "event_id must be a string"),
        ("source", [], "source must be a string"),
        ("seq", "not-int", "seq must be an integer"),
        ("seq", True, "seq must be an integer"),  # bool subclasses int
        ("ts", None, "ts must be a number"),
        ("ts", True, "ts must be a number"),
    ],
)
def test_parse_rejects_wrong_type(field, bad_value, expected):
    """Presence is not enough — anything that passes here is forwarded to the Ultra96.

    The bool cases matter specifically: `isinstance(True, int)` is True in
    Python, so a JSON boolean would otherwise sail through as a sequence number.
    """
    envelope = build_envelope({"kind": "demo"}, seq=1)
    envelope[field] = bad_value

    with pytest.raises(EnvelopeError, match=expected):
        parse_envelope(encode(envelope))


def test_parse_rejects_the_whole_malformed_object():
    """The exact object that used to be accepted, kept as a regression guard."""
    raw = encode({"event_id": "", "source": [], "seq": "not-int", "ts": None})

    with pytest.raises(EnvelopeError):
        parse_envelope(raw)


def test_parse_rejects_empty_event_id():
    """An empty id collapses every such message onto one dedup slot.

    The first would be delivered and every later one silently discarded as a
    duplicate — a data-loss bug that produces no error anywhere.
    """
    envelope = build_envelope({}, seq=1)
    envelope["event_id"] = ""

    with pytest.raises(EnvelopeError, match="event_id must not be empty"):
        parse_envelope(encode(envelope))


# --- QoS 1 dedup -----------------------------------------------------------


def test_seen_events_suppresses_a_redelivery():
    seen = SeenEvents()

    assert seen.check("laptop-relay:abc:1") is True
    assert seen.check("laptop-relay:abc:1") is False


def test_seen_events_evicts_oldest_first():
    seen = SeenEvents(capacity=3)
    for n in range(3):
        seen.check(f"e{n}")

    seen.check("e3")  # evicts e0

    assert len(seen) == 3
    assert seen.check("e0") is True  # evicted, so it reads as new again
    assert seen.check("e2") is False  # still remembered


def test_seen_events_stays_bounded():
    """The leak guard: this runs for the length of a demo."""
    seen = SeenEvents(capacity=10)
    for n in range(1000):
        seen.check(f"e{n}")

    assert len(seen) == 10


# --- queue-full policies ---------------------------------------------------


def test_enqueue_event_drops_oldest_when_full():
    """Outbound keeps the newest game state — the opposite of the BLE outbox.

    A visualiser reconnecting after a broker outage wants the current state, not
    a replay of what the game looked like when the link died.
    """
    outbound = asyncio.Queue(maxsize=2)
    stats = MqttStats()

    for seq in range(4):
        enqueue_event(outbound, mqtt_config.gesture_topic(1), build_envelope({}, seq=seq), stats)

    remaining = [outbound.get_nowait()[1]["seq"], outbound.get_nowait()[1]["seq"]]
    assert remaining == [2, 3]
    assert stats.dropped == 2


def test_enqueue_event_does_not_drop_below_capacity():
    outbound = asyncio.Queue(maxsize=8)
    stats = MqttStats()

    for seq in range(8):
        enqueue_event(outbound, mqtt_config.gesture_topic(1), build_envelope({}, seq=seq), stats)

    assert outbound.qsize() == 8
    assert stats.dropped == 0


# --- publish ordering across a failure -------------------------------------


class FakeClient:
    """Minimal stand-in for aiomqtt.Client.

    `fail_times` publishes raise MqttError before any succeed, which is how a
    broker going away mid-session presents. `messages` never yields, which is
    the quiet-broker case that used to hang shutdown.
    """

    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.published = []
        self.subscribed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    async def publish(self, topic, payload=None, qos=0):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise aiomqtt.MqttError("broker went away")
        self.published.append(json.loads(payload.decode("utf-8")))

    @property
    async def messages(self):  # pragma: no cover - replaced below
        raise NotImplementedError


class _NeverMessages:
    """An async iterator that blocks forever, like a subscribed but idle topic."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()  # never set


@pytest.mark.asyncio
async def test_failed_publish_keeps_order_across_a_reconnect():
    """A failed event must go out FIRST on the next session, not last.

    Requeueing it instead sent it out behind everything that queued while the
    link was down: [1, 2] with 1 failing published as 2, 1, so the visualiser
    received the older game state last.
    """
    outbound = asyncio.Queue(maxsize=8)
    stats = MqttStats()
    pending = []
    stop = asyncio.Event()
    for seq in (1, 2):
        enqueue_event(outbound, mqtt_config.gesture_topic(1), build_envelope({}, seq=seq), stats)

    failing = FakeClient(fail_times=1)
    with pytest.raises(aiomqtt.MqttError):
        await _publish_loop(failing, outbound, pending, stats, stop)

    # Event 1 is still in flight, not shuffled to the back of the queue.
    assert [envelope["seq"] for _topic, envelope in pending] == [1]
    assert stats.publish_failed == 1
    assert failing.published == []

    # The reconnect: same pending slot, a working client.
    working = FakeClient()
    task = asyncio.create_task(_publish_loop(working, outbound, pending, stats, stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert [e["seq"] for e in working.published] == [1, 2]
    assert pending == []


@pytest.mark.asyncio
async def test_successful_publish_clears_the_pending_slot():
    """Otherwise every event would be re-sent forever."""
    outbound = asyncio.Queue(maxsize=8)
    stats = MqttStats()
    pending = []
    stop = asyncio.Event()
    enqueue_event(outbound, mqtt_config.gesture_topic(1), build_envelope({}, seq=1), stats)

    client = FakeClient()
    task = asyncio.create_task(_publish_loop(client, outbound, pending, stats, stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert pending == []
    assert stats.published == 1


@pytest.mark.asyncio
async def test_unserialisable_envelope_is_dropped_not_retried_forever():
    """A poison pill must not block every later event.

    json.dumps raises on a payload it cannot encode, and that failure is
    deterministic — retrying it across reconnects fails identically forever.
    Held in `pending` it would stall the whole outbound path: the queue fills,
    enqueue_event starts discarding real game state, and the visualiser never
    updates again.
    """
    outbound = asyncio.Queue(maxsize=8)
    stats = MqttStats()
    pending = []
    stop = asyncio.Event()

    enqueue_event(outbound, mqtt_config.gesture_topic(1), build_envelope({"bad": {1, 2}}, seq=1), stats)  # set: not JSON
    enqueue_event(outbound, mqtt_config.gesture_topic(1), build_envelope({"kind": "fine"}, seq=2), stats)

    client = FakeClient()
    task = asyncio.create_task(_publish_loop(client, outbound, pending, stats, stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert stats.bad_payload == 1
    assert pending == []
    # The valid event behind it still went out.
    assert [e["seq"] for e in client.published] == [2]


@pytest.mark.asyncio
async def test_unserialisable_envelope_does_not_stall_across_reconnects():
    """The specific reproduction: repeated attempts must not keep retrying it."""
    outbound = asyncio.Queue(maxsize=8)
    stats = MqttStats()
    pending = []
    enqueue_event(outbound, mqtt_config.gesture_topic(1), build_envelope({"bad": {1, 2}}, seq=1), stats)

    for _ in range(3):  # three "sessions", as the supervisor would run them
        client = FakeClient()
        stop = asyncio.Event()
        task = asyncio.create_task(_publish_loop(client, outbound, pending, stats, stop))
        await asyncio.sleep(0.02)
        # stop before cancel, as every teardown here does: a cancel landing just as
        # wait_for takes the envelope is swallowed, and without stop the loop then
        # polls the empty queue forever (CLAUDE.md, the wait_for trap).
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # Dropped once on the first attempt, not re-attempted on every reconnect.
    assert stats.bad_payload == 1


# --- session shutdown ------------------------------------------------------


@pytest.mark.asyncio
async def test_session_exits_promptly_on_stop_with_an_idle_broker(monkeypatch):
    """Setting `stop` must end the session even when no message ever arrives.

    `asyncio.wait(..., FIRST_EXCEPTION)` degrades to ALL_COMPLETED when nothing
    raises, so a normally-completing `stop.wait()` did not end the wait and the
    subscribe loop stayed parked in `async for` indefinitely. The timeout below
    is the whole test: without FIRST_COMPLETED this never returns.
    """
    client = FakeClient()
    monkeypatch.setattr(type(client), "messages", _NeverMessages(), raising=False)
    monkeypatch.setattr(mqtt_client.aiomqtt, "Client", lambda **kwargs: client)

    stop = asyncio.Event()
    task = asyncio.create_task(
        mqtt_client.one_session(
            BrokerConfig("127.0.0.1", 1883),
            asyncio.Queue(),
            asyncio.Queue(),
            [],
            SeenEvents(),
            MqttStats(),
            stop,
        )
    )
    await asyncio.sleep(0.05)
    stop.set()

    await asyncio.wait_for(task, timeout=2.0)
    assert client.subscribed == [
        (mqtt_config.TOPIC_VIZ_READY_FILTER, mqtt_config.QOS_VIZ_READY)
    ]


# --- topic contract ---------------------------------------------------------


def test_gesture_topic_fills_in_the_player():
    """Each phone subscribes to its own player, so the id has to reach the topic."""
    assert mqtt_config.gesture_topic(1) == "game/gesture/1"
    assert mqtt_config.gesture_topic(2) == "game/gesture/2"


def test_player_comes_off_the_end_of_a_viz_ready_topic():
    """The laptop subscribes with `+`, so attribution is in the topic, not the
    payload — that is what lets the relay stay ignorant of the game schema."""
    assert mqtt_config.player_from_topic("viz/ready/1") == 1
    assert mqtt_config.player_from_topic("viz/ready/2") == 2


def test_player_from_a_malformed_topic_is_none_rather_than_an_error():
    """A phone publishing to a bad topic is the visualiser's bug. It must not
    take down a session that is otherwise carrying the demo."""
    assert mqtt_config.player_from_topic("viz/ready/everyone") is None
    assert mqtt_config.player_from_topic("viz/ready") is None


def test_the_subscribe_filter_matches_the_topic_phones_publish():
    """The `+` form and the `{player}` form must stay in agreement — they are
    two constants that only ever get edited one at a time."""
    concrete = mqtt_config.TOPIC_VIZ_READY.format(player=1)
    prefix = mqtt_config.TOPIC_VIZ_READY_FILTER.rsplit("/", 1)[0]
    assert concrete.startswith(prefix + "/")
    assert mqtt_config.player_from_topic(concrete) == 1


# --- broker address resolution ---------------------------------------------


def test_resolve_broker_defaults_to_loopback(monkeypatch):
    monkeypatch.delenv("MQTT_BROKER_HOST", raising=False)
    monkeypatch.delenv("MQTT_BROKER_PORT", raising=False)

    assert resolve_broker() == BrokerConfig(DEFAULT_HOST, PLAINTEXT_PORT, False)


def test_resolve_broker_reads_environment(monkeypatch):
    """The demo-day path: change the address without touching a file."""
    monkeypatch.setenv("MQTT_BROKER_HOST", "10.160.77.244")
    monkeypatch.delenv("MQTT_BROKER_PORT", raising=False)

    assert resolve_broker().host == "10.160.77.244"


def test_explicit_argument_beats_environment(monkeypatch):
    monkeypatch.setenv("MQTT_BROKER_HOST", "10.160.77.244")

    assert resolve_broker(host="192.168.1.5").host == "192.168.1.5"


def test_tls_selects_the_tls_port(monkeypatch):
    monkeypatch.delenv("MQTT_BROKER_PORT", raising=False)

    assert resolve_broker(tls=True).port == TLS_PORT


def test_bad_environment_port_is_rejected_at_startup(monkeypatch):
    """Fail loudly here rather than as an unexplained connection refusal."""
    monkeypatch.setenv("MQTT_BROKER_PORT", "eighteen-eighty-three")

    with pytest.raises(ValueError, match="MQTT_BROKER_PORT is not a number"):
        resolve_broker()


@pytest.mark.parametrize("bad_port", [0, -1, 65536])
def test_out_of_range_environment_port_is_rejected(monkeypatch, bad_port):
    """The env layer bypasses argparse, so it needs its own range check."""
    monkeypatch.setenv("MQTT_BROKER_PORT", str(bad_port))

    with pytest.raises(ValueError, match="out of range"):
        resolve_broker()


@pytest.mark.parametrize("bad_port", [0, 65536])
def test_out_of_range_explicit_port_is_rejected(monkeypatch, bad_port):
    monkeypatch.delenv("MQTT_BROKER_PORT", raising=False)

    with pytest.raises(ValueError, match="out of range"):
        resolve_broker(port=bad_port)


# --- CLI value validation --------------------------------------------------


@pytest.mark.parametrize("bad", ["0", "-1", "70000", "not-a-port"])
def test_port_number_rejects_bad_values(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        mqtt_config.port_number(bad)


@pytest.mark.parametrize("bad", ["0", "-5", "1.5", "many"])
def test_positive_int_rejects_bad_values(bad):
    """`--count 0` would otherwise report "no pings returned" and read as a network fault."""
    with pytest.raises(argparse.ArgumentTypeError):
        mqtt_config.positive_int(bad)


@pytest.mark.parametrize("bad", ["-0.5", "soon"])
def test_non_negative_float_rejects_bad_values(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        mqtt_config.non_negative_float(bad)


def test_non_negative_float_allows_zero():
    """0 is meaningful: it means "off" for --publish-every and "back to back" for --interval."""
    assert mqtt_config.non_negative_float("0") == 0.0


# --- ping statistics -------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "expected_rank"),
    [(20, 19), (30, 29), (11, 11), (40, 38)],
)
def test_percentile_uses_nearest_rank_not_rounding(n, expected_rank):
    """ceil(0.95*n), not round(0.95*n) — they disagree for most n in 11-19 and 30-39.

    Values are 1..n so the returned value IS the rank, which makes the
    off-by-one visible. At n=30 rounding picks the 28th sample where the
    definition wants the 29th, reporting a p95 one sample too optimistic.
    """
    assert mqtt_ping.percentile(list(range(1, n + 1)), 0.95) == expected_rank


def test_percentile_handles_a_single_sample():
    assert mqtt_ping.percentile([4.2], 0.95) == 4.2


class _MessageList:
    """Async-iterable stand-in for aiomqtt's client.messages."""

    class _Msg:
        def __init__(self, obj):
            self.payload = json.dumps(obj).encode("utf-8")
            self.topic = mqtt_config.TOPIC_PING

    def __init__(self, objects):
        self._objects = [self._Msg(o) for o in objects]

    @property
    def messages(self):
        async def gen():
            for msg in self._objects:
                yield msg

        return gen()


@pytest.mark.parametrize("bad_seq", [[], {}, "nope", None, True, -1])
@pytest.mark.asyncio
async def test_collector_survives_a_bad_seq(bad_seq):
    """An unusable seq must be skipped, not kill the collector.

    A list or dict is unhashable and raises TypeError from `dict.pop` itself,
    which is why the check has to happen before the lookup. The failure mode is
    nasty: the collector task dies mid-run and the ping report just stops
    counting, reading as packet loss rather than as a crash.
    """
    client = _MessageList([{"seq": 0}, {"seq": bad_seq}, {"seq": 1}])
    rtts, expected, done = [], {0: 0.0, 1: 0.0}, asyncio.Event()

    await asyncio.wait_for(mqtt_ping._collect(client, rtts, expected, done), timeout=2.0)

    assert len(rtts) == 2  # both real pings measured; the bad one skipped
    assert done.is_set()


@pytest.mark.asyncio
async def test_collector_skips_a_message_with_no_seq():
    client = _MessageList([{"seq": 0}, {"not-seq": 1}])
    rtts, expected, done = [], {0: 0.0}, asyncio.Event()

    await asyncio.wait_for(mqtt_ping._collect(client, rtts, expected, done), timeout=2.0)

    assert len(rtts) == 1


# --- programmatic validator edges ------------------------------------------


@pytest.mark.parametrize("bad", [1.5, True, None])
def test_port_number_rejects_non_integral_values(bad):
    """int(1.5) truncates to 1, so a float port would silently become a different port."""
    with pytest.raises(argparse.ArgumentTypeError):
        mqtt_config.port_number(bad)


def test_port_number_accepts_an_integral_float():
    assert mqtt_config.port_number(1883.0) == 1883


@pytest.mark.parametrize("bad", [2.5, True])
def test_positive_int_rejects_non_integral_values(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        mqtt_config.positive_int(bad)


def test_resolve_broker_rejects_a_float_port(monkeypatch):
    monkeypatch.delenv("MQTT_BROKER_PORT", raising=False)

    with pytest.raises(ValueError, match="whole number"):
        resolve_broker(port=1.5)


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_non_negative_float_rejects_non_finite(bad):
    """A nan interval would make every comparison false and never elapse."""
    with pytest.raises(argparse.ArgumentTypeError):
        mqtt_config.non_negative_float(bad)


def test_describe_shows_scheme_host_and_port():
    """This line is the first thing read when the phone cannot connect."""
    assert BrokerConfig("10.160.77.244", 1883).describe() == "mqtt://10.160.77.244:1883"
    assert BrokerConfig("10.160.77.244", 8883, tls=True).describe() == (
        "mqtts://10.160.77.244:8883"
    )


def test_tls_params_reports_missing_certs_by_name(monkeypatch, tmp_path):
    """TLS before Week 6 must say "no certs", not fail as a handshake error."""
    monkeypatch.setattr(mqtt_config, "CA_CERT", tmp_path / "ca.crt")
    monkeypatch.setattr(mqtt_config, "CLIENT_CERT", tmp_path / "laptop-relay.crt")
    monkeypatch.setattr(mqtt_config, "CLIENT_KEY", tmp_path / "laptop-relay.key")

    with pytest.raises(FileNotFoundError, match="ca.crt"):
        mqtt_config.tls_params()
