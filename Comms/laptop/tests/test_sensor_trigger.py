"""--sensor-trigger: AI events from the SENSOR stream, for latency runs.

The keystroke trigger makes two events per keypress a few ms apart, and the second
queues behind the first on A's return path, which P1 never does. This trigger stands
in for the last packet of a gesture window instead. notes/latency.md 3.9.
"""

import asyncio
import contextlib
import logging
import sys
from pathlib import Path

import pytest

from framing import recv_message, send_message
from helpers import DEADLINE_S, connected, serving, wait_until

import mqtt_publisher  # noqa: E402 - ultra96/, on sys.path via conftest
import tcp_client  # noqa: E402
import tcp_client_mqtt  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import latency_report  # noqa: E402

PREFIX = "2026-10-02 12:00:00,000Z [x] "


def sensor(node, seq, t=50.0):
    return {"type": "SENSOR", "node": node, "seq": seq, "timestamp": t, "fields": [1, 2, 3]}


class ScriptedRng:
    """randint() returns the scripted values in order, whatever the bounds."""

    def __init__(self, values):
        self.values = list(values)
        self.bounds = []

    def randint(self, low, high):
        self.bounds.append((low, high))
        return self.values.pop(0)


# --- which packets fire ------------------------------------------------------


def test_each_interval_is_drawn_between_half_and_one_and_a_half_times_the_mean():
    trigger = tcp_client.SensorTrigger(10, rng=__import__("random").Random(4002))
    fired = [i for i in range(1, 5001) if trigger.hit(1)]
    intervals = [b - a for a, b in zip([0] + fired, fired)]

    assert all(5 <= gap <= 15 for gap in intervals), intervals
    # Random, not every 10th: a fixed interval phase-locks the two nodes.
    assert len(set(intervals)) > 5
    assert 9 <= sum(intervals) / len(intervals) <= 11


def test_bounds_for_a_realistic_and_a_degenerate_mean():
    rng = ScriptedRng([5, 5])  # neither fires on its first packet, so one draw each
    tcp_client.SensorTrigger(100, rng=rng).hit(1)
    tcp_client.SensorTrigger(1, rng=rng).hit(1)
    assert rng.bounds == [(50, 150), (1, 1)]


def test_nodes_are_counted_separately():
    """Node 1 draws 3 and node 2 draws 7: each fires on its own nth packet, however the
    two streams interleave."""
    trigger = tcp_client.SensorTrigger(5, rng=ScriptedRng([3, 7, 99, 99]))
    fired = []
    for i in range(1, 9):
        for node in (1, 2):
            if trigger.hit(node):
                fired.append((node, i))
    assert fired == [(1, 3), (2, 7)]


def test_the_count_restarts_from_a_fresh_draw_after_each_trigger():
    trigger = tcp_client.SensorTrigger(5, rng=ScriptedRng([2, 4, 99]))
    assert [trigger.hit(1) for _ in range(6)] == [False, True, False, False, False, True]


# --- the receiver ------------------------------------------------------------


async def _run_receiver(frames, caplog, until, sensor_trigger=None):
    """Feed `frames` to the real receiver until `until(caplog)` holds."""

    async def handler(reader, writer):
        for frame in frames:
            await send_message(writer, frame)
        await asyncio.sleep(DEADLINE_S)

    async with serving(handler) as port:
        async with connected(port) as (reader, writer):
            stop = asyncio.Event()
            task = asyncio.create_task(
                tcp_client.receiver(
                    reader, writer, stop, tcp_client.EventSource(), sensor_trigger
                )
            )
            try:
                with caplog.at_level(logging.INFO, logger="tcp_client"):
                    for _ in range(int(DEADLINE_S / 0.01)):
                        if until(caplog) or task.done():
                            break
                        await asyncio.sleep(0.01)
            finally:
                stop.set()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            return task


def _ai_events(caplog):
    return [m for m in caplog.messages if m.startswith("AI event")]


@pytest.mark.asyncio
async def test_sensor_packets_trigger_nothing_by_default(caplog):
    """The ECHO after them is the sync point: once its event is logged, every SENSOR
    before it has been read."""
    frames = [sensor(1, seq) for seq in range(30)]
    frames.append({"type": "ECHO", "node": 2, "seq": 0, "timestamp": 1.0, "fields": [-99]})

    await _run_receiver(frames, caplog, lambda log: _ai_events(log))

    assert [m.split(" player=")[1][:1] for m in _ai_events(caplog)] == ["2"]


@pytest.mark.asyncio
async def test_a_picked_sensor_packet_makes_a_joinable_trigger_and_event(caplog):
    """Read back through the report itself, so the trigger line and the join agree."""
    trigger = tcp_client.SensorTrigger(3, rng=ScriptedRng([2, 1, 99, 99]))
    frames = [sensor(1, 254, 10.5), sensor(2, 17, 11.25), sensor(1, 255, 12.75)]

    await _run_receiver(frames, caplog, lambda log: len(_ai_events(log)) == 2, trigger)

    counts = latency_report.Counts()
    events = latency_report.parse_board(
        [PREFIX + m for m in caplog.messages], "A", counts
    )
    assert [(e.player, e.node, e.ble_seq, e.t_laptop_rx) for e in events] == [
        ("2", 2, "17", 11.25),
        ("1", 1, "255", 12.75),
    ]
    assert counts.triggers_without_event == counts.events_without_trigger == 0
    assert all("key=None" in m for m in _ai_events(caplog))


@pytest.mark.asyncio
async def test_a_sensor_without_an_integer_node_is_skipped_not_fatal(caplog):
    trigger = tcp_client.SensorTrigger(1)
    frames = [
        {"type": "SENSOR", "node": "1", "seq": 0, "timestamp": 1.0},
        {"type": "SENSOR", "seq": 1, "timestamp": 1.0},
        sensor(2, 2),
    ]

    task = await _run_receiver(frames, caplog, lambda log: _ai_events(log), trigger)

    assert not task.done() or task.cancelled(), "the receiver ended on a bad SENSOR"
    assert len(_ai_events(caplog)) == 1


# --- both configurations' clients are wired ----------------------------------


@pytest.mark.asyncio
async def test_configuration_a_session_sends_a_sensor_triggered_event():
    received = []

    async def laptop(reader, writer):
        await send_message(writer, sensor(2, 5))
        while True:
            msg = await recv_message(reader)
            if msg.get("type") == "AI_EVENT":
                received.append(msg)
                writer.close()  # ends the board's session
                return

    async with serving(laptop) as port:
        await asyncio.wait_for(
            tcp_client.one_session("127.0.0.1", port, sensor_every=1), timeout=DEADLINE_S
        )

    assert [e["player"] for e in received] == [2]


@pytest.mark.asyncio
async def test_configuration_b_session_queues_a_sensor_triggered_event():
    outbound = asyncio.Queue(maxsize=8)
    events = tcp_client_mqtt.MqttEventSource(outbound, mqtt_publisher.PublishStats())
    done = asyncio.Event()

    async def laptop(reader, writer):
        await send_message(writer, sensor(1, 5))
        await done.wait()
        writer.close()

    async with serving(laptop) as port:
        session = asyncio.create_task(
            tcp_client_mqtt.one_session("127.0.0.1", port, events, sensor_every=1)
        )
        try:
            await wait_until(lambda: not outbound.empty())
        finally:
            done.set()
            await asyncio.wait_for(session, timeout=DEADLINE_S)

    topic = outbound.get_nowait()[0]
    assert topic.endswith("1")


@pytest.mark.parametrize(
    "module, argv",
    [
        (tcp_client, ["tcp_client.py"]),
        (tcp_client_mqtt, ["tcp_client_mqtt.py"]),
    ],
)
def test_the_flag_reaches_run_and_defaults_off(module, argv, monkeypatch):
    seen = []

    async def fake_run(*args):
        seen.append(args[-1])

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "setup_logging", lambda *a: "log")
    for extra in ([], ["--sensor-trigger", "100"]):
        monkeypatch.setattr(sys, "argv", argv + extra)
        module.main()

    assert seen == [0, 100]


@pytest.mark.asyncio
async def test_configuration_a_run_passes_the_flag_to_every_session(monkeypatch):
    seen = []

    async def failing_session(host, port, event_every=0.0, sensor_every=0):
        seen.append(sensor_every)
        raise OSError("refused")

    monkeypatch.setattr(tcp_client, "one_session", failing_session)
    monkeypatch.setattr(tcp_client, "RECONNECT_BACKOFF_S", 0.01)
    task = asyncio.create_task(tcp_client.run("127.0.0.1", 1, 0.0, 7))
    try:
        await wait_until(lambda: len(seen) >= 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert set(seen) == {7}


@pytest.mark.asyncio
async def test_configuration_b_run_passes_the_flag_to_every_session(monkeypatch):
    seen = []

    async def failing_session(host, port, events, event_every=0.0, sensor_every=0):
        seen.append(sensor_every)
        raise OSError("refused")

    async def idle_publisher(host, port, outbound, stop, stats):
        await stop.wait()

    monkeypatch.setattr(tcp_client_mqtt, "one_session", failing_session)
    monkeypatch.setattr(tcp_client_mqtt, "RECONNECT_BACKOFF_S", 0.01)
    monkeypatch.setattr(mqtt_publisher, "run_publisher", idle_publisher)
    task = asyncio.create_task(tcp_client_mqtt.run("127.0.0.1", 1, "127.0.0.1", 1883, 0.0, 7))
    try:
        await wait_until(lambda: len(seen) >= 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert set(seen) == {7}
