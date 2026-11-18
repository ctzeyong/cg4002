"""TCP session tests: the server's per-client handling and the client's session
lifecycle. Wire format itself is covered in test_framing.py, and the forwarders
that join this leg to BLE and MQTT are covered in test_pipeline.py.

Socket helpers live in helpers.py, and the reason they are real sockets rather
than mocks is explained there.
"""

import asyncio
import contextlib
import functools
import struct

import pytest

import ai_events
import tcp_client
import tcp_server
from framing import MAX_MESSAGE_BYTES, recv_message, send_message
from helpers import DEADLINE_S, connected, frame, serving, wait_until


# --- helpers ---------------------------------------------------------------


def receiving(downlink):
    """A handler that puts everything the client sends onto `downlink`."""
    return functools.partial(tcp_server.handle_client, downlink=downlink)


async def _silent(reader, writer):
    """Accept, read forever, never reply.

    Models the half-open tunnel seen in the 2026-09-07 soak: the socket is open
    and writes succeed, but nothing ever comes back.
    """
    with contextlib.suppress(OSError):
        while await reader.read(1024):
            pass


def live_session_tasks():
    """Sender/receiver tasks currently alive, by coroutine name.

    Narrower than all_tasks(): the server's own handler tasks come and go
    independently, so a raw count would be measuring the fixture, not the
    session teardown under test.
    """
    return [
        task
        for task in asyncio.all_tasks()
        if getattr(task.get_coro(), "__qualname__", "") in ("sender", "receiver")
    ]


# --- server: normal traffic ------------------------------------------------


@pytest.mark.asyncio
async def test_server_acks_heartbeats_without_forwarding_them():
    """The heartbeat is answered here and goes no further.

    Both halves matter. The ACK is what tcp_client.receiver counts as liveness,
    so without it the tunnel would flap whenever BLE is quiet. And forwarding it
    would publish a heartbeat to the visualiser once a second.
    """
    downlink = asyncio.Queue(maxsize=8)
    async with serving(receiving(downlink)) as port:
        async with connected(port) as (reader, writer):
            await send_message(writer, {"type": "HEARTBEAT", "seq": 7})
            reply = await asyncio.wait_for(recv_message(reader), DEADLINE_S)
            assert (reply["type"], reply["seq"]) == ("HEARTBEAT_ACK", 7)

            # Ordering is the proof: this arrives after the heartbeat, so if the
            # heartbeat had been forwarded it would be at the head of the queue.
            await send_message(writer, {"type": "AI_EVENT", "player": 1})
            assert await asyncio.wait_for(downlink.get(), DEADLINE_S) == {
                "type": "AI_EVENT",
                "player": 1,
            }
            assert downlink.empty()


@pytest.mark.asyncio
async def test_receiver_excludes_heartbeat_acks_from_the_reported_rate(monkeypatch, caplog):
    """`rate` must count uplink traffic, not the laptop's own replies.

    The bug this pins: `count += 1` fired on every frame, so the ~1/s
    HEARTBEAT_ACK was folded into the measurement. On 2026-09-13 that made the
    receiver report `total=197,507` against the 196,872 packets BLE actually
    delivered — a receiver claiming more messages than were ever sent. Reported
    figures quote this number, so it has to mean one thing.

    Acks only, so the correct count is zero. This also pins the decision not to
    `continue` past them: the report still fires once a second while they
    arrive, which is what distinguishes "link alive, data stopped" from a dead
    process. With the bug it reported a steady ~1 msg/s of pure self-traffic.
    """
    monkeypatch.setattr(tcp_client, "PROGRESS_INTERVAL_S", 0.01)
    done = asyncio.Event()

    async def sends_only_acks(reader, writer):
        # Until the test has its reports, not a fixed three: on Windows
        # time.monotonic() ticks every 15.6 ms and asyncio fires a timer up to one
        # tick early, so a 20 ms sleep can return in under a millisecond. Two acks
        # then share a tick, the receiver sees elapsed=0 and skips that report.
        seq = 0
        while not done.is_set():
            await send_message(writer, {"type": "HEARTBEAT_ACK", "seq": seq})
            seq += 1
            await asyncio.sleep(0.02)

    async with serving(sends_only_acks) as port:
        async with connected(port) as (reader, writer):
            stop = asyncio.Event()
            events = tcp_client.EventSource()
            task = asyncio.create_task(tcp_client.receiver(reader, writer, stop, events))
            try:
                with caplog.at_level("INFO", logger="tcp_client"):
                    await wait_until(lambda: caplog.text.count("uplink rx:") >= 2)
            finally:
                done.set()
                stop.set()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    reports = [r for r in caplog.messages if "uplink rx:" in r]
    # Acks only, so the count must never leave zero. With the bug every report
    # reads total>=1, since the ack that triggers a report is counted first.
    assert reports
    assert all("total=0" in r for r in reports), reports


@pytest.mark.asyncio
async def test_server_reassembles_a_fragmented_frame():
    """A frame split across two TCP segments must still be handled as one.

    test_framing.py proves recv_message reassembles; this proves the server's
    receive loop actually gets that benefit rather than reading a fixed size.
    """
    raw = frame({"type": "AI_EVENT", "n": 1})
    downlink = asyncio.Queue(maxsize=8)
    async with serving(receiving(downlink)) as port:
        async with connected(port) as (_reader, writer):
            writer.write(raw[:3])  # splits the 4-byte length prefix itself
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.write(raw[3:])
            await writer.drain()
            forwarded = await asyncio.wait_for(downlink.get(), DEADLINE_S)
            assert forwarded == {"type": "AI_EVENT", "n": 1}


@pytest.mark.asyncio
async def test_server_sheds_oldest_when_the_downlink_backs_up():
    """A stalled MQTT bridge must not stall the Ultra96 session.

    Drop-oldest rather than drop-newest, for the same reason as
    mqtt_client.enqueue_event: the visualiser wants current game state, not the
    state from whenever the backlog started.
    """
    downlink = asyncio.Queue(maxsize=2)
    async with serving(receiving(downlink)) as port:
        async with connected(port) as (reader, writer):
            for n in range(4):
                await send_message(writer, {"type": "AI_EVENT", "n": n})
            # The ACK is the barrier: one connection, one handler, strictly in
            # order, so receiving it proves all four events were handled. Waiting
            # on qsize instead would pass the moment the queue first hit 2, which
            # is after the *second* event, and read [0, 1].
            await send_message(writer, {"type": "HEARTBEAT", "seq": 0})
            await asyncio.wait_for(recv_message(reader), DEADLINE_S)
            assert [downlink.get_nowait()["n"], downlink.get_nowait()["n"]] == [2, 3]


# --- server: slow-send instrumentation -------------------------------------


@pytest.mark.asyncio
async def test_a_slow_uplink_send_is_reported_with_the_buffer_depth(monkeypatch, caplog):
    """Backpressure must be visible, and must say how much is buffered.

    This is the measurement that separates "the tunnel cannot absorb the rate"
    from "the laptop's event loop is busy" — the 2026-09-12 soak shed 12733
    packets and the log could not tell which. A slow send against a *full*
    buffer is the peer; a slow send against an empty one is us.
    """
    monkeypatch.setattr(tcp_server, "SLOW_SEND_MS", 5.0)
    monkeypatch.setattr(tcp_server, "_slow_last_report", 0.0)

    real_send = tcp_server.send_message

    async def slow_send(writer, obj):
        await asyncio.sleep(0.02)  # 20 ms, well over the patched threshold
        await real_send(writer, obj)

    monkeypatch.setattr(tcp_server, "send_message", slow_send)

    downlink = asyncio.Queue(maxsize=8)
    async with serving(receiving(downlink)) as port:
        async with connected(port) as (reader, writer):
            await send_message(writer, {"type": "HEARTBEAT", "seq": 0})
            await asyncio.wait_for(recv_message(reader), DEADLINE_S)

            with caplog.at_level("WARNING", logger="tcp_server"):
                assert await tcp_server.send_to_current({"type": "SENSOR"}) is True

    slow = [r for r in caplog.records if "slow TCP uplink send" in r.message]
    assert len(slow) == 1, "a send over the threshold must be reported"
    # A real reading, not the -1 "transport would not say" sentinel: on healthy
    # loopback the buffer drains immediately, so 0 is the answer. Asserting only
    # that the word "buffered" appears would pass even if the number were never
    # actually read, which is the half of the diagnosis that matters.
    assert "buffered=0" in slow[0].getMessage(), slow[0].getMessage()


@pytest.mark.asyncio
async def test_write_buffer_size_falls_back_when_the_transport_wont_say():
    """The buffer reading must never be what breaks a send path.

    It is diagnostics riding inside the hot loop, so an exotic transport that
    does not implement it has to degrade to a sentinel rather than raise.
    """

    class Mute:
        def get_write_buffer_size(self):
            raise NotImplementedError

    class FakeWriter:
        transport = Mute()

    assert tcp_server._write_buffer_size(FakeWriter()) == -1


@pytest.mark.asyncio
async def test_slow_send_warnings_are_summarised_not_one_per_packet(monkeypatch, caplog):
    """At 200 sends/s a sustained stall would emit 200 warnings a second, and the
    logging would become part of what it is measuring.

    The shape is: the first slow send warns immediately, so the onset of a stall
    is visible at once; everything after it accumulates into the next summary.
    Nothing is lost, and the log stays readable at 200 msg/s.
    """
    monkeypatch.setattr(tcp_server, "SLOW_SEND_MS", 0.0)  # every send counts as slow
    monkeypatch.setattr(tcp_server, "SLOW_SEND_REPORT_S", 60.0)  # suppress the next report
    monkeypatch.setattr(tcp_server, "_slow_last_report", 0.0)
    monkeypatch.setattr(tcp_server, "_slow_sends", 0)

    downlink = asyncio.Queue(maxsize=8)
    async with serving(receiving(downlink)) as port:
        async with connected(port) as (reader, writer):
            await send_message(writer, {"type": "HEARTBEAT", "seq": 0})
            await asyncio.wait_for(recv_message(reader), DEADLINE_S)

            with caplog.at_level("WARNING", logger="tcp_server"):
                for _ in range(10):
                    await tcp_server.send_to_current({"type": "SENSOR"})

                slow = [r for r in caplog.records if "slow TCP uplink send" in r.message]
                assert len(slow) == 1, f"10 slow sends gave {len(slow)} lines, want 1"
                assert "1 in the last" in slow[0].getMessage()  # the immediate one

                # Let the window expire: the 9 that were suppressed must survive
                # into the next summary rather than being silently discarded.
                monkeypatch.setattr(tcp_server, "SLOW_SEND_REPORT_S", 0.0)
                await tcp_server.send_to_current({"type": "SENSOR"})

    slow = [r for r in caplog.records if "slow TCP uplink send" in r.message]
    assert len(slow) == 2
    assert "10 in the last" in slow[1].getMessage(), slow[1].getMessage()


@pytest.mark.asyncio
async def test_the_board_link_line_counts_heartbeats_apart_from_forwarded(caplog):
    """The periodic line counts heartbeats separately from forwarded frames."""
    downlink = asyncio.Queue(maxsize=8)
    with caplog.at_level("INFO", logger="tcp_server"):
        async with serving(receiving(downlink)) as port:
            async with connected(port) as (_reader, writer):
                for seq in range(tcp_server.PROGRESS_EVERY - 1):
                    await send_message(writer, {"type": "HEARTBEAT", "seq": seq})
                await send_message(writer, {"type": "AI_EVENT", "player": 1})
                await wait_until(
                    lambda: any("board link:" in r.getMessage() for r in caplog.records)
                )

    line = next(r.getMessage() for r in caplog.records if "board link:" in r.getMessage())
    every = tcp_server.PROGRESS_EVERY
    expected = f"{every} frames received (heartbeats={every - 1} forwarded=1 dropped=0)"
    assert expected in line, line
    assert "127.0.0.1" not in line, "the tunnel's random port says nothing about the board"


# --- the keystroke -> gesture channel --------------------------------------


def echo_message(key, node=1, seq=0):
    """An ECHO as the board returns it: every CMD field negated, seq preserved."""
    fields = [-(ord(key))] + [-(i * 7) for i in range(1, 12)]
    return {"type": "ECHO", "node": node, "seq": seq, "timestamp": 0.0, "fields": fields}


def test_parse_echo_undoes_the_boards_negation():
    """The board sends -ord(key); recovering it is what picks the gesture."""
    assert tcp_client.parse_echo(echo_message("c", node=2)) == (2, "c")


def test_parse_echo_survives_a_board_that_did_not_negate():
    """abs() means the channel works either way rather than silently mis-mapping."""
    message = echo_message("t")
    message["fields"][0] = ord("t")

    assert tcp_client.parse_echo(message) == (1, "t")


_MISSING = object()


@pytest.mark.parametrize(
    "field, value",
    [
        ("fields", _MISSING),
        ("fields", []),
        ("fields", "not-a-list"),
        ("fields", [None]),
        ("fields", ["99"]),  # text, even though it looks like a number
        ("fields", [True]),  # bool is an int subclass
        ("fields", [-99.0]),
        ("node", _MISSING),
        ("node", "1"),
        ("node", True),
    ],
    ids=[
        "fields-missing", "fields-empty", "fields-not-a-list", "null", "string",
        "bool", "float", "node-missing", "node-string", "node-bool",
    ],
)
def test_parse_echo_rejects_a_malformed_echo(field, value):
    """Each is rejected with a reason, not turned into a random gesture."""
    message = echo_message("c")
    if value is _MISSING:
        del message[field]
    else:
        message[field] = value

    with pytest.raises(ValueError):
        tcp_client.parse_echo(message)


@pytest.mark.parametrize("code", [0, 0x110000 + 1], ids=["zero", "out-of-range"])
def test_parse_echo_accepts_a_well_formed_echo_with_no_readable_key(code):
    """A dummy-data change can zero field 0; the packet is still valid."""
    message = echo_message("c", node=2)
    message["fields"][0] = code

    assert tcp_client.parse_echo(message) == (2, None)


def test_a_mapped_key_always_picks_its_gesture():
    """Repeated: the fallback is random, so one pass agrees by luck 1 time in 7."""
    for _ in range(20):
        assert ai_events.random_event(1, 0, "l")["gesture"] == "lightning"


def test_an_unmapped_key_falls_back_to_a_random_gesture():
    """Every key still produces an event; only the gesture stops being chosen."""
    gestures = {ai_events.random_event(1, 0, "1")["gesture"] for _ in range(200)}

    assert gestures <= set(ai_events.GESTURES)
    assert len(gestures) > 1, "fallback stopped being random"


@pytest.mark.asyncio
async def test_an_echo_produces_the_gesture_matching_the_key_pressed():
    """Press `w`, see `warrior`: the whole Ultra96 half, negation to wire."""
    received = asyncio.Queue()

    async def echoes_then_listens(reader, writer):
        await send_message(writer, echo_message("w"))
        await received.put(await recv_message(reader))
        await asyncio.sleep(DEADLINE_S)  # hold the connection open

    async with serving(echoes_then_listens) as port:
        async with connected(port) as (reader, writer):
            stop = asyncio.Event()
            task = asyncio.create_task(
                tcp_client.receiver(reader, writer, stop, tcp_client.EventSource())
            )
            try:
                event = await asyncio.wait_for(received.get(), DEADLINE_S)
            finally:
                stop.set()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    assert event["type"] == "AI_EVENT"
    assert event["gesture"] == "warrior"


@pytest.mark.asyncio
async def test_an_unmapped_key_still_produces_an_event():
    """`1` and `2` select a node and map to no gesture; they must not go silent."""
    received = asyncio.Queue()

    async def echoes_then_listens(reader, writer):
        await send_message(writer, echo_message("1"))
        await received.put(await recv_message(reader))
        await asyncio.sleep(DEADLINE_S)

    async with serving(echoes_then_listens) as port:
        async with connected(port) as (reader, writer):
            stop = asyncio.Event()
            task = asyncio.create_task(
                tcp_client.receiver(reader, writer, stop, tcp_client.EventSource())
            )
            try:
                event = await asyncio.wait_for(received.get(), DEADLINE_S)
            finally:
                stop.set()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    assert event["gesture"] in ai_events.GESTURES


@pytest.mark.asyncio
async def test_an_echo_whose_key_field_was_zeroed_still_produces_an_event():
    """A zeroed field 0 must still produce an event, or the demo pipeline goes silent."""
    received = asyncio.Queue()
    zeroed = echo_message("c", node=2)
    zeroed["fields"][0] = 0

    async def echoes_then_listens(reader, writer):
        await send_message(writer, zeroed)
        await received.put(await recv_message(reader))
        await asyncio.sleep(DEADLINE_S)

    async with serving(echoes_then_listens) as port:
        async with connected(port) as (reader, writer):
            stop = asyncio.Event()
            task = asyncio.create_task(
                tcp_client.receiver(reader, writer, stop, tcp_client.EventSource())
            )
            try:
                event = await asyncio.wait_for(received.get(), DEADLINE_S)
            finally:
                stop.set()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    assert event["player"] == 2
    assert event["gesture"] in ai_events.GESTURES


@pytest.mark.asyncio
async def test_a_malformed_echo_is_dropped_and_logged_not_answered(caplog):
    """No event for the bad ECHO, and the session survives to answer the next one."""
    received = asyncio.Queue()
    bad = echo_message("c", node=1, seq=4)
    bad["fields"] = ["garbage"]

    async def echoes_then_listens(reader, writer):
        await send_message(writer, bad)
        await send_message(writer, echo_message("l", node=2, seq=5))
        while True:
            event = await recv_message(reader)
            await received.put(event)
            if event.get("player") == 2:
                break
        await asyncio.sleep(DEADLINE_S)  # hold the connection open

    with caplog.at_level("WARNING", logger="tcp_client"):
        async with serving(echoes_then_listens) as port:
            async with connected(port) as (reader, writer):
                stop = asyncio.Event()
                task = asyncio.create_task(
                    tcp_client.receiver(reader, writer, stop, tcp_client.EventSource())
                )
                events = []
                try:
                    # Different nodes, so an event made from the bad ECHO would
                    # show up as player 1.
                    while not events or events[-1]["player"] != 2:
                        events.append(await asyncio.wait_for(received.get(), DEADLINE_S))
                finally:
                    stop.set()
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    assert [(e["player"], e["seq"]) for e in events] == [(2, 0)], events
    assert events[0]["gesture"] == "lightning"
    dropped = [r.getMessage() for r in caplog.records if "malformed ECHO" in r.getMessage()]
    assert len(dropped) == 1 and "seq=4" in dropped[0], dropped


# --- server: dropping bad or idle clients ----------------------------------


@pytest.mark.asyncio
async def test_server_drops_a_silent_client(monkeypatch):
    """No frame within HEARTBEAT_TIMEOUT_S and the client is cut loose."""
    monkeypatch.setattr(tcp_server, "HEARTBEAT_TIMEOUT_S", 0.1)
    async with serving(receiving(asyncio.Queue(maxsize=8))) as port:
        async with connected(port) as (reader, _writer):
            # read() returning b"" is EOF: the server hung up on us.
            assert await asyncio.wait_for(reader.read(), DEADLINE_S) == b""


@pytest.mark.asyncio
async def test_server_drops_a_client_that_sends_garbage():
    """A well-framed but undecodable payload desynchronises the stream, so the
    only correct response is to drop the connection rather than resync."""
    body = b"\xff\xfe not json"
    async with serving(receiving(asyncio.Queue(maxsize=8))) as port:
        async with connected(port) as (reader, writer):
            writer.write(struct.pack(">I", len(body)) + body)
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), DEADLINE_S) == b""


@pytest.mark.asyncio
async def test_server_drops_a_client_claiming_an_oversized_frame():
    """The length cap must be enforced before the payload is read, so a
    corrupted header cannot make the server allocate on command."""
    async with serving(receiving(asyncio.Queue(maxsize=8))) as port:
        async with connected(port) as (reader, writer):
            writer.write(struct.pack(">I", MAX_MESSAGE_BYTES + 1))
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), DEADLINE_S) == b""


# --- server: connection bookkeeping ----------------------------------------


@pytest.mark.asyncio
async def test_new_client_supersedes_the_previous_one():
    """A reconnecting Ultra96 must evict its own stale session immediately,
    rather than the server waiting out HEARTBEAT_TIMEOUT_S first."""
    async with serving(receiving(asyncio.Queue(maxsize=8))) as port:
        async with connected(port) as (reader_a, writer_a):
            # Round-trip a heartbeat first, so A's handler has definitely claimed
            # _current before B arrives — otherwise the eviction is a race.
            await send_message(writer_a, {"type": "HEARTBEAT", "seq": 1})
            await asyncio.wait_for(recv_message(reader_a), DEADLINE_S)

            async with connected(port) as (reader_b, writer_b):
                await send_message(writer_b, {"type": "HEARTBEAT", "seq": 2})
                reply = await asyncio.wait_for(recv_message(reader_b), DEADLINE_S)
                assert (reply["type"], reply["seq"]) == ("HEARTBEAT_ACK", 2)
                # A is hung up on without having sent anything wrong.
                assert await asyncio.wait_for(reader_a.read(), DEADLINE_S) == b""


@pytest.mark.asyncio
async def test_server_releases_current_writer_on_disconnect():
    """A departing client must clear the global, or the next connection logs a
    bogus 'supersedes' against a dead peer."""
    async with serving(receiving(asyncio.Queue(maxsize=8))) as port:
        async with connected(port) as (reader, writer):
            await send_message(writer, {"type": "HEARTBEAT", "seq": 1})
            await asyncio.wait_for(recv_message(reader), DEADLINE_S)
            assert tcp_server._current is not None
        await wait_until(lambda: tcp_server._current is None)


# --- client: session lifecycle ---------------------------------------------


@pytest.mark.asyncio
async def test_session_ends_when_the_receiver_dies_without_setting_stop(monkeypatch):
    """A receiver that dies unexpectedly must still end the session.

    This is the hang the gather() version had: gather waits for *both* tasks,
    and the sender only exits when `stop` is set or a write fails. Writes keep
    succeeding on a live socket, so the sender heartbeated forever and `run`
    never got its chance to reconnect. The deadline below is the whole test.
    """

    async def exploding_receiver(reader, writer, stop, events, windows=None):
        raise RuntimeError("receiver blew up")

    monkeypatch.setattr(tcp_client, "receiver", exploding_receiver)
    monkeypatch.setattr(tcp_client, "HEARTBEAT_INTERVAL_S", 0.02)

    async with serving(_silent) as port:
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(
                tcp_client.one_session("127.0.0.1", port), timeout=DEADLINE_S
            )


@pytest.mark.asyncio
async def test_session_ends_when_the_sender_dies_without_setting_stop(monkeypatch):
    """The mirror case, which merely stalled rather than hanging outright.

    RECV_TIMEOUT_S is raised well above the deadline on purpose: if the session
    only unwinds because the receiver eventually timed out on its own, this
    fails. It must end because the sender finished.
    """

    async def exploding_sender(writer, stop):
        raise RuntimeError("sender blew up")

    monkeypatch.setattr(tcp_client, "sender", exploding_sender)
    monkeypatch.setattr(tcp_client, "RECV_TIMEOUT_S", 30.0)

    async with serving(_silent) as port:
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(
                tcp_client.one_session("127.0.0.1", port), timeout=DEADLINE_S
            )


@pytest.mark.asyncio
async def test_session_reconnects_when_the_server_goes_silent(monkeypatch):
    """The 2026-09-07 soak failure: socket open, writes fine, nothing coming
    back. Only the application-layer heartbeat catches this."""
    monkeypatch.setattr(tcp_client, "HEARTBEAT_INTERVAL_S", 0.02)
    monkeypatch.setattr(tcp_client, "RECV_TIMEOUT_S", 0.15)

    async with serving(_silent) as port:
        # Returns normally: a missed-heartbeat reconnect is expected operation,
        # not an error the supervisor should log as a failure.
        await asyncio.wait_for(tcp_client.one_session("127.0.0.1", port), DEADLINE_S)


@pytest.mark.asyncio
async def test_repeated_sessions_leave_no_orphan_tasks(monkeypatch):
    """Reconnects must not accumulate senders. Three sessions, zero survivors —
    otherwise a long soak ends up with dozens of tasks writing to dead sockets.
    """
    monkeypatch.setattr(tcp_client, "HEARTBEAT_INTERVAL_S", 0.02)
    monkeypatch.setattr(tcp_client, "RECV_TIMEOUT_S", 0.1)

    async with serving(_silent) as port:
        for _ in range(3):
            await asyncio.wait_for(tcp_client.one_session("127.0.0.1", port), DEADLINE_S)
    await asyncio.sleep(0)  # let cancelled tasks retire
    assert live_session_tasks() == []


@pytest.mark.asyncio
async def test_connecting_to_a_dead_port_raises_for_the_supervisor():
    """one_session must let the connect failure out; `run` owns the backoff.

    The generous deadline is not slack. The test host is Windows, which takes
    ~2.0 s to report WinError 1225 on a refused loopback connect where Linux
    answers instantly, so DEADLINE_S would race the refusal here even though
    the client itself deploys to the Ultra96.
    """
    async with serving(_silent) as port:
        pass  # server is now closed, so nothing is listening on `port`
    with pytest.raises(OSError):
        await asyncio.wait_for(tcp_client.one_session("127.0.0.1", port), timeout=15.0)


# --- client + server together ----------------------------------------------


@pytest.mark.asyncio
async def test_heartbeats_round_trip_against_the_real_server(monkeypatch):
    """End-to-end over a real socket: the manual loopback check, automated.

    Asserts the sequence numbers are consecutive from 0, which is what makes
    this a throughput check and not just a liveness one — a session that
    silently restarted mid-run would repeat seq 0.
    """
    monkeypatch.setattr(tcp_client, "HEARTBEAT_INTERVAL_S", 0.01)

    acked = []
    real_send = tcp_server.send_message

    async def counting_send(writer, obj):
        acked.append(obj)
        await real_send(writer, obj)

    monkeypatch.setattr(tcp_server, "send_message", counting_send)

    async with serving(receiving(asyncio.Queue(maxsize=8))) as port:
        session = asyncio.create_task(tcp_client.one_session("127.0.0.1", port))
        try:
            await wait_until(lambda: len(acked) >= 20)
        finally:
            session.cancel()
            await asyncio.gather(session, return_exceptions=True)

    first20 = acked[:20]
    assert all(msg["type"] == "HEARTBEAT_ACK" for msg in first20)
    assert [msg["seq"] for msg in first20] == list(range(20))


# --- client: the reconnect supervisor --------------------------------------


@pytest.mark.asyncio
async def test_run_retries_after_a_refused_connection(monkeypatch):
    """The Ultra96 leg has no supervisor above `run`, so it must keep dialling
    when the laptop end is not up yet — start order must not matter."""
    monkeypatch.setattr(tcp_client, "RECONNECT_BACKOFF_S", 0.01)
    attempts = []

    async def failing_session(
        host, port, event_every=0.0, classifier=None, gesture_flag=False
    ):
        attempts.append((host, port))
        raise OSError("connection refused")

    monkeypatch.setattr(tcp_client, "one_session", failing_session)

    task = asyncio.create_task(tcp_client.run("127.0.0.1", 8888))
    try:
        await wait_until(lambda: len(attempts) >= 3)
        assert not task.done()  # supervisor still alive
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_survives_an_unexpected_error(monkeypatch):
    """Nothing may escape the reconnect loop. An unhandled error here used to
    kill the process and strand the board until a human noticed."""
    monkeypatch.setattr(tcp_client, "RECONNECT_BACKOFF_S", 0.01)
    attempts = []

    async def exploding_session(
        host, port, event_every=0.0, classifier=None, gesture_flag=False
    ):
        attempts.append((host, port))
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(tcp_client, "one_session", exploding_session)

    task = asyncio.create_task(tcp_client.run("127.0.0.1", 8888))
    try:
        await wait_until(lambda: len(attempts) >= 3)
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
