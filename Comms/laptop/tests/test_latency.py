"""Latency measurement on the laptop and board: the heartbeat offset, the trigger line, the per-event
times on both board clients' lines, and scripts/tools/latency_report.py's join.

The report is fed lines the real code produced wherever it can,
so a renamed field fails here rather than as an empty report after a hardware run.
"""

import asyncio
import contextlib
import csv
import io
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import mqtt_client
import pipeline
import tcp_server
from framing import recv_message, send_message
from helpers import DEADLINE_S, connected, serving

import ai_events  # noqa: E402 - ultra96/, on sys.path via conftest
import gesture  # noqa: E402
import mqtt_publisher  # noqa: E402
import tcp_client  # noqa: E402
import tcp_client_mqtt  # noqa: E402

TOOLS = Path(__file__).resolve().parents[2] / "scripts" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import latency_report  # noqa: E402

PREFIX = "2026-10-02 12:00:00,000Z [x] "  # a log line's time, which the report must ignore


def lines(*messages):
    return [PREFIX + m for m in messages]


def joined(*args, **kwargs):
    """report()'s events and counts, for tests that need nothing else from the run."""
    run = latency_report.report(*args, **kwargs)
    return run.events, run.counts


# --- the offset estimator --------------------------------------------------


def test_offset_keeps_the_lowest_rtt_sample():
    clock = tcp_client.ClockOffset()
    # Board clock = laptop + 1000. A slow round trip skews (t1 + t3)/2; the fast one doesn't.
    clock.add(t1=1000.0, t2=0.050, t3=1000.100, now=0.0)  # rtt 100 ms, offset 1000.000
    clock.add(t1=1001.0, t2=1.001, t3=1001.002, now=1.0)  # rtt 2 ms, offset 1000.000
    clock.add(t1=1002.0, t2=2.010, t3=1002.080, now=2.0)  # rtt 80 ms, offset 999.99 + 0.04

    offset, rtt = clock.best()
    assert rtt == pytest.approx(0.002)
    assert offset == pytest.approx(1000.0)


def test_offset_window_expires_old_samples():
    clock = tcp_client.ClockOffset(window_s=30.0)
    clock.add(1000.0, 0.0005, 1000.001, now=0.0)  # the best ever, but it will expire
    clock.add(1040.0, 40.005, 1040.010, now=40.0)

    _, rtt = clock.best()
    assert rtt == pytest.approx(0.010)


@pytest.mark.parametrize(
    "t1, t2",
    [(None, 1.0), (1.0, None), ("1.0", 1.0), (True, 1.0), (1.0, False)],
)
def test_an_ack_without_usable_times_is_ignored(t1, t2):
    """An older laptop sends no t1/t2; that must not crash or spoil the estimate."""
    clock = tcp_client.ClockOffset()
    clock.add(t1, t2, 5.0, now=0.0)
    assert clock.best() is None
    assert clock.describe() == "offset=- rtt=-"


def test_a_negative_rtt_is_ignored():
    """The board's clock stepped back between send and receive."""
    clock = tcp_client.ClockOffset()
    clock.add(1000.0, 0.0, 999.0, now=0.0)
    assert clock.best() is None


# --- the ACK on the wire ---------------------------------------------------


@pytest.mark.asyncio
async def test_server_ack_carries_t1_and_t2_as_numbers():
    """Types, not presence: a t1 that came back as a string would still be 'there'."""
    async with serving(
        lambda r, w: tcp_server.handle_client(r, w, downlink=asyncio.Queue(maxsize=8))
    ) as port:
        async with connected(port) as (reader, writer):
            await send_message(writer, {"type": "HEARTBEAT", "seq": 3, "ts": 1759406400.123456})
            ack = await asyncio.wait_for(recv_message(reader), DEADLINE_S)
            # An older board sends no ts; the ACK still goes out.
            await send_message(writer, {"type": "HEARTBEAT", "seq": 4})
            bare = await asyncio.wait_for(recv_message(reader), DEADLINE_S)

    assert ack["t1"] == 1759406400.123456 and type(ack["t1"]) is float
    assert type(ack["t2"]) is float
    assert (bare["type"], bare["seq"], bare["t1"]) == ("HEARTBEAT_ACK", 4, None)
    assert type(bare["t2"]) is float


async def _run_receiver(server_handler, caplog, until, events=None):
    """Run the real receiver against `server_handler` until `until(caplog)` holds.

    Return whether the receiver ended by itself before the test stopped it.
    """
    events = events or tcp_client.EventSource()
    async with serving(server_handler) as port:
        async with connected(port) as (reader, writer):
            stop = asyncio.Event()
            task = asyncio.create_task(tcp_client.receiver(reader, writer, stop, events))
            try:
                with caplog.at_level(logging.INFO, logger="tcp_client"):
                    for _ in range(int(DEADLINE_S / 0.01)):
                        if until(caplog) or task.done():
                            break
                        await asyncio.sleep(0.01)
                # checked before stopping it, since stop then lets it end cleanly
                ended_by_itself = task.done()
            finally:
                stop.set()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            return ended_by_itself


@pytest.mark.asyncio
async def test_receiver_survives_bad_acks_and_reports_the_offset(monkeypatch, caplog):
    """No t1/t2, a repeated seq, a missing seq: none may end the session. A good ACK
    after them reaches the 1 Hz line, where latency_report reads it."""
    monkeypatch.setattr(tcp_client, "PROGRESS_INTERVAL_S", 0.01)

    def has_offset(log):
        return any(
            latency_report.RATE.search(m) for m in log.messages if "uplink rx:" in m
        )

    bad_acks = (
        {"type": "HEARTBEAT_ACK", "seq": 0},
        {"type": "HEARTBEAT_ACK", "seq": 0, "t1": None, "t2": "x"},
        {"type": "HEARTBEAT_ACK"},
    )

    async def acks(reader, writer):
        # resend until each line is logged, since the receiver's clock ticks every 15.6 ms on Windows
        for _ in range(int(DEADLINE_S / 0.02)):
            if any("offset=- rtt=-" in m for m in caplog.messages):
                break
            for ack in bad_acks:
                await send_message(writer, ack)
            await asyncio.sleep(0.02)
        for _ in range(int(DEADLINE_S / 0.02)):
            if has_offset(caplog):
                break
            good = {"type": "HEARTBEAT_ACK", "seq": 5, "t1": tcp_client.time.time(), "t2": 12.5}
            await send_message(writer, good)
            await asyncio.sleep(0.02)
        await asyncio.sleep(DEADLINE_S)

    ended_by_itself = await _run_receiver(acks, caplog, has_offset)

    assert not ended_by_itself, "the receiver ended on a bad ACK"
    rates = [m for m in caplog.messages if "uplink rx:" in m]
    assert any("offset=- rtt=-" in m for m in rates)
    assert has_offset(caplog)


@pytest.mark.asyncio
async def test_trigger_line_carries_node_seq_and_the_frames_timestamp(caplog):
    """Read back through the report's own regex, so the two cannot drift apart."""
    echo = {"type": "ECHO", "node": 2, "seq": 41, "timestamp": 77.25, "fields": [-99, 0]}

    async def sends_echo(reader, writer):
        await send_message(writer, echo)
        await asyncio.sleep(DEADLINE_S)

    await _run_receiver(sends_echo, caplog, lambda log: "AI event" in log.text)

    triggers = [latency_report.TRIGGER.search(m) for m in caplog.messages]
    triggers = [m for m in triggers if m]
    assert len(triggers) == 1
    assert (triggers[0]["node"], triggers[0]["seq"]) == ("2", "41")
    assert float(triggers[0]["t_laptop_rx"]) == 77.25
    assert float(triggers[0]["t_rx"]) > 1e9  # board wall clock, not the frame's time


# --- both emits write the event's own timestamp ----------------------------


FIXED_EVENT_TIME = 1759406400.654321  # far from when the line is logged


@pytest.fixture
def fixed_event_time(monkeypatch):
    """Pin the event's timestamp without touching time.time() for logging itself,
    so a parser that fell back to the line's own time would read a different value."""
    monkeypatch.setattr(ai_events, "time", SimpleNamespace(time=lambda: FIXED_EVENT_TIME))


@pytest.mark.asyncio
async def test_configuration_a_emit_logs_the_events_timestamp(fixed_event_time, caplog):
    class Writer:
        def write(self, data):
            pass

        async def drain(self):
            pass

    with caplog.at_level(logging.INFO, logger="tcp_client"):
        await tcp_client.EventSource().emit(Writer(), 1, "c")

    events = latency_report.parse_board(
        lines(*caplog.messages), "A", latency_report.Counts()
    )
    assert [e.t_event for e in events] == [pytest.approx(FIXED_EVENT_TIME, abs=1e-6)]


@pytest.mark.asyncio
async def test_configuration_b_emit_logs_the_events_timestamp(fixed_event_time, caplog):
    source = tcp_client_mqtt.MqttEventSource(asyncio.Queue(maxsize=8), mqtt_publisher.PublishStats())
    with caplog.at_level(logging.INFO, logger="tcp_client_mqtt"):
        await source.emit(None, 2, "s")

    events = latency_report.parse_board(
        lines(*caplog.messages), "B", latency_report.Counts()
    )
    assert [e.t_event for e in events] == [pytest.approx(FIXED_EVENT_TIME, abs=1e-6)]
    assert events[0].player == "2"


class _NullWriter:
    def write(self, data):
        pass

    async def drain(self):
        pass


@pytest.mark.asyncio
async def test_both_emits_t_infer_ms_becomes_the_inference_row(caplog, tmp_path):
    """Both emits' real lines give a classified event an inference row, and a dummy none."""
    result = gesture.Result("mage", 0.75, 5.25, 0)
    source_b = tcp_client_mqtt.MqttEventSource(asyncio.Queue(maxsize=8), mqtt_publisher.PublishStats())
    board = []
    for config, emit, logger in (
        ("A", lambda r: tcp_client.EventSource().emit(_NullWriter(), 1, None, r), "tcp_client"),
        ("B", lambda r: source_b.emit(_NullWriter(), 1, None, r), "tcp_client_mqtt"),
    ):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=logger):
            await emit(result)
            await emit(None)  # a dummy event, so no inference time
        board = lines(
            "uplink rx: total=1 rate=1.0 msg/s, 0 AI events sent, offset=1000.000000 rtt=2.000ms",
            *(m for m in caplog.messages if m.startswith("AI event")),
        )
        run = latency_report.report(board, config)

        classified, dummy = run.events
        assert classified.t_infer_ms == 5.25, (config, board)
        assert classified.hops["inference"] == 5.25
        assert dummy.t_infer_ms is None and "inference" not in dummy.hops

    out = io.StringIO()
    latency_report.print_summary(run, "B", out=out)
    (row,) = [line for line in out.getvalue().splitlines() if "inference (t_infer_ms)" in line]
    assert row.split()[-4:] == ["1", "5.250", "5.250", "5.250"]  # n, median, p95, max

    latency_report.write_csv(run, tmp_path / "e.csv")
    with open(tmp_path / "e.csv", newline="", encoding="utf-8") as f:
        assert [r["inference"] for r in csv.DictReader(f)] == ["5.25", ""]


# --- the publish and downlink lines, as the real code writes them -----------


class _AcceptingClient:
    def __init__(self):
        self.published = []

    async def publish(self, topic, payload=None, qos=0):
        self.published.append(topic)


async def _publish_one(loop_coro, client, stop):
    task = asyncio.create_task(loop_coro)
    try:
        for _ in range(2000):
            if client.published:
                return
            await asyncio.sleep(0)
        pytest.fail("the publish loop never published")
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_both_publish_lines_carry_a_time_the_report_reads(caplog):
    for module, logger, stats in (
        (mqtt_client, "mqtt_client", mqtt_client.MqttStats()),
        (mqtt_publisher, "mqtt_publisher", mqtt_publisher.PublishStats()),
    ):
        caplog.clear()
        outbound, stop, client = asyncio.Queue(maxsize=8), asyncio.Event(), _AcceptingClient()
        module.enqueue_event(outbound, "game/gesture/1", {"event_id": "src:s:7"}, stats)
        with caplog.at_level(logging.INFO, logger=logger):
            await _publish_one(module._publish_loop(client, outbound, [], stats, stop), client, stop)

        found = [latency_report.PUBLISHED.search(m) for m in caplog.messages]
        found = [m for m in found if m]
        assert len(found) == 1, (module.__name__, caplog.messages)
        assert latency_report._event_id_seq(found[0]["event_id"]) == 7
        assert float(found[0]["t"]) > 0


@pytest.mark.asyncio
async def test_a_downlink_line_joins_to_its_board_event(caplog):
    """The real forward_downlink line, single-quoted dict and all, joins exactly."""
    event = {"type": "AI_EVENT", "player": 1, "seq": 4, "gesture": "arc",
             "mana_cost": 20, "timestamp": 1000.123456789}
    downlink, outbound, stop = asyncio.Queue(maxsize=8), asyncio.Queue(maxsize=8), asyncio.Event()
    task = asyncio.create_task(
        pipeline.forward_downlink(downlink, outbound, mqtt_client.MqttStats(), stop)
    )
    try:
        with caplog.at_level(logging.INFO, logger="pipeline"):
            downlink.put_nowait(event)
            await asyncio.wait_for(outbound.get(), DEADLINE_S)
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    downlink_lines = [m for m in caplog.messages if "downlink seq=" in m]
    assert "'seq': 4" in downlink_lines[0]  # the single-quoted form the report must parse

    board = lines(
        "uplink rx: total=1 rate=1.0 msg/s, 0 AI events sent, offset=900.000000 rtt=2.000ms",
        "trigger node=1 seq=9 t_laptop_rx=100.000000 t_rx=1000.120000",
        "AI event seq=4 player=1 key='a' gesture=arc mana=20 t_event=1000.123457",
    )
    events, counts = joined(board, "A", lines(*downlink_lines))
    assert events[0].relay_seq == 0
    assert "event_to_laptop_downlink" in events[0].hops
    assert counts.downlinks_unmatched == 0


# --- latency_report: the join and the arithmetic ----------------------------


B_RUN = lines(
    # board clock = laptop perf_counter + 1000 s, rtt 2 ms
    "uplink rx: total=5 rate=5.0 msg/s, 0 AI events sent, offset=1000.000000 rtt=2.000ms",
    "trigger node=1 seq=10 t_laptop_rx=50.000000 t_rx=1050.004000",
    "AI event seq=0 player=1 key='c' gesture=circle mana=10 t_event=1050.005000 -> game/gesture/1",
    "published ultra96-relay:abc:0 to game/gesture/1 t=1050.060000",
)


def test_offset_is_applied_with_the_right_sign():
    """A flipped sign would move each cross-clock hop by twice the offset: 2000 s."""
    events, _ = joined(B_RUN, "B")
    hops = events[0].hops
    assert hops["laptop_rx_to_board_rx"] == pytest.approx(4.0)
    assert hops["board_rx_to_event"] == pytest.approx(1.0)
    assert hops["laptop_rx_to_event"] == pytest.approx(5.0)
    assert hops["event_to_publish_acked"] == pytest.approx(55.0)


def test_hop_4_comes_from_the_events_timestamp_not_the_lines_time():
    """Every line in B_RUN is stamped 12:00:00,000; only t_event can give 1 ms."""
    events, _ = joined(B_RUN, "B")
    assert events[0].t_event == 1050.005


def test_a_events_are_joined_exactly_even_when_a_reconnect_reuses_board_seq():
    """tcp_client's EventSource restarts seq per TCP session, so (player, seq) alone is not unique."""
    board = lines(
        "uplink rx: total=1 rate=1.0 msg/s, 0 AI events sent, offset=1000.000000 rtt=2.000ms",
        "trigger node=1 seq=1 t_laptop_rx=10.000000 t_rx=1010.003000",
        "AI event seq=0 player=1 key='c' gesture=circle mana=10 t_event=1010.004000",
        "trigger node=1 seq=2 t_laptop_rx=20.000000 t_rx=1020.003000",
        "AI event seq=0 player=1 key='c' gesture=circle mana=10 t_event=1020.004000",
    )
    laptop = lines(
        "downlink seq=0 t=10.010000 -> game/gesture/1: {'type': 'AI_EVENT', 'player': 1, "
        "'seq': 0, 'gesture': 'circle', 'mana_cost': 10, 'timestamp': 1010.004}",
        "published laptop-relay:s:0 to game/gesture/1 t=10.030000",
        "downlink seq=1 t=20.020000 -> game/gesture/1: {'type': 'AI_EVENT', 'player': 1, "
        "'seq': 0, 'gesture': 'circle', 'mana_cost': 10, 'timestamp': 1020.004}",
        "published laptop-relay:s:1 to game/gesture/1 t=20.050000",
    )
    events, counts = joined(board, "A", laptop)

    assert [e.hops["event_to_laptop_downlink"] for e in events] == [
        pytest.approx(6.0),
        pytest.approx(16.0),
    ]
    assert [e.hops["event_to_publish_acked"] for e in events] == [
        pytest.approx(26.0),
        pytest.approx(46.0),
    ]
    assert counts.downlinks_unmatched == 0 and counts.events_without_downlink == 0


def test_missing_links_are_counted_not_dropped():
    board = lines(
        "uplink rx: total=1 rate=1.0 msg/s, 0 AI events sent, offset=1000.000000 rtt=2.000ms",
        "trigger node=1 seq=1 t_laptop_rx=10.000000 t_rx=1010.003000",  # emit failed
        "trigger node=2 seq=2 t_laptop_rx=11.000000 t_rx=1011.003000",
        "AI event seq=0 player=2 key='s' gesture=square mana=10 t_event=1011.004000 -> game/gesture/2",
        "AI event seq=1 player=1 key=None gesture=arc mana=10 t_event=1012.000000 -> game/gesture/1",
        "published ultra96-relay:abc:9 to game/gesture/1 t=1013.000000",  # no such event
    )
    events, counts = joined(board, "B")

    assert len(events) == 2
    assert counts.triggers_without_event == 1
    assert counts.events_without_trigger == 1  # the ticker's event
    assert counts.published_unmatched == 1
    assert counts.events_without_published == 2


def test_a_downlink_whose_key_cannot_be_hashed_is_unmatched_not_a_crash():
    """A list where the board seq should be counts as an unmatched downlink."""
    board = lines(
        "uplink rx: total=1 rate=1.0 msg/s, 0 AI events sent, offset=1000.000000 rtt=2.000ms",
        "AI event seq=0 player=1 key='c' gesture=circle mana=10 t_event=1010.004000",
    )
    laptop = lines(
        "downlink seq=0 t=10.010000 -> game/gesture/1: {'type': 'AI_EVENT', 'player': 1, "
        "'seq': [0], 'gesture': 'circle', 'timestamp': 1010.004}",
    )
    _, counts = joined(board, "A", laptop)
    assert counts.downlinks_unmatched == 1


def test_a_trigger_never_pairs_with_another_players_event():
    """A trigger from node 1 must not be matched to the next event if it is for player 2."""
    board = lines(
        "uplink rx: total=1 rate=1.0 msg/s, 0 AI events sent, offset=1000.000000 rtt=2.000ms",
        "trigger node=1 seq=1 t_laptop_rx=10.000000 t_rx=1010.003000",
        "AI event seq=0 player=2 key=None gesture=arc mana=10 t_event=1011.000000 -> game/gesture/2",
    )
    events, counts = joined(board, "B")

    assert events[0].t_laptop_rx is None
    assert counts.triggers_without_event == 1
    assert counts.events_without_trigger == 1


def test_events_before_the_first_offset_take_the_first_one_measured():
    board = lines(
        "trigger node=1 seq=1 t_laptop_rx=10.000000 t_rx=1010.003000",
        "AI event seq=0 player=1 key='c' gesture=circle mana=10 t_event=1010.004000",
        "uplink rx: total=1 rate=1.0 msg/s, 1 AI events sent, offset=1000.000000 rtt=2.000ms",
    )
    events, counts = joined(board, "A")
    assert events[0].hops["laptop_rx_to_event"] == pytest.approx(4.0)
    assert counts.events_without_offset == 0


def test_summary_uses_nearest_rank_p95():
    assert latency_report.summarise(list(range(1, 101))) == (100, 50.5, 95, 100)
    assert latency_report.summarise([7.0]) == (1, 7.0, 7.0, 7.0)
    assert latency_report.summarise([]) is None


def test_a_wrong_offset_is_flagged_by_causality():
    """Wall clocks can disagree by 200 ms with the offset still right (2 Oct), so the
    check is a hop going negative, not a wall-clock comparison."""
    import io

    good = latency_report.report(B_RUN, "B")
    wrong = latency_report.report(
        [line.replace("offset=1000.000000", "offset=1000.050000") for line in B_RUN], "B"
    )
    outputs = []
    for run in (good, wrong):
        buffer = io.StringIO()
        latency_report.print_summary(run, "B", out=buffer)
        outputs.append(buffer.getvalue())

    assert "(ok)" in outputs[0] and "NEGATIVE" not in outputs[0]
    assert "NEGATIVE" in outputs[1]
