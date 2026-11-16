"""Pipeline tests: the forwarders that join the three legs, and the keyboard.

Each leg is tested in its own file; this covers only the joins. The TCP half
runs against a real loopback socket for the reason given in helpers.py.
"""

import asyncio
import contextlib
import functools
import json
import signal

import pytest

import ble_client
import ble_packet
import mqtt_client
import pipeline
import tcp_server
from framing import recv_message, send_message
from helpers import DEADLINE_S, connected, serving, wait_until
from mqtt_config import UNATTRIBUTED_PLAYER, BrokerConfig
import plumbing
from plumbing import RateWindow, put_drop_oldest

FIELDS = [1, -2, 3, -4, 5, -6, 0, 0]


# --- helpers ---------------------------------------------------------------


def sensor_packet(node_id=1, packet_type=ble_packet.TYPE_ECHO, seq=42, t_recv=1234.5):
    """One BlePacket carrying a real CRC-protected payload, built the firmware's way."""
    raw = ble_packet.build_fixed_packet(packet_type, seq, FIELDS)
    _, _, payload = ble_packet.decode(raw)
    return ble_packet.BlePacket(node_id, packet_type, seq, payload, t_recv)


@contextlib.asynccontextmanager
async def running(coro, stop=None):
    """Run `coro` as a task for the block, then shut it down as the pipeline does.

    Order matters, and getting it wrong hangs rather than fails. Every forwarder
    parks in `asyncio.wait_for(queue.get(), timeout=1.0)`, and a cancel that
    lands in the window where that inner get() has *just* completed is swallowed
    by wait_for — it returns the result instead of re-raising, so the task keeps
    running and the reap below never returns.

    Setting `stop` first makes the loop exit on its own `while not stop.is_set()`
    whether or not the cancel survives. That is exactly the order run_pipeline
    and run_standalone use in their own `finally`, so this teardown exercises the
    real shutdown path rather than a test-only one.
    """
    task = asyncio.create_task(coro)
    try:
        yield task
    finally:
        if stop is not None:
            stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --- step 1: BLE -> TCP uplink ---------------------------------------------


@pytest.mark.asyncio
async def test_uplink_forwards_a_ble_packet_to_the_ultra96():
    """The join the keypress chain is built on, and the format the Ultra96 parses.

    Asserts the decoded message rather than just that something arrived:
    the wire carries readable fields, and the Ultra96 keys its
    AI event trigger off `type` and attributes it with `node`.
    """
    uplink = asyncio.Queue(maxsize=8)
    stop = asyncio.Event()
    handler = functools.partial(tcp_server.handle_client, downlink=asyncio.Queue(maxsize=8))

    async with serving(handler) as port:
        async with connected(port) as (reader, writer):
            # Round-trip a heartbeat first so the handler has claimed _current;
            # otherwise the forwarder races the accept and drops the packet.
            await send_message(writer, {"type": "HEARTBEAT", "seq": 0})
            await asyncio.wait_for(recv_message(reader), DEADLINE_S)

            async with running(pipeline.forward_uplink(uplink, stop), stop):
                uplink.put_nowait(sensor_packet())
                msg = await asyncio.wait_for(recv_message(reader), DEADLINE_S)

    assert msg == {
        "type": "ECHO",
        "node": 1,
        "seq": 42,
        "timestamp": 1234.5,
        "fields": FIELDS,
        "imu": {"ax": 1, "ay": -2, "az": 3, "gx": -4, "gy": 5, "gz": -6},
        "reserved": [0, 0],
        "crc": int.from_bytes(
            ble_packet.build_fixed_packet(ble_packet.TYPE_ECHO, 42, FIELDS)[
                ble_packet.CRC_OFFSET :
            ],
            "little",
        ),
    }


@pytest.mark.asyncio
async def test_uplink_says_once_when_it_has_no_ultra96_to_send_to(caplog):
    """An idle Ultra96 leg must announce itself, once, and not read as loss.

    From a real run on 2026-09-12: `uplink: forwarded=0 dropped=2917` next to
    `node 1: ... dropped=0` was read as catastrophic packet loss, when it only
    meant the Ultra96 client was not running. The counter is `unsent`, and the
    state change is logged edge-triggered — not per packet, at 100 Hz.
    """
    uplink = asyncio.Queue(maxsize=8)
    stop = asyncio.Event()

    with caplog.at_level("WARNING", logger="pipeline"):
        async with running(pipeline.forward_uplink(uplink, stop), stop):
            for _ in range(3):
                uplink.put_nowait(sensor_packet())
            await wait_until(lambda: uplink.empty())

    warnings = [r for r in caplog.records if "no Ultra96" in r.message]
    assert len(warnings) == 1, "should announce the idle sink once, not per packet"


@pytest.mark.asyncio
async def test_uplink_drops_packets_while_no_ultra96_is_connected():
    """A down TCP leg must not stall or kill the forwarder.

    The BLE queue upstream sheds its own oldest when it fills, so the packet is
    dropped here rather than held — but the task has to survive to forward the
    next one once the Ultra96 dials back in.
    """
    uplink = asyncio.Queue(maxsize=8)
    stop = asyncio.Event()

    async with running(pipeline.forward_uplink(uplink, stop), stop) as task:
        uplink.put_nowait(sensor_packet())
        await wait_until(lambda: uplink.empty())
        assert not task.done()


# --- step 2: TCP -> MQTT downlink ------------------------------------------


@pytest.mark.asyncio
async def test_downlink_wraps_ultra96_messages_in_a_valid_envelope():
    """The AI event must reach MQTT inside the envelope the visualiser parses.

    Validated with parse_envelope rather than by eye: that is the same check the
    receiving side applies, and it checks field *types*, not just presence.
    """
    downlink = asyncio.Queue(maxsize=8)
    outbound = asyncio.Queue(maxsize=8)
    stats = mqtt_client.MqttStats()
    stop = asyncio.Event()
    event = {"type": "AI_EVENT", "player": 1, "seq": 0, "gesture": "circle", "confidence": 0.9}

    async with running(pipeline.forward_downlink(downlink, outbound, stats, stop), stop):
        downlink.put_nowait(event)
        topic, envelope = await asyncio.wait_for(outbound.get(), DEADLINE_S)

    # Routed by the player that rode all the way from the FireBeetle's node id,
    # without the relay opening the payload to find it.
    assert topic == "game/gesture/1"
    # The Ultra96's message rides inside `payload` untouched — that is what lets
    # the AI event schema move without the visualiser moving with it.
    assert envelope["payload"] == event
    assert mqtt_client.parse_envelope(json.dumps(envelope).encode("utf-8")) == envelope


@pytest.mark.asyncio
async def test_downlink_numbers_events_consecutively():
    """event_id is "<source>:<session>:<seq>", so a repeated seq would make the
    visualiser discard the second event as a QoS 1 duplicate."""
    downlink = asyncio.Queue(maxsize=8)
    outbound = asyncio.Queue(maxsize=8)
    stats = mqtt_client.MqttStats()
    stop = asyncio.Event()

    async with running(pipeline.forward_downlink(downlink, outbound, stats, stop), stop):
        for n in range(3):
            downlink.put_nowait({"type": "AI_EVENT", "player": 2, "n": n})
        await wait_until(lambda: outbound.qsize() == 3)

    items = [outbound.get_nowait() for _ in range(3)]
    assert [envelope["seq"] for _topic, envelope in items] == [0, 1, 2]
    assert len({envelope["event_id"] for _topic, envelope in items}) == 3
    assert {topic for topic, _envelope in items} == {"game/gesture/2"}


@pytest.mark.asyncio
async def test_downlink_publishes_unattributed_rather_than_dropping():
    """An AI event with no usable player must still reach the visualiser.

    Dropping it loses real data, and guessing player 1 puts a player-2 gesture on
    player 1's screen. Publishing under an obviously-wrong id keeps it visible to
    anything on `game/gesture/+` and makes the fault legible in a log.
    """
    downlink = asyncio.Queue(maxsize=8)
    outbound = asyncio.Queue(maxsize=8)
    stats = mqtt_client.MqttStats()
    stop = asyncio.Event()

    async with running(pipeline.forward_downlink(downlink, outbound, stats, stop), stop):
        # `True` is the tricky case. bool subclasses int, so a naive isinstance check
        # would route this to `game/gesture/True`.
        downlink.put_nowait({"type": "AI_EVENT", "player": True})
        topic, envelope = await asyncio.wait_for(outbound.get(), DEADLINE_S)

    assert topic == f"game/gesture/{UNATTRIBUTED_PLAYER}"
    assert envelope["payload"]["player"] is True  # forwarded, not silently repaired


# --- step 4: the keyboard --------------------------------------------------


@pytest.mark.asyncio
async def test_a_digit_key_targets_only_that_node(monkeypatch):
    """'1' and '2' pick one board, which is how the two-node demo stays legible."""
    keys = iter(["2"])
    monkeypatch.setattr(pipeline, "read_key", lambda: next(keys, None))
    outboxes = ble_client.make_outboxes(ble_client.NODES)
    stop = asyncio.Event()

    async with running(
        pipeline.keystroke_reader(ble_client.NODES, outboxes, stop, poll_interval=0.001), stop
    ):
        await wait_until(lambda: not outboxes[2].empty())

    assert outboxes[1].empty()
    packet = outboxes[2].get_nowait()
    # The real CMD wire shape, not a test string: the board length-checks it.
    assert len(packet) == ble_packet.PACKET_LEN
    assert packet[0] == ble_packet.TYPE_CMD


@pytest.mark.asyncio
async def test_any_other_key_goes_to_every_node(monkeypatch):
    """The one-key-any-board case, for when only one FireBeetle is powered."""
    keys = iter(["x"])
    monkeypatch.setattr(pipeline, "read_key", lambda: next(keys, None))
    outboxes = ble_client.make_outboxes(ble_client.NODES)
    stop = asyncio.Event()

    async with running(
        pipeline.keystroke_reader(ble_client.NODES, outboxes, stop, poll_interval=0.001), stop
    ):
        await wait_until(lambda: not outboxes[1].empty() and not outboxes[2].empty())


@pytest.mark.asyncio
async def test_a_full_outbox_warns_only_on_the_key_that_drops_a_command(monkeypatch, caplog):
    """`post_command` returns how many older CMDs it dropped. A caller still testing its
    old True-on-success result would warn on every normal key and stay silent on a drop."""
    keys = iter(["1", "1", "1"])
    monkeypatch.setattr(pipeline, "read_key", lambda: next(keys, None))
    outboxes = {1: asyncio.Queue(maxsize=2), 2: asyncio.Queue(maxsize=2)}
    stop = asyncio.Event()

    with caplog.at_level("INFO", logger="pipeline"):
        async with running(
            pipeline.keystroke_reader(ble_client.NODES, outboxes, stop, poll_interval=0.001), stop
        ):
            await wait_until(lambda: any("seq=2 queued" in r.getMessage() for r in caplog.records))

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "seq=2" in warnings[0], warnings
    assert [outboxes[1].get_nowait()[1] for _ in range(2)] == [1, 2]  # seq byte: the newest kept


@pytest.mark.asyncio
async def test_the_pressed_key_rides_in_the_cmds_first_field(monkeypatch):
    """Pinned separately from the Ultra96 half, which can be correct while this is not."""
    keys = iter(["c"])
    monkeypatch.setattr(pipeline, "read_key", lambda: next(keys, None))
    outboxes = ble_client.make_outboxes(ble_client.NODES)
    stop = asyncio.Event()

    async with running(
        pipeline.keystroke_reader(ble_client.NODES, outboxes, stop, poll_interval=0.001), stop
    ):
        await wait_until(lambda: not outboxes[1].empty())

    _, _, payload = ble_packet.decode(outboxes[1].get_nowait())

    assert ble_packet.unpack_fields(payload)[0] == ord("c")


@pytest.mark.asyncio
async def test_only_letters_and_digits_send_a_packet(monkeypatch, caplog):
    """Other keys are logged and ignored, never sent."""
    keys = iter(["!", " ", "é", "\r", "c"])
    monkeypatch.setattr(pipeline, "read_key", lambda: next(keys, None))
    nodes = ble_client.NODES[:1]
    outboxes = ble_client.make_outboxes(nodes)
    stop = asyncio.Event()

    with caplog.at_level("INFO", logger="pipeline"):
        async with running(
            pipeline.keystroke_reader(nodes, outboxes, stop, poll_interval=0.001), stop
        ):
            await wait_until(lambda: not outboxes[1].empty())

    assert outboxes[1].qsize() == 1, "only the 'c' may have become a packet"
    _, _, payload = ble_packet.decode(outboxes[1].get_nowait())
    assert ble_packet.unpack_fields(payload)[0] == ord("c")
    ignored = [r for r in caplog.records if "ignored" in r.getMessage()]
    assert len(ignored) == 4, [r.getMessage() for r in ignored]


class FakeConsole:
    """msvcrt stand-in: kbhit/getwch over a fixed character buffer."""

    def __init__(self, chars):
        self.chars = list(chars)

    def kbhit(self):
        return bool(self.chars)

    def getwch(self):
        return self.chars.pop(0)


@pytest.mark.asyncio
async def test_an_arrow_key_is_one_ignored_press_not_a_letter(monkeypatch):
    """Up-arrow arrives as '\\xe0' 'H'; reading them apart would send an 'H' packet."""
    console = FakeConsole(["\xe0", "H", "\x00", "A", "c"])  # up-arrow, F7, then 'c'
    monkeypatch.setattr(pipeline, "msvcrt", console)
    nodes = ble_client.NODES[:1]
    outboxes = ble_client.make_outboxes(nodes)
    stop = asyncio.Event()

    async with running(
        pipeline.keystroke_reader(nodes, outboxes, stop, poll_interval=0.001), stop
    ):
        await wait_until(lambda: not console.chars and not outboxes[1].empty())

    assert outboxes[1].qsize() == 1
    _, _, payload = ble_packet.decode(outboxes[1].get_nowait())
    assert ble_packet.unpack_fields(payload)[0] == ord("c")


@pytest.mark.asyncio
async def test_a_ctrl_c_character_stops_the_pipeline(monkeypatch):
    """'\\x03' arriving as a *character* is a quit key, not a dummy packet.

    This does not describe what a real console does. It used to: the claim was
    that getwch() hands Ctrl-C over rather than processing it, and
    logs/pipeline.log disproves that — "quit key pressed" never appears in any
    recorded run. Ctrl-C arrives as SIGINT, covered by the SIGINT tests below.

    Kept because QUIT_KEYS still maps the character and the branch should stay
    correct if the console is ever put in raw mode. It is not evidence that
    Ctrl-C works.
    """
    monkeypatch.setattr(pipeline, "read_key", lambda: "\x03")
    outboxes = ble_client.make_outboxes(ble_client.NODES)
    stop = asyncio.Event()

    await asyncio.wait_for(
        pipeline.keystroke_reader(ble_client.NODES, outboxes, stop, poll_interval=0.001),
        DEADLINE_S,
    )

    assert stop.is_set()
    assert outboxes[1].empty()  # a quit key is not also a packet


@pytest.mark.asyncio
async def test_keystroke_seq_wraps_at_one_byte(monkeypatch):
    """seq shares the packet's 1-byte header field, so it must wrap, not grow.

    build_cmd_packet masks it anyway, so an unwrapped counter would not corrupt
    the wire — it would silently restart the sequence the board echoes back,
    which is worse to diagnose than a crash.
    """
    presses = 258
    keys = iter(["x"] * presses)
    monkeypatch.setattr(pipeline, "read_key", lambda: next(keys, None))
    nodes = ble_client.NODES[:1]
    # Deeper than OUTBOX_MAXSIZE on purpose: this is measuring the counter, and
    # a real outbox would start dropping long before 258 keystrokes.
    outboxes = {nodes[0].node_id: asyncio.Queue(maxsize=presses)}
    stop = asyncio.Event()

    async with running(
        pipeline.keystroke_reader(nodes, outboxes, stop, poll_interval=0.001), stop
    ):
        await wait_until(lambda: outboxes[1].qsize() == presses)

    seqs = [outboxes[1].get_nowait()[1] for _ in range(presses)]
    assert seqs[:3] == [0, 1, 2]
    assert seqs[255:] == [255, 0, 1]


@pytest.mark.asyncio
async def test_keystroke_reader_yields_between_keys(monkeypatch):
    """A key repeat or a pasted line must not hold up the rest of the loop.

    read_key never returns None here, so without an unconditional yield the
    reader spins forever and every other task — BLE, TCP, MQTT — stops running.
    The assertion is that anything else gets scheduled at all.
    """
    monkeypatch.setattr(pipeline, "read_key", lambda: "x")
    nodes = ble_client.NODES[:1]
    outboxes = ble_client.make_outboxes(nodes)
    stop = asyncio.Event()
    ticks = 0

    async def other_task():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    async with running(pipeline.keystroke_reader(nodes, outboxes, stop, poll_interval=0.0), stop):
        async with running(other_task()):
            await asyncio.wait_for(wait_until(lambda: ticks > 10), DEADLINE_S)


# --- shutdown --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_quit_key_shuts_the_whole_pipeline_down(monkeypatch):
    """Esc must end the run, not hang it.

    The bug this pins: `run_pipeline` used `asyncio.gather()`, which waits for
    *every* task. `tcp_server.run()` serves forever and has no `stop` to watch,
    so a quit key set `stop`, every other leg retired, and shutdown blocked on
    the one task that never finishes — a silent hang with no error.

    The BLE and MQTT legs are stubbed as well-behaved stop-watchers on purpose:
    that leaves the keystroke reader as the only task that finishes on its own,
    which is exactly the shape that used to hang.
    """

    async def waits_for_stop(*args, **kwargs):
        stop = next(a for a in args if isinstance(a, asyncio.Event))
        await stop.wait()

    monkeypatch.setattr(ble_client, "run_all", waits_for_stop)
    monkeypatch.setattr(mqtt_client, "run_relay", waits_for_stop)
    monkeypatch.setattr(pipeline, "read_key", lambda: "\x1b")  # Esc

    await asyncio.wait_for(
        pipeline.run_pipeline(
            ble_client.NODES, BrokerConfig("127.0.0.1", 1), "127.0.0.1", 0
        ),
        timeout=DEADLINE_S,
    )


@pytest.mark.asyncio
async def test_sigint_shuts_down_through_stop_not_a_keyboardinterrupt(monkeypatch):
    """Ctrl-C must retire the legs the same way Esc does.

    The bug this pins: nothing installed a SIGINT handler, so Ctrl-C raised
    KeyboardInterrupt inside whichever task was awaiting at the time. That skips
    run_pipeline's teardown — which sets `stop` *before* cancelling, the ordering
    that stops a wait_for(queue.get()) from swallowing the cancel — and leaves
    asyncio.run's _cancel_all_tasks doing a cancel-only teardown instead. On
    2026-09-13 that closed the loop with six tasks still pending and broke
    bleak's disconnect() with "no running event loop".

    The test asserts that run_pipeline *returns*. Before the fix this
    propagated KeyboardInterrupt out of the task that happened to catch it.
    """

    async def waits_for_stop(*args, **kwargs):
        stop = next(a for a in args if isinstance(a, asyncio.Event))
        await stop.wait()

    monkeypatch.setattr(ble_client, "run_all", waits_for_stop)
    monkeypatch.setattr(mqtt_client, "run_relay", waits_for_stop)
    monkeypatch.setattr(pipeline, "read_key", lambda: None)  # no quit key

    async def interrupt_once():
        # Let run_pipeline install its handler before the signal lands, or this
        # races the default handler and raises KeyboardInterrupt in the test.
        await wait_until(lambda: signal.getsignal(signal.SIGINT) is not original)
        signal.raise_signal(signal.SIGINT)

    original = signal.getsignal(signal.SIGINT)
    async with running(interrupt_once()):
        await asyncio.wait_for(
            pipeline.run_pipeline(
                ble_client.NODES, BrokerConfig("127.0.0.1", 1), "127.0.0.1", 0
            ),
            timeout=DEADLINE_S,
        )


@pytest.mark.asyncio
async def test_the_sigint_handler_is_restored_when_the_pipeline_ends(monkeypatch):
    """Leaving the handler installed points a later Ctrl-C at a dead loop.

    call_soon_threadsafe on a closed loop raises from inside a signal handler,
    which is close to undebuggable. The tests run several pipelines in one
    process, so this matters here as much as in a demo.
    """

    async def waits_for_stop(*args, **kwargs):
        stop = next(a for a in args if isinstance(a, asyncio.Event))
        await stop.wait()

    monkeypatch.setattr(ble_client, "run_all", waits_for_stop)
    monkeypatch.setattr(mqtt_client, "run_relay", waits_for_stop)
    monkeypatch.setattr(pipeline, "read_key", lambda: "\x1b")  # Esc

    original = signal.getsignal(signal.SIGINT)
    await asyncio.wait_for(
        pipeline.run_pipeline(
            ble_client.NODES, BrokerConfig("127.0.0.1", 1), "127.0.0.1", 0
        ),
        timeout=DEADLINE_S,
    )

    assert signal.getsignal(signal.SIGINT) is original


@pytest.mark.asyncio
async def test_a_second_interrupt_restores_the_default_handler():
    """An operator who cannot stop the process is worse off than a messy exit.

    The first Ctrl-C asks the legs to drain. If draining hangs, the next one has
    to kill the process the ordinary way rather than setting an event nobody is
    left watching — so the handler disarms itself as it fires.
    """
    stop = asyncio.Event()
    original = signal.getsignal(signal.SIGINT)
    restore = pipeline.install_interrupt_handler(stop)
    try:
        assert signal.getsignal(signal.SIGINT) is not original

        signal.raise_signal(signal.SIGINT)
        await wait_until(stop.is_set)

        assert signal.getsignal(signal.SIGINT) is original
    finally:
        restore()


@pytest.mark.asyncio
async def test_a_dying_leg_surfaces_instead_of_hanging(monkeypatch):
    """A leg that raises must take the run down with a traceback.

    Swallowing it would leave the pipeline running with one leg silently dead —
    the failure mode that is hardest to notice mid-demo.
    """

    async def explodes(*args, **kwargs):
        raise RuntimeError("BLE leg blew up")

    monkeypatch.setattr(ble_client, "run_all", explodes)
    monkeypatch.setattr(mqtt_client, "run_relay", explodes)
    monkeypatch.setattr(pipeline, "read_key", lambda: None)

    with pytest.raises(RuntimeError, match="blew up"):
        await asyncio.wait_for(
            pipeline.run_pipeline(
                ble_client.NODES, BrokerConfig("127.0.0.1", 1), "127.0.0.1", 0
            ),
            timeout=DEADLINE_S,
        )


# --- shared plumbing -------------------------------------------------------


@pytest.mark.asyncio
async def test_gatt_cancel_filter_swallows_a_set_result_on_a_cancelled_future():
    """The exact shape bleak produced at shutdown, reproduced without bleak.

    `bleak_winrt` is a compiled bridge, so the `set_result` call itself cannot be
    reached from a test. It does not need to be: what reaches the loop is an
    ordinary `call_soon(fut.set_result, value)` against a future that has since
    been cancelled, and that is reproducible in three lines. If this stops
    matching, the filter has stopped matching the real thing too.
    """
    loop = asyncio.get_running_loop()
    seen = []
    loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx))

    restore = plumbing.install_gatt_cancel_error_filter()
    try:
        fut = loop.create_future()
        fut.cancel()
        loop.call_soon(fut.set_result, "GattCommunicationStatus.SUCCESS")
        await asyncio.sleep(0)  # let the callback run and raise
    finally:
        restore()

    assert seen == [], seen


@pytest.mark.asyncio
async def test_gatt_cancel_filter_swallows_a_set_exception_on_a_cancelled_future():
    """The mid-run shape from 28 Sep: a board dropped while a CMD write was in flight."""
    loop = asyncio.get_running_loop()
    seen = []
    loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx))

    restore = plumbing.install_gatt_cancel_error_filter()
    try:
        fut = loop.create_future()
        fut.cancel()
        # The error Windows reported for the cancelled write (ERROR_CANCELLED).
        error = OSError(22, "The operation was canceled by the user.", None, -2147023673)
        loop.call_soon(fut.set_exception, error)
        await asyncio.sleep(0)  # let the callback run and raise
    finally:
        restore()

    assert seen == [], seen


@pytest.mark.asyncio
async def test_the_gatt_filter_is_still_installed_when_teardown_raises_it(monkeypatch):
    """Where the filter is installed matters as much as what it matches.

    The real errors arrive *during* run_pipeline's teardown gather, as the BLE
    legs are cancelled out of stop_notify and disconnect. Restore the handler a
    line too early and the filter is provably correct while doing nothing at all
    — which the two unit tests above would not catch, because they exercise the
    filter directly rather than through the pipeline.

    So this stands in for bleak: a leg that, on its way out, schedules exactly
    the callback the compiled bridge schedules.
    """
    loop = asyncio.get_running_loop()
    seen = []
    loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx))

    async def waits_for_stop(*args, **kwargs):
        stop = next(a for a in args if isinstance(a, asyncio.Event))
        await stop.wait()

    async def completes_a_gatt_op_after_being_cancelled(*args, **kwargs):
        # Deliberately does NOT watch `stop`. An earlier version of this awaited
        # it, which made the test vacuous: the leg then exited *normally* the
        # moment Esc set `stop`, so its finally ran during the FIRST_COMPLETED
        # wait — before run_pipeline's teardown had started — and the callback
        # was always scheduled while the filter was installed no matter where the
        # restore went. Real bleak is cancelled mid-stop_notify, so the stand-in
        # has to park until it is cancelled too.
        try:
            await asyncio.Event().wait()  # never set; only the cancel ends this
        finally:
            # What bleak_winrt does: the WinRT operation finishes even though the
            # future waiting on it has been cancelled.
            fut = loop.create_future()
            fut.cancel()
            loop.call_soon(fut.set_result, "GattCommunicationStatus.SUCCESS")

    monkeypatch.setattr(ble_client, "run_all", completes_a_gatt_op_after_being_cancelled)
    monkeypatch.setattr(mqtt_client, "run_relay", waits_for_stop)
    monkeypatch.setattr(pipeline, "read_key", lambda: "\x1b")  # Esc

    await asyncio.wait_for(
        pipeline.run_pipeline(
            ble_client.NODES, BrokerConfig("127.0.0.1", 1), "127.0.0.1", 0
        ),
        timeout=DEADLINE_S,
    )
    await asyncio.sleep(0)  # let any callback still queued actually run

    assert seen == [], seen


@pytest.mark.asyncio
async def test_gatt_cancel_filter_still_reports_everything_else():
    """A filter that hides real failures is worse than the traceback it removes.

    Two probes: an unrelated exception, and an InvalidStateError that did *not*
    come from a set_result callback. Both must reach the previous handler, and
    the previous handler must be the one that was installed before — restoring
    it is what lets the tests run more than one pipeline in a process.
    """
    loop = asyncio.get_running_loop()
    seen = []
    original = lambda _loop, ctx: seen.append(ctx)  # noqa: E731
    loop.set_exception_handler(original)

    restore = plumbing.install_gatt_cancel_error_filter()
    try:
        loop.call_soon(lambda: 1 / 0)
        await asyncio.sleep(0)
        loop.call_exception_handler(
            {"message": "something else", "exception": asyncio.InvalidStateError("nope")}
        )
    finally:
        restore()

    assert len(seen) == 2, seen
    assert isinstance(seen[0]["exception"], ZeroDivisionError)
    assert isinstance(seen[1]["exception"], asyncio.InvalidStateError)
    assert loop.get_exception_handler() is original


def test_rate_window_does_not_count_its_own_anchor():
    """Elapsed must span exactly as many intervals as there are counted packets.

    Counting the anchor reports a rate one packet too high on every window; at
    20 Hz that is a 5% error that looks like jitter rather than a bug.
    """
    window = RateWindow(interval=1.0)
    assert window.tick(100.0, 26) is None  # anchor
    assert window.tick(100.5, 26) is None  # inside the window
    rate, kbps = window.tick(101.0, 26)
    assert rate == pytest.approx(2.0)
    assert kbps == pytest.approx(2 * 26 * 8 / 1000)


def test_rate_window_reanchors_on_the_reporting_packet():
    """The packet that closed a window must not also count towards the next."""
    window = RateWindow(interval=1.0)
    window.tick(100.0)
    assert window.tick(101.0) is not None
    assert window.tick(101.5) is None
    rate, _ = window.tick(102.0)
    assert rate == pytest.approx(2.0)


def test_rate_window_reset_forgets_the_anchor():
    """A BLE reconnect must not measure its first window across the outage."""
    window = RateWindow(interval=1.0)
    window.tick(100.0)
    window.reset()
    assert window.tick(500.0) is None  # re-anchored, not reported as one packet in 400 s
    assert window.tick(501.0) is not None


@pytest.mark.asyncio
async def test_put_drop_oldest_sheds_until_the_item_fits():
    """Shared by every drop-oldest queue in the relay, so it is tested once here."""
    queue = asyncio.Queue(maxsize=2)
    assert put_drop_oldest(queue, "a") == 0
    assert put_drop_oldest(queue, "b") == 0
    assert put_drop_oldest(queue, "c") == 1
    assert [queue.get_nowait(), queue.get_nowait()] == ["b", "c"]


def test_queue_get_warning_filter_is_narrow():
    """The filter hides the shutdown race, not un-awaited coroutines at large."""
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        plumbing.suppress_queue_get_shutdown_warning()

        warnings.warn("coroutine 'Queue.get' was never awaited", RuntimeWarning)
        warnings.warn("coroutine 'forward_uplink' was never awaited", RuntimeWarning)
        warnings.warn("something else entirely", RuntimeWarning)

    messages = [str(w.message) for w in caught]
    assert "coroutine 'Queue.get' was never awaited" not in messages
    assert "coroutine 'forward_uplink' was never awaited" in messages
    assert "something else entirely" in messages
