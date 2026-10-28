"""How the board turns packets into AI events, in both configurations.

An ECHO always makes a dummy event. With --gesture-flag every SENSOR sample goes to AI, and
each gesture it classifies makes one event. The transport rules are tested in test_gesture.py.
"""

import asyncio
import contextlib
import logging
import sys
from pathlib import Path

import pytest

from framing import recv_message, send_message
from helpers import DEADLINE_S, connected, serving, wait_until

import ai_events  # noqa: E402 - ultra96/, on sys.path via conftest
import gesture  # noqa: E402
import mqtt_publisher  # noqa: E402
import tcp_client  # noqa: E402
import tcp_client_mqtt  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import latency_report  # noqa: E402

PREFIX = "2026-10-02 12:00:00,000Z [x] "


def sensor(node, seq, t=50.0, status=gesture.IMU_OK, flag=0):
    imu = dict(zip(gesture.CHANNELS, (seq * 100, 1, 2, 3, 4, 5)))
    reserved = [status, flag, 0, 0, 0]
    return {"type": "SENSOR", "node": node, "seq": seq & 0xFF, "timestamp": t, "imu": imu, "reserved": reserved}


def flagged_gesture(node, first_seq, n, t0):
    """n flagged SENSOR packets 10 ms apart, then the unflagged one that ends the gesture."""
    frames = [sensor(node, first_seq + i, t0 + i * 0.01, flag=1) for i in range(n)]
    frames.append(sensor(node, first_seq + n, t0 + n * 0.01))
    return frames


# --- the receiver ------------------------------------------------------------


async def _run_receiver(frames, caplog, until, windows=None):
    """Feed `frames` to the real receiver until `until(caplog)` holds."""

    async def handler(reader, writer):
        for frame in frames:
            await send_message(writer, frame)
        await asyncio.sleep(DEADLINE_S)

    async with serving(handler) as port:
        async with connected(port) as (reader, writer):
            stop = asyncio.Event()
            task = asyncio.create_task(
                tcp_client.receiver(reader, writer, stop, tcp_client.EventSource(), windows)
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
async def test_a_sensor_without_an_integer_node_is_skipped_not_fatal(caplog):
    frames = [
        {"type": "SENSOR", "node": "1", "seq": 0, "timestamp": 1.0, "reserved": [1, 1, 0, 0, 0]},
        {"type": "SENSOR", "seq": 1, "timestamp": 1.0},
        *flagged_gesture(2, 2, 30, 10.0),
    ]

    task = await _run_receiver(
        frames, caplog, lambda log: _ai_events(log), gesture.Windows(gesture.StubAI())
    )

    assert not task.done() or task.cancelled(), "the receiver ended on a bad SENSOR"
    assert len(_ai_events(caplog)) == 1


# --- --gesture-flag: AI gets every sample ------------------------------------

ECHO = {"type": "ECHO", "node": 1, "seq": 0, "timestamp": 1.0, "fields": [-99]}


@pytest.mark.asyncio
async def test_with_the_gesture_flag_each_flagged_gesture_makes_one_joinable_event(caplog):
    """The ECHO at the end is the sync point: once its event is logged, every SENSOR before
    it has been read, so the unflagged packets around the gesture made nothing."""
    frames = [sensor(2, seq, 10.0 + seq * 0.01) for seq in range(5)]
    frames += flagged_gesture(2, 5, 30, 10.05)  # ends at seq 35, at t=10.35
    frames += [sensor(2, seq, 10.0 + seq * 0.01) for seq in range(36, 40)]
    frames.append(ECHO)
    windows = gesture.Windows(gesture.StubAI())

    await _run_receiver(frames, caplog, lambda log: len(_ai_events(log)) == 2, windows)

    counts = latency_report.Counts()
    events = latency_report.parse_board([PREFIX + m for m in caplog.messages], "A", counts)
    assert [(e.player, e.node, e.ble_seq) for e in events][0] == ("2", 2, "35")
    assert events[0].t_laptop_rx == pytest.approx(10.35)
    assert [e.player for e in events] == ["2", "1"]  # and the ECHO's
    assert counts.triggers_without_event == counts.events_without_trigger == 0
    assert " conf=" in _ai_events(caplog)[0] and " missing=0" in _ai_events(caplog)[0]
    assert sum(m.startswith("trigger ") for m in caplog.messages) == 2


@pytest.mark.asyncio
async def test_with_the_gesture_flag_a_gesture_the_glove_rejected_makes_no_event(caplog):
    caplog.set_level(logging.INFO, logger="gesture")
    rejected = flagged_gesture(1, 0, 30, 10.0)
    rejected[-1]["reserved"][1] = gesture.REJECTED  # the glove's 2 where the flag drops
    frames = rejected + flagged_gesture(1, 31, 30, 10.31) + [ECHO]  # the second ends at seq 61
    windows = gesture.Windows(gesture.StubAI())

    await _run_receiver(frames, caplog, lambda log: len(_ai_events(log)) == 2, windows)

    counts = latency_report.Counts()
    events = latency_report.parse_board([PREFIX + m for m in caplog.messages], "A", counts)
    assert [(e.node, e.ble_seq) for e in events] == [(1, "61"), (1, "0")]  # then the ECHO's
    assert "no AI event: node 1's glove rejected its flagged gesture of 30 samples" in caplog.messages


@pytest.mark.asyncio
async def test_with_the_gesture_flag_a_failed_gesture_is_skipped_and_the_next_works(caplog):
    class FailsOnce(gesture.StubAI):
        calls = 0

        def on_sample(self, player, record):
            if not record["end_of_gesture"]:
                return None
            self.calls += 1
            if self.calls == 1:
                raise gesture.ClassifierError("DMA still busy after 50 ms")
            return "5", 0.9

    frames = flagged_gesture(1, 0, 30, 10.0) + flagged_gesture(1, 31, 30, 10.31)
    windows = gesture.Windows(FailsOnce())

    task = await _run_receiver(frames, caplog, lambda log: _ai_events(log), windows)

    assert not task.done() or task.cancelled(), "a failed gesture ended the receiver"
    assert "no AI event: classifier failed on node 1 (DMA still busy after 50 ms)" in caplog.messages
    assert len(_ai_events(caplog)) == 1 and "gesture=5" in _ai_events(caplog)[0]


@pytest.mark.asyncio
async def test_configuration_a_session_sends_a_flagged_gesture():
    received = []

    async def laptop(reader, writer):
        for frame in flagged_gesture(2, 0, 30, 10.0):
            await send_message(writer, frame)
        while True:
            msg = await recv_message(reader)
            if msg.get("type") == "AI_EVENT":
                received.append(msg)
                writer.close()
                return

    async with serving(laptop) as port:
        await asyncio.wait_for(
            tcp_client.one_session("127.0.0.1", port, gesture_flag=True), timeout=DEADLINE_S
        )

    assert [e["player"] for e in received] == [2]
    assert received[0]["gesture"] in ai_events.GESTURES


@pytest.mark.asyncio
async def test_configuration_b_session_queues_a_flagged_gesture():
    outbound = asyncio.Queue(maxsize=8)
    events = tcp_client_mqtt.MqttEventSource(outbound, mqtt_publisher.PublishStats())
    done = asyncio.Event()

    async def laptop(reader, writer):
        for frame in flagged_gesture(1, 0, 30, 10.0):
            await send_message(writer, frame)
        await done.wait()
        writer.close()

    async with serving(laptop) as port:
        session = asyncio.create_task(
            tcp_client_mqtt.one_session("127.0.0.1", port, events, gesture_flag=True)
        )
        try:
            await wait_until(lambda: not outbound.empty())
        finally:
            done.set()
            await asyncio.wait_for(session, timeout=DEADLINE_S)

    topic, envelope = outbound.get_nowait()[:2]
    assert topic.endswith("1")
    assert envelope["payload"]["gesture"] in ai_events.GESTURES


@pytest.mark.asyncio
async def test_configuration_b_session_sends_the_laptop_a_vibrate_for_a_flagged_gesture():
    events = tcp_client_mqtt.MqttEventSource(
        asyncio.Queue(maxsize=8), mqtt_publisher.PublishStats()
    )
    received = []

    async def laptop(reader, writer):
        for frame in flagged_gesture(2, 0, 30, 10.0):
            await send_message(writer, frame)
        while True:
            msg = await recv_message(reader)
            if msg.get("type") != "HEARTBEAT":
                received.append(msg)
                writer.close()
                return

    async with serving(laptop) as port:
        await asyncio.wait_for(
            tcp_client_mqtt.one_session("127.0.0.1", port, events, gesture_flag=True),
            timeout=DEADLINE_S,
        )

    assert received == [{"type": "VIBRATE", "node": 2, "pattern": 1}]


@pytest.mark.asyncio
async def test_with_the_gesture_flag_a_reconnect_starts_every_gesture_again(caplog):
    """Session 1 sends half a gesture; session 2's unflagged packet must not end it."""
    sessions = []

    async def laptop(reader, writer):
        sessions.append(writer)
        if len(sessions) == 1:
            for seq in range(30):
                await send_message(writer, sensor(2, seq, 10.0 + seq * 0.01, flag=1))
        else:
            await send_message(writer, sensor(2, 30, 10.30))  # seq and time carry straight on
            await send_message(writer, ECHO)
            while (await recv_message(reader)).get("type") != "AI_EVENT":
                pass
        writer.close()

    classifier = gesture.StubAI()  # one for the whole run, as run() does
    with caplog.at_level(logging.INFO, logger="tcp_client"):
        async with serving(laptop) as port:
            for _ in range(2):
                await asyncio.wait_for(
                    tcp_client.one_session("127.0.0.1", port, 0.0, classifier, gesture_flag=True),
                    timeout=DEADLINE_S,
                )

    assert [m.split(" player=")[1][:1] for m in _ai_events(caplog)] == ["1"]  # the ECHO's only


@pytest.mark.asyncio
async def test_both_configurations_emit_the_classifiers_answer_alike(caplog):
    """Same event fields as a dummy, and the same new log fields right after t_event."""

    class Writer:
        def write(self, data):
            pass

        async def drain(self):
            pass

    result = gesture.Result("4", 0.75, 0.8125, 2)
    source_b = tcp_client_mqtt.MqttEventSource(
        asyncio.Queue(maxsize=8), mqtt_publisher.PublishStats()
    )
    with caplog.at_level(logging.INFO, logger="tcp_client"), caplog.at_level(
        logging.INFO, logger="tcp_client_mqtt"
    ):
        event_a = await tcp_client.EventSource().emit(Writer(), 1, None, result)
        event_b = await source_b.emit(Writer(), 1, None, result)

    assert event_a["gesture"] == event_b["gesture"] == "4"
    assert set(event_a) == set(event_b) == set(ai_events.random_event(1, 0))
    after_t_event = [m.split(" t_event=")[1].split(" ", 1)[1] for m in _ai_events(caplog)]
    fields = "conf=0.750 t_infer_ms=0.812 missing=2"
    assert after_t_event[0] == fields
    assert after_t_event[1].startswith(fields + " -> ")
    events = latency_report.parse_board(
        [PREFIX + m for m in caplog.messages], "B", latency_report.Counts()
    )
    assert len(events) == 2


@pytest.mark.parametrize(
    "module, argv",
    [
        (tcp_client, ["tcp_client.py"]),
        (tcp_client_mqtt, ["tcp_client_mqtt.py"]),
    ],
)
def test_the_flag_reaches_run_and_defaults_off(module, argv, monkeypatch):
    seen = []

    async def fake_run(*args, gesture_flag=False, **kwargs):
        seen.append((args[-1], gesture_flag))  # event_every is the last positional

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "setup_logging", lambda *a: "log")
    for extra in ([], ["--gesture-flag"]):
        monkeypatch.setattr(sys, "argv", argv + extra)
        module.main()

    assert seen == [(0.0, False), (0.0, True)]


@pytest.mark.parametrize(
    "module, retired",
    [
        (tcp_client, ["--sensor-trigger", "100"]),
        (tcp_client_mqtt, ["--sensor-trigger", "100"]),
        (tcp_client_mqtt, ["--resample"]),
    ],
)
def test_a_retired_flag_is_refused(module, retired, monkeypatch):
    """An old command line must fail, not start a board that runs differently."""

    async def fake_run(*args, **kwargs):
        raise AssertionError(f"ran with {retired[0]}")

    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module, "setup_logging", lambda *a: "log")
    monkeypatch.setattr(sys, "argv", ["x.py", *retired])
    with pytest.raises(SystemExit):
        module.main()


@pytest.mark.asyncio
@pytest.mark.parametrize("gesture_flag", [False, True])
async def test_configuration_a_run_passes_the_flag_to_every_session(gesture_flag, monkeypatch):
    seen = []

    async def failing_session(host, port, event_every=0.0, classifier=None, gesture_flag=False):
        seen.append((gesture_flag, classifier))
        raise OSError("refused")

    monkeypatch.setattr(tcp_client, "one_session", failing_session)
    monkeypatch.setattr(tcp_client, "RECONNECT_BACKOFF_S", 0.01)
    task = asyncio.create_task(tcp_client.run("127.0.0.1", 1, 0.0, gesture_flag=gesture_flag))
    try:
        await wait_until(lambda: len(seen) >= 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    # One classifier, loaded once before the first dial, for every session.
    assert {flag for flag, _ in seen} == {gesture_flag}
    assert len({id(classifier) for _, classifier in seen}) == 1
    # off the board the flag gets the stub, and no flag gets no model at all
    assert type(seen[0][1]) is (gesture.StubAI if gesture_flag else type(None))


@pytest.mark.asyncio
@pytest.mark.parametrize("gesture_flag", [False, True])
async def test_configuration_b_run_passes_the_flag_to_every_session(gesture_flag, monkeypatch):
    seen = []

    async def failing_session(
        host, port, events, event_every=0.0, classifier=None, gesture_flag=False
    ):
        seen.append((gesture_flag, classifier))
        raise OSError("refused")

    async def idle_publisher(host, port, outbound, stop, stats):
        await stop.wait()

    monkeypatch.setattr(tcp_client_mqtt, "one_session", failing_session)
    monkeypatch.setattr(tcp_client_mqtt, "RECONNECT_BACKOFF_S", 0.01)
    monkeypatch.setattr(mqtt_publisher, "run_publisher", idle_publisher)
    task = asyncio.create_task(
        tcp_client_mqtt.run("127.0.0.1", 1, "127.0.0.1", 1883, 0.0, gesture_flag=gesture_flag)
    )
    try:
        await wait_until(lambda: len(seen) >= 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert {flag for flag, _ in seen} == {gesture_flag}
    assert len({id(classifier) for _, classifier in seen}) == 1
    assert type(seen[0][1]) is (gesture.StubAI if gesture_flag else type(None))
