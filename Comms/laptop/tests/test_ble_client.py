"""Connection, queue and bookkeeping tests for the two-way BLE client.

No radio involved. Packet encoding lives in test_ble_packet.py; this file covers
the notification handler and supervised bleak sessions around that codec.
"""

import asyncio
import itertools

import pytest
from bleak.exc import BleakError

import ble_client as ble_module
from ble_client import (
    NODES,
    NodeStats,
    drain_outbox,
    enqueue,
    key_cmd_fields,
    make_notify_handler,
    post_command,
    random_cmd_fields,
    setup_connection,
)
from ble_packet import (
    CMD_KIND_FIELD,
    CMD_VIBRATE,
    CRC_OFFSET,
    DATA_FIELD_COUNT,
    IMU_FIELD_COUNT,
    IMU_MISSING,
    IMU_RECOVERING,
    RESERVED_FIELD_COUNT,
    TYPE_ECHO,
    TYPE_SENSOR,
    VIBRATE_RECOGNISED,
    BlePacket,
    build_cmd_packet,
    build_fixed_packet,
    calculate_crc,
    unpack_fields,
)
from logging_setup import LINK_COLOURS, NODE_COLOURS, ConsoleColourFormatter, _sgr


def make_packet(packet_type, seq, fields=None):
    """Build a PACKET_LEN packet of `packet_type`, as the firmware would put it on air."""
    fields = list(range(DATA_FIELD_COUNT)) if fields is None else fields
    if packet_type in (TYPE_SENSOR, TYPE_ECHO):
        return build_fixed_packet(packet_type, seq, fields)

    # Unknown types are rejected by the notification handler before their
    # payload is interpreted, so give them an otherwise realistic wire shape.
    packet = bytearray(build_fixed_packet(TYPE_SENSOR, seq, fields))
    packet[0] = packet_type
    packet[CRC_OFFSET:] = calculate_crc(packet[:CRC_OFFSET]).to_bytes(2, "little")
    return bytes(packet)


def test_random_cmd_fields_stay_negatable():
    """Range must exclude -32768, which has no positive counterpart in int16."""
    for _ in range(200):
        fields = random_cmd_fields()
        assert len(fields) == DATA_FIELD_COUNT
        assert all(-32768 < value < 32768 for value in fields[:IMU_FIELD_COUNT])
        assert fields[IMU_FIELD_COUNT:] == [0] * RESERVED_FIELD_COUNT


def test_key_cmd_fields_carry_the_key_in_field_zero():
    """Field 0 is the channel the gesture rides home on; the rest stay dummy data."""
    fields = key_cmd_fields("c")

    assert len(fields) == DATA_FIELD_COUNT
    assert fields[0] == ord("c")
    assert fields[IMU_FIELD_COUNT:] == [0] * RESERVED_FIELD_COUNT


def test_key_cmd_fields_stay_negatable_for_every_printable_key():
    """An overflowing key would raise out of keystroke_reader and retire the task."""
    for code in range(0x20, 0x7F):
        fields = key_cmd_fields(chr(code))
        assert all(-32768 < value < 32768 for value in fields)
        # Round-trips exactly: the mask must not perturb anything printable.
        assert fields[0] == code

    # And nothing in the BMP overflows either, mask or no mask.
    for code in (0x3000, 0x7FFF, 0x8000, 0xFFFF):
        assert -32768 < key_cmd_fields(chr(code))[0] < 32768


# --- notification handler bookkeeping --------------------------------------


@pytest.mark.asyncio
async def test_echo_does_not_disturb_seq_tracking():
    """An ECHO's seq comes from the downlink sequence space, not the sensor stream.

    Regression guard: fold ECHO into the gap detector and every keystroke invents
    one huge jump out to the CMD seq and another back, so a working link reports
    hundreds of missed packets.
    """
    queue = asyncio.Queue(maxsize=100)
    stats = NodeStats()
    handler = make_notify_handler(NODES[0], queue, stats)

    handler(None, make_packet(TYPE_SENSOR, 10))
    handler(None, make_packet(TYPE_SENSOR, 11))
    handler(None, make_packet(TYPE_ECHO, 200))
    handler(None, make_packet(TYPE_SENSOR, 12))

    assert stats.missed == 0
    assert stats.echoed == 1
    assert stats.received == 4
    assert stats.last_seq == 12
    assert queue.qsize() == 4  # the ECHO is forwarded like any other uplink packet


@pytest.mark.asyncio
async def test_a_vibrates_echo_is_not_forwarded_to_the_board():
    """If forwarded, the board would treat it as a keypress and publish a fake gesture."""
    queue = asyncio.Queue(maxsize=100)
    stats = NodeStats()
    handler = make_notify_handler(NODES[0], queue, stats)
    vibrate = [-VIBRATE_RECOGNISED] + [0] * (DATA_FIELD_COUNT - 1)  # as the firmware echoes it
    vibrate[CMD_KIND_FIELD] = CMD_VIBRATE
    keystroke = [-ord("1")] + [0] * (DATA_FIELD_COUNT - 1)

    handler(None, make_packet(TYPE_ECHO, 0, vibrate))
    handler(None, make_packet(TYPE_ECHO, 0, keystroke))

    assert stats.echoed == 2
    assert queue.qsize() == 1
    assert unpack_fields(queue.get_nowait().payload)[0] == -ord("1")


@pytest.mark.asyncio
async def test_each_board_prints_in_its_own_green(monkeypatch, caplog):
    """Guideline 3: a colour per connection. Driven through the real handler, so a
    line that loses its board tag fails here, and a board's counter and event lines
    must share its one colour."""
    clock = itertools.count(1000.0, 1.0)  # a second per packet: every one closes the window
    monkeypatch.setattr(ble_module.time, "perf_counter", lambda: next(clock))
    fmt = ConsoleColourFormatter(enabled=True)
    seen = {}

    for node in NODES:
        stats = NodeStats()
        handler = make_notify_handler(node, asyncio.Queue(maxsize=100), stats)
        caplog.clear()
        with caplog.at_level("INFO", logger="ble_client"):
            handler(None, make_packet(TYPE_SENSOR, 0))
            handler(None, make_packet(TYPE_SENSOR, 1))  # the 1 Hz counter line
            handler(None, make_packet(TYPE_ECHO, 7))  # a per-event line

        counter = next(r for r in caplog.records if "rate=" in r.getMessage())
        event = next(r for r in caplog.records if "ECHO" in r.getMessage())
        seen[node.node_id] = (fmt.colour_for(counter), fmt.colour_for(event))

    assert seen[1] == tuple(_sgr(i) for i in NODE_COLOURS[1])
    assert seen[2] == tuple(_sgr(i) for i in NODE_COLOURS[2])
    assert seen[1][0] == seen[1][1] and seen[2][0] == seen[2][1]  # one shade per board
    assert seen[1] != seen[2]
    # Lines about both boards, like drain's totals, carry no tag: they get the third green.
    shared = _sgr(LINK_COLOURS["ble_client"][0])
    assert shared not in seen[1] + seen[2]


@pytest.mark.asyncio
async def test_seq_gap_still_counted():
    queue = asyncio.Queue(maxsize=100)
    stats = NodeStats()
    handler = make_notify_handler(NODES[0], queue, stats)

    handler(None, make_packet(TYPE_SENSOR, 10))
    handler(None, make_packet(TYPE_SENSOR, 14))

    assert stats.missed == 3


@pytest.mark.asyncio
async def test_seq_gap_storm_is_summarised_not_one_warning_per_lost_packet(monkeypatch, caplog):
    """The first loss warns at once; the rest fold into the next summary, and
    `missed` stays exact. notes/logging.md."""
    monkeypatch.setattr(ble_module, "LOSS_REPORT_S", 60.0)  # suppress all but the first
    queue = asyncio.Queue(maxsize=100)
    stats = NodeStats()
    handler = make_notify_handler(NODES[0], queue, stats)

    with caplog.at_level("WARNING", logger="ble_client"):
        handler(None, make_packet(TYPE_SENSOR, 0))  # anchors last_seq; not a gap
        for seq in range(2, 22, 2):  # 10 packets, each one lost packet behind it
            handler(None, make_packet(TYPE_SENSOR, seq))

        gaps = [r for r in caplog.records if "seq gap" in r.message]
        assert len(gaps) == 1, f"10 gaps gave {len(gaps)} warnings, want 1"
        assert "more gaps" not in gaps[0].getMessage()  # the immediate one hid nothing

        # Let the window expire: the 9 suppressed gaps must survive into the next
        # warning rather than being silently forgotten.
        monkeypatch.setattr(ble_module, "LOSS_REPORT_S", 0.0)
        handler(None, make_packet(TYPE_SENSOR, 24))

    gaps = [r for r in caplog.records if "seq gap" in r.message]
    assert len(gaps) == 2
    assert "9 more gaps" in gaps[1].getMessage(), gaps[1].getMessage()
    assert stats.missed == 13  # 10 single losses, then 3 more across 21..23


@pytest.mark.asyncio
async def test_gaps_and_duplicates_share_one_quiet_window(monkeypatch, caplog):
    """Two timers would let a link alternating between them warn twice as often."""
    monkeypatch.setattr(ble_module, "LOSS_REPORT_S", 60.0)
    queue = asyncio.Queue(maxsize=100)
    stats = NodeStats()
    handler = make_notify_handler(NODES[0], queue, stats)

    with caplog.at_level("WARNING", logger="ble_client"):
        handler(None, make_packet(TYPE_SENSOR, 10))
        handler(None, make_packet(TYPE_SENSOR, 12))  # gap: warns immediately
        handler(None, make_packet(TYPE_SENSOR, 12))  # duplicate: suppressed
        handler(None, make_packet(TYPE_SENSOR, 14))  # gap: suppressed

        assert len([r for r in caplog.records if "seq" in r.message]) == 1

        monkeypatch.setattr(ble_module, "LOSS_REPORT_S", 0.0)
        handler(None, make_packet(TYPE_SENSOR, 14))  # duplicate: due, reports both

    anomalies = [r for r in caplog.records if "seq" in r.message]
    assert len(anomalies) == 2
    summary = anomalies[1].getMessage()
    assert "1 more gaps" in summary and "1 more duplicates" in summary, summary
    assert stats.missed == 2 and stats.duplicates == 2


@pytest.mark.asyncio
async def test_a_new_session_may_warn_immediately(monkeypatch):
    """A session ending inside the window must not silence the next one."""
    stats = NodeStats()
    stats.loss_reported_at = 12345.0
    stats.suppressed_gaps = 7
    stats.suppressed_duplicates = 2
    stats.prev_seq = 7  # a rebooted board restarts at seq 0, so 7 must not survive

    async def no_device(_node):
        return None

    monkeypatch.setattr(ble_module, "find_device", no_device)
    # A scan miss is enough: the reset runs before the link is attempted.
    linked = await ble_module.one_session(
        NODES[0], asyncio.Queue(), asyncio.Queue(), stats, asyncio.Event()
    )

    assert linked is False
    assert stats.loss_reported_at == 0.0
    assert stats.suppressed_gaps == 0
    assert stats.suppressed_duplicates == 0
    assert stats.prev_seq is None


@pytest.mark.asyncio
async def test_duplicate_seq_is_not_counted_as_loss():
    """Replay of the 2026-09-09 22:23 failure: sensor 3, sensor 3 again, sensor 4.

    The board re-sent a packet instead of an echo. Counting the backwards step as
    (3 - 4) mod 256 turned each duplicate into 255 phantom losses — the real log
    reported missed=510 out of 656 packets received, a 78% loss rate on a link that
    lost nothing.
    """
    queue = asyncio.Queue(maxsize=100)
    stats = NodeStats()
    handler = make_notify_handler(NODES[0], queue, stats)

    handler(None, make_packet(TYPE_SENSOR, 3))
    handler(None, make_packet(TYPE_SENSOR, 3))
    handler(None, make_packet(TYPE_SENSOR, 4))

    assert stats.missed == 0
    assert stats.duplicates == 1
    assert stats.received == 3


def queued_seqs(queue):
    """Every seq on `queue`, in order, emptying it."""
    seqs = []
    while not queue.empty():
        seqs.append(queue.get_nowait().seq)
    return seqs


@pytest.mark.asyncio
async def test_an_exact_repeat_is_counted_but_not_forwarded():
    """3, 3, 4: the second 3 repeats the previous packet, so it is withheld.

    Forwarded, it would put the same sample into the Ultra96's gesture window twice.
    """
    queue = asyncio.Queue(maxsize=100)
    stats = NodeStats()
    handler = make_notify_handler(NODES[0], queue, stats)

    for seq in (3, 3, 4):
        handler(None, make_packet(TYPE_SENSOR, seq))

    assert queued_seqs(queue) == [3, 4]
    assert stats.duplicates == 1
    assert stats.repeats == 1
    assert stats.received == 3  # still counted as arrived; only forwarding is skipped


@pytest.mark.asyncio
async def test_a_reordered_packet_is_still_forwarded():
    """3, 5, 4, 6: the late 4 is not a repeat of the previous seq (5), so it goes on."""
    queue = asyncio.Queue(maxsize=100)
    stats = NodeStats()
    handler = make_notify_handler(NODES[0], queue, stats)

    for seq in (3, 5, 4, 6):
        handler(None, make_packet(TYPE_SENSOR, seq))

    assert queued_seqs(queue) == [3, 5, 4, 6]
    assert stats.duplicates == 1
    assert stats.repeats == 0


@pytest.mark.asyncio
async def test_valid_packets_after_a_128_packet_loss_are_still_forwarded():
    """Losing exactly 128 packets makes the next ones read as backwards.

    They are real, new samples, so dropping the whole backwards class would throw
    away up to 128 of them (1.28 s at 100 Hz). Only an exact repeat is withheld.
    """
    queue = asyncio.Queue(maxsize=300)
    stats = NodeStats()
    handler = make_notify_handler(NODES[0], queue, stats)

    handler(None, make_packet(TYPE_SENSOR, 0))
    after_loss = [(129 + i) & 0xFF for i in range(128)]  # 1..128 lost
    for seq in after_loss:
        handler(None, make_packet(TYPE_SENSOR, seq))

    # The last of them, seq 0 again after the wrap, equals the stale last_seq: a
    # rule keyed on last_seq would drop it. It is new data and must go on.
    assert queued_seqs(queue) == [0, *after_loss]
    assert stats.repeats == 0

    # A genuine repeat inside that stretch is still caught.
    handler(None, make_packet(TYPE_SENSOR, 1))
    handler(None, make_packet(TYPE_SENSOR, 1))
    assert queued_seqs(queue) == [1]
    assert stats.repeats == 1


@pytest.mark.asyncio
async def test_reordered_packet_does_not_double_count():
    """3, 5, 4, 6 — one real loss, one reorder, and last_seq must not rewind.

    Rewinding last_seq to the late arrival would make the next in-order packet look
    like a second gap, so one reorder would be charged twice.
    """
    queue = asyncio.Queue(maxsize=100)
    stats = NodeStats()
    handler = make_notify_handler(NODES[0], queue, stats)

    for seq in (3, 5, 4, 6):
        handler(None, make_packet(TYPE_SENSOR, seq))

    assert stats.missed == 1  # only seq 4's original transmission
    assert stats.duplicates == 1
    assert stats.last_seq == 6


@pytest.mark.asyncio
async def test_large_forward_gap_still_counted_as_loss():
    """A gap just under the threshold is loss, not a reorder."""
    queue = asyncio.Queue(maxsize=100)
    stats = NodeStats()
    handler = make_notify_handler(NODES[0], queue, stats)

    handler(None, make_packet(TYPE_SENSOR, 0))
    handler(None, make_packet(TYPE_SENSOR, 100))

    assert stats.missed == 99
    assert stats.duplicates == 0


@pytest.mark.asyncio
async def test_seq_wrap_is_not_a_gap():
    queue = asyncio.Queue(maxsize=100)
    stats = NodeStats()
    handler = make_notify_handler(NODES[0], queue, stats)

    handler(None, make_packet(TYPE_SENSOR, 255))
    handler(None, make_packet(TYPE_SENSOR, 0))

    assert stats.missed == 0


@pytest.mark.asyncio
async def test_truncated_packet_counted_not_raised():
    """A bad packet must be counted and dropped — bleak swallows exceptions here."""
    queue = asyncio.Queue(maxsize=100)
    stats = NodeStats()
    handler = make_notify_handler(NODES[0], queue, stats)

    handler(None, make_packet(TYPE_SENSOR, 1)[:20])

    assert stats.bad_len == 1
    assert stats.received == 0
    assert queue.empty()


@pytest.mark.asyncio
async def test_bad_crc_counted_separately_and_dropped():
    queue = asyncio.Queue(maxsize=100)
    stats = NodeStats()
    handler = make_notify_handler(NODES[0], queue, stats)
    packet = bytearray(make_packet(TYPE_SENSOR, 1))
    packet[5] ^= 0x01

    handler(None, packet)

    assert stats.bad_crc == 1
    assert stats.bad_len == 0
    assert stats.received == 0
    assert queue.empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("packet_type", [0x09, 0x02], ids=["unknown", "retired EVENT"])
async def test_unknown_type_counted_every_time_but_logged_once(packet_type):
    queue = asyncio.Queue(maxsize=100)
    stats = NodeStats()
    handler = make_notify_handler(NODES[0], queue, stats)

    handler(None, make_packet(packet_type, 1))
    handler(None, make_packet(packet_type, 2))

    assert stats.bad_type == 2
    assert stats.unknown_types == {packet_type}
    assert queue.empty()


@pytest.mark.asyncio
async def test_cmd_type_is_not_accepted_as_uplink():
    """The board must never notify a CMD back; if it does, that is a firmware bug."""
    queue = asyncio.Queue(maxsize=100)
    stats = NodeStats()
    handler = make_notify_handler(NODES[0], queue, stats)

    handler(None, build_cmd_packet(1, [0] * DATA_FIELD_COUNT))

    assert stats.bad_type == 1
    assert queue.empty()


# --- queue policies --------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_sheds_oldest_when_full():
    """Uplink keeps the freshest samples — stale IMU readings are worthless."""
    queue = asyncio.Queue(maxsize=2)
    stats = NodeStats()
    for seq in range(3):
        enqueue(queue, BlePacket(1, TYPE_SENSOR, seq, b"", 0.0), stats)

    assert stats.dropped == 1
    assert queue.get_nowait().seq == 1
    assert queue.get_nowait().seq == 2


@pytest.mark.asyncio
async def test_post_command_drops_oldest_when_full():
    """The latest keypress must get through. Dropping the newest instead lost the player's
    last action for good, even though the link was about to catch up."""
    outbox = asyncio.Queue(maxsize=2)
    packets = [build_cmd_packet(seq, [0] * DATA_FIELD_COUNT) for seq in range(3)]

    assert post_command(outbox, packets[0]) == 0
    assert post_command(outbox, packets[1]) == 0
    assert post_command(outbox, packets[2]) == 1

    assert outbox.get_nowait() == packets[1]
    assert outbox.get_nowait() == packets[2]
    assert outbox.empty()


@pytest.mark.asyncio
async def test_cmd_ticker_warns_only_when_a_command_is_dropped(caplog):
    """The standalone ticker's warning follows `post_command`'s drop count: nothing drains
    these outboxes, so the first warning must be the third CMD's, never the first's."""
    nodes = NODES[:1]
    outboxes = {nodes[0].node_id: asyncio.Queue(maxsize=2)}
    stop = asyncio.Event()

    with caplog.at_level("WARNING", logger="ble_client"):
        task = asyncio.create_task(ble_module.cmd_ticker(nodes, outboxes, stop, interval=0.001))
        try:
            assert await settle(lambda: any(r.levelname == "WARNING" for r in caplog.records))
        finally:
            stop.set()
            await task

    first = next(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "seq=2" in first, first


# --- writer task -----------------------------------------------------------


class FakeClient:
    """BleakClient stand-in recording writes, optionally failing the first N."""

    def __init__(self, fail_times=0):
        self.writes = []
        self.fail_times = fail_times

    async def write_gatt_char(self, char_uuid, data, response=True):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise BleakError("simulated write failure")
        self.writes.append((char_uuid, bytes(data), response))


async def settle(predicate, timeout=1.0):
    """Let the writer task run until `predicate` holds. Returns False on timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


async def run_writer(client, outbox, stats, predicate):
    """Run drain_outbox until `predicate` holds, then cancel it as a session end would."""
    task = asyncio.create_task(drain_outbox(client, NODES[0], outbox, stats))
    try:
        return await settle(predicate)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_the_cmd_line_is_logged_before_its_own_echo(caplog):
    """Hop 2 of the criterion-5 chain must print before hop 3.

    The board answers a CMD while `write_gatt_char` is still awaiting its ack, so a
    line logged on completion came out *after* the ECHO its own write caused. Seen on
    every keypress of the 2026-09-22 configuration-B run, 0-6 ms apart.
    """
    outbox = asyncio.Queue(maxsize=4)
    stats = NodeStats()
    queue = asyncio.Queue(maxsize=4)
    notify = make_notify_handler(NODES[0], queue, stats)

    class EchoingClient:
        """A board that notifies its ECHO before the write's ack returns."""

        async def write_gatt_char(self, _uuid, packet, response=False):
            notify(None, make_packet(TYPE_ECHO, packet[1]))
            await asyncio.sleep(0)  # the ack lands a scheduler turn later

    post_command(outbox, build_cmd_packet(7, [1] * DATA_FIELD_COUNT))

    with caplog.at_level("INFO", logger="ble_client"):
        assert await run_writer(
            EchoingClient(), outbox, stats, lambda: stats.sent == 1
        ), "the writer never completed its write"

    order = [r.getMessage() for r in caplog.records if "seq=7" in r.getMessage()]
    assert len(order) == 2, order
    assert "CMD" in order[0] and "ECHO" in order[1], order


@pytest.mark.asyncio
async def test_drain_outbox_writes_to_the_cmd_characteristic():
    """Downlink must land on the CMD characteristic, not the SENSOR one."""
    outbox = asyncio.Queue(maxsize=4)
    stats = NodeStats()
    client = FakeClient()
    packet = build_cmd_packet(3, [1] * DATA_FIELD_COUNT)
    post_command(outbox, packet)

    assert await run_writer(client, outbox, stats, lambda: stats.sent == 1)
    assert client.writes == [(NODES[0].cmd_char_uuid, packet, True)]


@pytest.mark.asyncio
async def test_drain_outbox_preserves_order():
    outbox = asyncio.Queue(maxsize=8)
    stats = NodeStats()
    client = FakeClient()
    packets = [build_cmd_packet(seq, [seq] * DATA_FIELD_COUNT) for seq in range(4)]
    for packet in packets:
        post_command(outbox, packet)

    assert await run_writer(client, outbox, stats, lambda: stats.sent == 4)
    assert [written for _, written, _ in client.writes] == packets


@pytest.mark.asyncio
async def test_drain_outbox_survives_a_failed_write():
    """A failed write must not kill the writer.

    If it did, one transient error would silently end the downlink for the rest of
    the session while the uplink kept streaming — the link would look healthy.
    """
    outbox = asyncio.Queue(maxsize=4)
    stats = NodeStats()
    client = FakeClient(fail_times=1)
    for seq in range(2):
        post_command(outbox, build_cmd_packet(seq, [0] * DATA_FIELD_COUNT))

    assert await run_writer(client, outbox, stats, lambda: stats.sent == 1)
    assert stats.cmd_failed == 1
    assert len(client.writes) == 1  # the second packet, after the first was dropped


@pytest.mark.asyncio
async def test_a_cmd_in_flight_when_the_session_ends_is_counted(caplog):
    """A write cut off by a disconnect must land in `cmd_failed`, not vanish.

    The packet has already left the outbox, so a reconnect's stale-discard can't count it
    either. Seen 2026-09-28 08:26: node 2 was addressed 11 CMDs and `sent=7 stale=3`
    accounted for 10.
    """
    outbox = asyncio.Queue(maxsize=4)
    stats = NodeStats()
    in_flight = asyncio.Event()

    class DeadLinkClient:
        """A write the board never acknowledges. Parks on something never set, so it can
        only end by cancellation, as it does when `one_session` tears the writer down."""

        async def write_gatt_char(self, _uuid, _packet, response=True):
            in_flight.set()
            await asyncio.Event().wait()

    for seq in range(2):
        post_command(outbox, build_cmd_packet(seq, [0] * DATA_FIELD_COUNT))

    with caplog.at_level("WARNING", logger="ble_client"):
        assert await run_writer(DeadLinkClient(), outbox, stats, in_flight.is_set)

    assert stats.sent == 0
    assert stats.cmd_failed == 1
    # Every CMD is in exactly one place: counted, or still queued for the stale-discard.
    assert stats.sent + stats.cmd_failed + outbox.qsize() == 2
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("seq=0" in m for m in warnings), caplog.text


@pytest.mark.asyncio
async def test_a_reconnect_discards_commands_queued_while_the_link_was_down(
    monkeypatch, caplog
):
    """Keys pressed during an outage must not replay as a burst when the node returns."""

    class SessionClient(FakeClient):
        async def stop_notify(self, _uuid):
            pass

        async def disconnect(self):
            pass

    client = SessionClient()

    async def fake_setup_connection(*_args):
        return client

    monkeypatch.setattr(ble_module, "setup_connection", fake_setup_connection)
    outbox = asyncio.Queue(maxsize=8)
    stats = NodeStats()
    stop = asyncio.Event()
    for seq in range(3):  # pressed while the node was unreachable
        post_command(outbox, build_cmd_packet(seq, [0] * DATA_FIELD_COUNT))

    with caplog.at_level("WARNING", logger="ble_client"):
        session = asyncio.create_task(
            ble_module.one_session(NODES[0], asyncio.Queue(), outbox, stats, stop)
        )
        try:
            assert await settle(outbox.empty)
            # A key pressed after reconnecting must still be sent, so this test
            # cannot pass by sending nothing at all.
            fresh = build_cmd_packet(9, [0] * DATA_FIELD_COUNT)
            post_command(outbox, fresh)
            assert await settle(lambda: stats.sent >= 1 and client.writes[-1][1] == fresh)
        finally:
            stop.set()
            await session

    assert [written for _, written, _ in client.writes] == [fresh]
    assert stats.stale == 3
    assert any("discarded 3 CMD(s)" in r.getMessage() for r in caplog.records)


# --- connection setup ------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_setup_is_serialised(monkeypatch):
    """A second WinRT setup must not begin before the first has subscribed."""
    events = []

    async def fake_find_device(node):
        events.append((node.node_id, "find"))
        await asyncio.sleep(0)
        return node

    class SetupClient:
        def __init__(self, device, disconnected_callback):
            self.node = device
            self.mtu_size = 128

        async def connect(self):
            events.append((self.node.node_id, "connect"))
            await asyncio.sleep(0)

        async def start_notify(self, _uuid, _handler):
            events.append((self.node.node_id, "subscribe"))
            await asyncio.sleep(0)

        async def disconnect(self):
            pass

    monkeypatch.setattr(ble_module, "find_device", fake_find_device)
    monkeypatch.setattr(ble_module, "BleakClient", SetupClient)

    lock = asyncio.Lock()
    queue = asyncio.Queue()
    clients = await asyncio.gather(
        setup_connection(NODES[0], queue, NodeStats(), lambda _client: None, lock),
        setup_connection(NODES[1], queue, NodeStats(), lambda _client: None, lock),
    )

    assert events == [
        (1, "find"),
        (1, "connect"),
        (1, "subscribe"),
        (2, "find"),
        (2, "connect"),
        (2, "subscribe"),
    ]
    await asyncio.gather(
        *(ble_module.disconnect_client(client, node) for client, node in zip(clients, NODES))
    )


@pytest.mark.asyncio
async def test_run_all_shares_one_setup_gate_between_nodes(monkeypatch):
    """run_all is the composition point which must supply the shared lock."""
    locks = []

    async def fake_run_node(_node, _queue, _outbox, _stop, setup_lock=None):
        locks.append(setup_lock)

    monkeypatch.setattr(ble_module, "run_node", fake_run_node)
    outboxes = {node.node_id: asyncio.Queue() for node in NODES}

    await ble_module.run_all(NODES, asyncio.Queue(), outboxes, asyncio.Event())

    assert len(locks) == 2
    assert locks[0] is locks[1]


@pytest.mark.asyncio
async def test_connection_setup_timeout_disconnects_and_retries_cleanly(monkeypatch):
    """A WinRT connect stuck after discovery must become a supervised failure."""
    disconnected = asyncio.Event()

    async def fake_find_device(node):
        return node

    class HungClient:
        def __init__(self, _device, disconnected_callback):
            pass

        async def connect(self):
            await asyncio.Event().wait()

        async def disconnect(self):
            disconnected.set()

    monkeypatch.setattr(ble_module, "find_device", fake_find_device)
    monkeypatch.setattr(ble_module, "BleakClient", HungClient)
    monkeypatch.setattr(ble_module, "SETUP_STEP_TIMEOUT_S", 0.01)

    with pytest.raises(BleakError, match="connect timed out after 0s"):
        await setup_connection(
            NODES[0], asyncio.Queue(), NodeStats(), lambda _client: None, asyncio.Lock()
        )

    assert disconnected.is_set()


# --- retry pacing ----------------------------------------------------------


async def _collect_backoffs(monkeypatch, outcomes):
    """Run scripted sessions without sleeping and return their retry delays."""
    waits = []
    remaining = list(outcomes)

    async def fake_one_session(*_args, **_kwargs):
        if not remaining:
            stop.set()
            return False
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def fake_wait_for(awaitable, timeout):
        # run_node only ever wait_fors the stop event here
        awaitable.close()
        waits.append(timeout)
        raise asyncio.TimeoutError

    stop = asyncio.Event()
    monkeypatch.setattr(ble_module, "one_session", fake_one_session)
    monkeypatch.setattr(ble_module.asyncio, "wait_for", fake_wait_for)
    await ble_module.run_node(NODES[0], asyncio.Queue(), asyncio.Queue(), stop)
    return waits


@pytest.mark.asyncio
async def test_backoff_does_not_escalate_before_the_first_connect(monkeypatch):
    """The first startup failures stay at the minimum retry delay."""
    waits = await _collect_backoffs(
        monkeypatch, [False] * ble_module.STARTUP_FAST_RETRIES
    )

    assert waits == [ble_module.MIN_BACKOFF_S] * ble_module.STARTUP_FAST_RETRIES


@pytest.mark.asyncio
async def test_backoff_escalates_once_the_eager_retries_are_used_up(monkeypatch):
    """A board that really is off must still go quiet rather than scan forever."""
    waits = await _collect_backoffs(
        monkeypatch, [False] * (ble_module.STARTUP_FAST_RETRIES + 2)
    )

    assert waits == [
        *[ble_module.MIN_BACKOFF_S] * ble_module.STARTUP_FAST_RETRIES,
        ble_module.MIN_BACKOFF_S * 2,
        ble_module.MIN_BACKOFF_S * 4,
    ]


@pytest.mark.asyncio
async def test_backoff_escalates_immediately_after_a_link_has_existed(monkeypatch):
    """A prior link keeps the established power-pull backoff behaviour."""
    waits = await _collect_backoffs(monkeypatch, [True, False, False])

    assert waits == [
        ble_module.MIN_BACKOFF_S,
        ble_module.MIN_BACKOFF_S * 2,
        ble_module.MIN_BACKOFF_S * 4,
    ]


# --- IMU status (reserved[0]) ------------------------------------------------


def with_status(packet_type, seq, status):
    """A packet with plausible IMU values and `status` in reserved[0]."""
    return make_packet(packet_type, seq, [120, -45, 8192, 3, -2, 1, status, 0, 0, 0, 0])


def feed(handler, statuses, first_seq=0):
    """Send one SENSOR packet per status, with consecutive seqs."""
    for i, status in enumerate(statuses):
        handler(None, with_status(TYPE_SENSOR, (first_seq + i) & 0xFF, status))


def imu_lines(caplog):
    return [(r.levelname, r.getMessage()) for r in caplog.records if "IMU" in r.getMessage()]


@pytest.mark.asyncio
async def test_old_firmware_and_a_healthy_imu_say_nothing(caplog):
    """Status 0 is firmware from before the IMU, so it must stay quiet like status 1."""
    handler = make_notify_handler(NODES[0], asyncio.Queue(maxsize=1000), NodeStats())

    with caplog.at_level("INFO", logger="ble_client"):
        feed(handler, [0] * 150 + [1] * 150)

    assert imu_lines(caplog) == []


@pytest.mark.parametrize(
    "status, warning",
    [
        (IMU_MISSING, "IMU missing, sending zeros"),
        (IMU_RECOVERING, "IMU readings restarting after a reset or a gap"),
        (7, "unknown IMU status 7"),
    ],
)
@pytest.mark.asyncio
async def test_an_imu_problem_warns_once_not_per_packet(caplog, status, warning):
    handler = make_notify_handler(NODES[0], asyncio.Queue(maxsize=1000), NodeStats())

    with caplog.at_level("INFO", logger="ble_client"):
        feed(handler, [1] * 5 + [status] * 200)

    assert imu_lines(caplog) == [("WARNING", f"node 1: {warning}")]


@pytest.mark.asyncio
async def test_the_imu_coming_back_logs_once_how_long_it_was_out(monkeypatch, caplog):
    clock = itertools.count(1000.0, 0.01)  # 100 Hz
    monkeypatch.setattr(ble_module.time, "perf_counter", lambda: next(clock))
    handler = make_notify_handler(NODES[0], asyncio.Queue(maxsize=1000), NodeStats())

    with caplog.at_level("INFO", logger="ble_client"):
        feed(handler, [1] * 10 + [IMU_MISSING] * 250 + [1] * 100)

    assert imu_lines(caplog) == [
        ("WARNING", "node 1: IMU missing, sending zeros"),
        ("INFO", "node 1: IMU OK again after 2.5s"),
    ]


@pytest.mark.asyncio
async def test_an_imu_unplug_and_replug_is_one_warning_and_one_info(caplog):
    """Missing then recovering is one problem, so a replug must not warn a second time."""
    handler = make_notify_handler(NODES[0], asyncio.Queue(maxsize=1000), NodeStats())

    with caplog.at_level("INFO", logger="ble_client"):
        feed(handler, [1] * 5 + [IMU_MISSING] * 100 + [IMU_RECOVERING] + [1] * 5)
        assert [level for level, _ in imu_lines(caplog)] == ["WARNING", "INFO"]

        feed(handler, [IMU_MISSING], first_seq=111)  # a later problem is a new one

    assert [level for level, _ in imu_lines(caplog)] == ["WARNING", "INFO", "WARNING"]


@pytest.mark.asyncio
async def test_an_echo_is_not_read_for_imu_status(caplog):
    """An ECHO's reserved fields are copied from its CMD, not set by the IMU."""
    handler = make_notify_handler(NODES[0], asyncio.Queue(maxsize=1000), NodeStats())

    with caplog.at_level("INFO", logger="ble_client"):
        handler(None, with_status(TYPE_ECHO, 7, IMU_MISSING))

    assert imu_lines(caplog) == []


@pytest.mark.asyncio
async def test_a_reconnect_starts_with_a_fresh_imu_check(caplog):
    """The board may have been fixed or swapped while the link was down, so warn again."""
    stats = NodeStats()

    with caplog.at_level("INFO", logger="ble_client"):
        feed(make_notify_handler(NODES[0], asyncio.Queue(maxsize=1000), stats), [IMU_MISSING] * 3)
        feed(make_notify_handler(NODES[0], asyncio.Queue(maxsize=1000), stats), [IMU_MISSING] * 3)

    assert [level for level, _ in imu_lines(caplog)] == ["WARNING", "WARNING"]
