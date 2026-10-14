"""Laptop BLE side: one supervised connection per FireBeetle.

Uplink: find the board by its service UUID, connect, subscribe to SENSOR, check each
notification and put it on one shared queue for the TCP forwarder.

Downlink: while connected, a writer task sends queued CMD packets to the CMD
characteristic. The board's ECHO reply comes back through the normal uplink path.

The player number is not inside the packet. It comes from which connection the
packet arrived on (NodeConfig). Design notes: notes/two_node_test_plan.md.
"""

import argparse
import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from ble_packet import (
    HEADER_LEN,
    IMU_FIELD_COUNT,
    RESERVED_FIELD_COUNT,
    TYPE_ECHO,
    TYPE_EVENT,
    TYPE_SENSOR,
    BlePacket,
    PacketCrcError,
    PacketLengthError,
    build_cmd_packet,
    decode,
    format_packet,
    unpack_fields,
)
from logging_setup import PROGRESS, setup_logging
from plumbing import (
    RateWindow,
    install_gatt_cancel_error_filter,
    put_drop_oldest,
    suppress_queue_get_shutdown_warning,
)

P1_SERVICE_UUID     = "b1160001-0000-4000-8000-000000000001"
P1_SENSOR_CHAR_UUID = "b1160002-0000-4000-8000-000000000001"
P1_CMD_CHAR_UUID    = "b1160003-0000-4000-8000-000000000001"
P2_SERVICE_UUID     = "b1160001-0000-4000-8000-000000000002"
P2_SENSOR_CHAR_UUID = "b1160002-0000-4000-8000-000000000002"
P2_CMD_CHAR_UUID    = "b1160003-0000-4000-8000-000000000002"

# Types this client accepts from the SENSOR notification characteristic.
UPLINK_TYPES = (TYPE_SENSOR, TYPE_EVENT, TYPE_ECHO)

# seq is 8 bits and wraps 255 -> 0. A jump of 128 or more "forwards" is read as the
# packet going backwards (a repeat or a late packet), not as 128+ lost packets.
SEQ_BACKWARD_THRESHOLD = 128

LOSS_REPORT_S = 1.0  # at most one loss warning per node per second

SCAN_TIMEOUT_S = 10.0
# Time limit for each setup step (connect, subscribe). Windows can hang there, and the
# limit lets the supervisor give up and retry. Long enough for first-time pairing.
SETUP_STEP_TIMEOUT_S = 15.0
DISCONNECT_TIMEOUT_S = 5.0
QUEUE_MAXSIZE  = 2000  # about 10 s of both boards at 100 Hz; see enqueue()
OUTBOX_MAXSIZE = 32  # CMDs come one per keypress, so this is plenty

# Reconnect wait: 2 s, doubling after each short session (2,4,8,16,32,60), and back
# to 2 s after a session of GOOD_RUN_S or more. Same shape as tunnel.py.
MIN_BACKOFF_S  = 2.0
MAX_BACKOFF_S  = 60.0
GOOD_RUN_S     = 60.0  # a session lasting this long resets the backoff

# Before a board has ever connected, its first three retries stay at 2 s instead of
# doubling, so a board switched on late is found quickly. notes/two_node_test_plan.md.
STARTUP_FAST_RETRIES = 3

LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "ble_client.log"

log = logging.getLogger("ble_client")


class NodeLog(logging.LoggerAdapter):
    """`log`, with each record tagged `node=<id>` so the console can colour the two
    boards apart (guideline 3). Keeps any `extra` the caller passes, such as PROGRESS."""

    def process(self, msg, kwargs):
        kwargs["extra"] = {**self.extra, **(kwargs.get("extra") or {})}
        return msg, kwargs


_node_logs = {}


def node_log(node):
    """The tagged logger for one board. Use it for every line about that board."""
    if node.node_id not in _node_logs:
        _node_logs[node.node_id] = NodeLog(log, {"node": node.node_id})
    return _node_logs[node.node_id]


@dataclass(frozen=True)
class NodeConfig:
    """Identity and GATT addresses for one FireBeetle."""

    node_id: int
    service_uuid: str
    sensor_char_uuid: str
    cmd_char_uuid: str


NODES = (
    NodeConfig(1, P1_SERVICE_UUID, P1_SENSOR_CHAR_UUID, P1_CMD_CHAR_UUID), # player 1
    NodeConfig(2, P2_SERVICE_UUID, P2_SENSOR_CHAR_UUID, P2_CMD_CHAR_UUID), # player 2
)


@dataclass
class NodeStats:
    """Counters for one board. Only that board's task updates them, so no lock is needed.

    `last_seq` resets on every reconnect, so `missed` only counts loss inside one
    connection, not packets lost while disconnected.
    """

    last_seq: Optional[int] = None  # highest SENSOR seq seen this connection
    prev_seq: Optional[int] = None  # seq of the SENSOR packet just before this one
    received: int = 0
    missed: int = 0
    duplicates: int = 0  # same seq twice, or one out of order; not loss
    repeats: int = 0  # duplicates that repeat the previous packet; these are not forwarded
    dropped: int = 0  # shed by enqueue() when the queue is full
    bad_len: int = 0
    bad_crc: int = 0
    bad_type: int = 0
    sent: int = 0  # downlink CMDs successfully written
    cmd_failed: int = 0  # downlink writes the board rejected, or that hit a dead link
    stale: int = 0  # CMDs queued while the link was down, discarded on reconnect
    echoed: int = 0  # ECHO replies received
    window: RateWindow = field(default_factory=RateWindow)  # rolling Hz/kbps report
    unknown_types: set = field(default_factory=set)  # logged once per type, not per packet
    # Used by seq_anomaly_suffix to limit loss warnings; the counters above stay exact.
    loss_reported_at: float = 0.0
    suppressed_gaps: int = 0
    suppressed_duplicates: int = 0


def random_cmd_fields():
    """Six random IMU-style int16 values, then five reserved zeros.

    Kept within ±30000 so the board's negation is always a clean sign flip
    (-32768 has no positive int16 and would come back unchanged).
    """
    imu_fields = [random.randint(-30000, 30000) for _ in range(IMU_FIELD_COUNT)]
    return imu_fields + [0] * RESERVED_FIELD_COUNT


KEY_FIELD_MASK = 0x3FFF  # keeps the key code small enough to negate; see notes/tcp_leg.md


def key_cmd_fields(key):
    """Random dummy fields, except field 0, which carries the pressed key's code.

    The Ultra96 reads it back from the ECHO to pick the gesture; see notes/tcp_leg.md.
    """
    fields = random_cmd_fields()
    fields[0] = ord(key) & KEY_FIELD_MASK
    return fields


def enqueue(queue, packet, stats):
    """Put a packet on the queue, dropping the oldest one if it is full.

    Must not block: bleak calls the notify handler as a plain function, which cannot await.
    """
    stats.dropped += put_drop_oldest(queue, packet)


def post_command(outbox, packet):
    """Queue one downlink packet for the node's writer task, dropping the oldest if full.

    The same policy as enqueue() above, so the latest keypress always gets through. Old
    keypresses are not replayed either way: the outbox only fills while the link cannot
    send, and a reconnect empties it first (discard_stale_commands).

    Returns:
        How many older commands were dropped to make room, normally 0.
    """
    return put_drop_oldest(outbox, packet)


def discard_stale_commands(outbox):
    """Empty `outbox` without sending anything, and return how many were removed."""
    discarded = 0
    while True:
        try:
            outbox.get_nowait()
        except asyncio.QueueEmpty:
            return discarded
        discarded += 1


def seq_anomaly_suffix(stats, now):
    """Rate-limit the loss warnings; see notes/logging.md.

    Returns:
        A suffix naming what was suppressed since the last warning, or None while
        inside the quiet window, meaning the caller counts it and logs nothing.
    """
    if now - stats.loss_reported_at < LOSS_REPORT_S:
        return None

    hidden = []
    if stats.suppressed_gaps:
        hidden.append(f"{stats.suppressed_gaps} more gaps")
    if stats.suppressed_duplicates:
        hidden.append(f"{stats.suppressed_duplicates} more duplicates")

    stats.loss_reported_at = now
    stats.suppressed_gaps = 0
    stats.suppressed_duplicates = 0

    if not hidden:
        return ""
    return f"; {' and '.join(hidden)} in the last {LOSS_REPORT_S:.0f}s"


def make_notify_handler(node, queue, stats):
    """Build the function bleak calls for every notification from `node`.

    It checks the packet (length, CRC, type), tracks the sequence number, and puts
    good packets on the shared queue. Bad packets are counted and dropped.
    """
    nlog = node_log(node)

    def on_notify(_char, data):
        try:
            # perf_counter, not time.time(): 0.1 us against 15.6 ms on Windows. It rides
            # to the board as the frame's `timestamp`; notes/latency.md 3.3.
            now = time.perf_counter()

            try:
                packet_type, seq, payload = decode(data)
            except PacketCrcError as exc:
                stats.bad_crc += 1
                nlog.warning("node %d: %s", node.node_id, exc)
                return
            except PacketLengthError as exc:
                stats.bad_len += 1
                nlog.warning("node %d: %s", node.node_id, exc)
                return

            if packet_type not in UPLINK_TYPES:
                stats.bad_type += 1

                if packet_type not in stats.unknown_types:
                    stats.unknown_types.add(packet_type)
                    nlog.warning(
                        "node %d: unknown packet type 0x%02x (further occurrences silent)",
                        node.node_id,
                        packet_type,
                    )
                return

            stats.received += 1
            forward = True

            if packet_type == TYPE_ECHO:
                # An ECHO reuses its CMD's seq, not the sensor stream's, so skip the gap check.
                stats.echoed += 1
                nlog.info(
                    "node %d: ECHO seq=%d fields[:3]=%s",
                    node.node_id,
                    seq,
                    unpack_fields(payload)[:3],
                )
            elif stats.last_seq is None:
                stats.last_seq = seq
            else:
                expected = (stats.last_seq + 1) & 0xFF
                delta = (seq - expected) & 0xFF

                if delta >= SEQ_BACKWARD_THRESHOLD:
                    # seq went backwards: a repeat or a late packet, not loss.
                    # last_seq keeps its highest value so the next packet is not a false gap.
                    stats.duplicates += 1
                    # Only an exact repeat of the previous packet is withheld; other
                    # backwards packets are still forwarded. notes/two_node_test_plan.md.
                    if seq == stats.prev_seq:
                        stats.repeats += 1
                        forward = False
                    suffix = seq_anomaly_suffix(stats, now)
                    if suffix is None:
                        stats.suppressed_duplicates += 1
                    else:
                        nlog.warning(
                            "node %d: duplicate/reordered seq, expected %d got %d%s",
                            node.node_id,
                            expected,
                            seq,
                            suffix,
                        )
                else:
                    if delta:
                        stats.missed += delta
                        suffix = seq_anomaly_suffix(stats, now)
                        if suffix is None:
                            stats.suppressed_gaps += 1
                        else:
                            nlog.warning(
                                "node %d: seq gap, expected %d got %d (missed %d)%s",
                                node.node_id,
                                expected,
                                seq,
                                delta,
                                suffix,
                            )
                    stats.last_seq = seq

            if packet_type != TYPE_ECHO:
                stats.prev_seq = seq

            if forward:
                enqueue(queue, BlePacket(node.node_id, packet_type, seq, payload, now), stats)

            # Counted after the checks, so Hz and kbps describe valid packets only.
            report = stats.window.tick(now, len(data))
            if report:
                rate, kbps = report
                nlog.info(
                    "node %d: n=%d seq=%d rate=%.1f Hz %.1f kbps missed=%d dup=%d "
                    "dropped=%d bad_crc=%d sent=%d echoed=%d",
                    node.node_id,
                    stats.received,
                    seq,
                    rate,
                    kbps,
                    stats.missed,
                    stats.duplicates,
                    stats.dropped,
                    stats.bad_crc,
                    stats.sent,
                    stats.echoed,
                    extra=PROGRESS,  # a 1 Hz counter
                )
        except Exception:
            nlog.exception("node %d: notify handler error", node.node_id)

    return on_notify


async def find_device(node):
    """Scan for `node` by advertised service UUID.

    Returns:
        The matching BLEDevice, or None if nothing matched within SCAN_TIMEOUT_S.
    """
    nlog = node_log(node)
    wanted = node.service_uuid.lower()
    nlog.info(
        "node %d: scanning for %s (up to %.0fs)", node.node_id, node.service_uuid, SCAN_TIMEOUT_S
    )
    device = await BleakScanner.find_device_by_filter(
        lambda _d, ad: wanted in [str(uuid).lower() for uuid in (ad.service_uuids or [])],
        timeout=SCAN_TIMEOUT_S,
    )

    if device is None:
        nlog.warning("node %d: nothing advertising that service", node.node_id)
    else:
        nlog.info("node %d: found name=%r addr=%s", node.node_id, device.name, device.address)
    return device


async def drain_outbox(client, node, outbox, stats):
    """Write queued CMD packets to this board until the session is cancelled.

    response=True makes the board acknowledge each write, so `sent` counts only
    acknowledged writes. A failed write or commands sent during disconnect is logged 
    and skipped, not retried.
    """
    nlog = node_log(node)
    while True:
        packet = await outbox.get()
        # Logged before the write so it prints before the ECHO; see notes/logging.md.
        nlog.info(
            "node %d: CMD seq=%d fields[:3]=%s",
            node.node_id,
            packet[1],
            unpack_fields(packet[HEADER_LEN:])[:3],
        )
        try:
            await client.write_gatt_char(node.cmd_char_uuid, packet, response=True)
        except (BleakError, OSError) as exc:
            stats.cmd_failed += 1
            nlog.warning("node %d: CMD write failed: %s", node.node_id, exc)
        except asyncio.CancelledError:
            stats.cmd_failed += 1
            nlog.warning(
                "node %d: CMD seq=%d not acknowledged: session ended mid-write",
                node.node_id,
                packet[1],
            )
            raise
        else:
            stats.sent += 1


async def disconnect_client(client, node):
    """Best-effort disconnect, time-bounded, never raising over the real error."""
    nlog = node_log(node)
    try:
        await asyncio.wait_for(client.disconnect(), timeout=DISCONNECT_TIMEOUT_S)
    except asyncio.TimeoutError:
        nlog.warning(
            "node %d: disconnect timed out after %.0fs", node.node_id, DISCONNECT_TIMEOUT_S
        )
    except Exception as exc:
        nlog.warning("node %d: disconnect failed: %s", node.node_id, exc)


async def setup_connection(node, queue, stats, on_disconnect, setup_lock):
    """Scan, connect and subscribe, holding the shared setup lock.

    Only setup is one board at a time; afterwards both boards stream together.
    Windows was unreliable setting up two connections at once (notes/two_node_test_plan.md).
    """
    nlog = node_log(node)
    async with setup_lock:
        device = await find_device(node)
        if device is None:
            return None

        client = BleakClient(device, disconnected_callback=on_disconnect)
        try:
            try:
                await asyncio.wait_for(client.connect(), timeout=SETUP_STEP_TIMEOUT_S)
            except asyncio.TimeoutError as exc:
                raise BleakError(
                    f"connect timed out after {SETUP_STEP_TIMEOUT_S:.0f}s"
                ) from exc
            nlog.info("node %d: connected", node.node_id)

            try:
                await asyncio.wait_for(
                    client.start_notify(
                        node.sensor_char_uuid, make_notify_handler(node, queue, stats)
                    ),
                    timeout=SETUP_STEP_TIMEOUT_S,
                )
            except asyncio.TimeoutError as exc:
                raise BleakError(
                    f"subscribe timed out after {SETUP_STEP_TIMEOUT_S:.0f}s"
                ) from exc

            # Read the MTU after subscribing; right after connect it can still show 23.
            nlog.info(
                "node %d: subscribed; mtu=%s",
                node.node_id,
                getattr(client, "mtu_size", "unknown"),
            )
            return client
        except BaseException:
            # Clean up a half-made connection so it cannot break the next attempt.
            await disconnect_client(client, node)
            raise


async def one_session(node, queue, outbox, stats, stop, setup_lock=None):
    """Hold one BLE connection until it drops or `stop` is set.

    The first connect pairs and bonds (Just Works, since the characteristics need
    encryption); later connects reuse the stored bond, even after a power cycle.
    Returns False if the board was not found, True once a connection was made.

    Raises:
        BleakError: the board was found, but connect or subscribe failed or timed out.
    """
    nlog = node_log(node)
    loop = asyncio.get_running_loop()
    disconnected = asyncio.Event()

    def on_disconnect(_client):
        # bleak may call this from another thread, so let the loop set the event.
        nlog.warning("node %d: peripheral disconnected", node.node_id)
        loop.call_soon_threadsafe(disconnected.set)

    # Reset per connection: a rebooted board restarts seq at 0, which is not a gap.
    stats.last_seq = None
    stats.prev_seq = None
    stats.window.reset()
    # A new connection may warn about loss straight away.
    stats.loss_reported_at = 0.0
    stats.suppressed_gaps = 0
    stats.suppressed_duplicates = 0

    # run_all passes one lock shared by every board; a direct caller gets its own.
    if setup_lock is None:
        setup_lock = asyncio.Lock()
    client = await setup_connection(node, queue, stats, on_disconnect, setup_lock)
    if client is None:
        return False

    try:
        # (A few packets can be lost at the start while Windows finishes subscribing.)

        # CMDs queued while disconnected are stale: drop them rather than replay them.
        stale = discard_stale_commands(outbox)
        if stale:
            stats.stale += stale
            nlog.warning(
                "node %d: discarded %d CMD(s) queued while the link was down",
                node.node_id,
                stale,
            )

        # Start the writer only now: CMD needs an encrypted link and rejects earlier writes.
        writer = asyncio.create_task(drain_outbox(client, node, outbox, stats))

        # Run until the board disconnects or the program is stopping.
        waiters = [
            asyncio.create_task(disconnected.wait()),
            asyncio.create_task(stop.wait()),
        ]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            writer.cancel()
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(writer, *waiters, return_exceptions=True)

        if not disconnected.is_set():
            try:
                await client.stop_notify(node.sensor_char_uuid)  # unsubscribe cleanly
            except BleakError:
                pass  # the board went away between the check and the call
        return True
    finally:
        await disconnect_client(client, node)


async def run_node(node, queue, outbox, stop, setup_lock=None):
    """Keep one board connected: connect, stream, wait, retry. Returns only when `stop` is set.

    A session shorter than GOOD_RUN_S doubles the wait; a longer one resets it to MIN_BACKOFF_S.
    """
    nlog = node_log(node)
    stats = NodeStats()
    backoff = MIN_BACKOFF_S
    connected_once = False
    startup_failures = 0
    if setup_lock is None:
        setup_lock = asyncio.Lock()

    # In a finally so the final counts still print when this task is cancelled.
    try:
        while not stop.is_set():
            started = time.monotonic()  # monotonic: measuring a duration, not a wall time
            try:
                if await one_session(node, queue, outbox, stats, stop, setup_lock):
                    connected_once = True
            except (BleakError, OSError) as exc:
                nlog.warning("node %d: session failed: %s", node.node_id, exc)
            except Exception:
                nlog.exception("node %d: unexpected session error", node.node_id)

            if stop.is_set():
                break

            elapsed = time.monotonic() - started
            if elapsed >= GOOD_RUN_S:
                nlog.info(
                    "node %d: session lasted %.0fs -> healthy run, backoff reset",
                    node.node_id,
                    elapsed,
                )
                backoff = MIN_BACKOFF_S
            else:
                nlog.warning("node %d: session ended after %.0fs", node.node_id, elapsed)

            nlog.info("node %d: retrying in %.0fs", node.node_id, backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass

            if elapsed < GOOD_RUN_S:
                if not connected_once:
                    startup_failures += 1
                    if startup_failures >= STARTUP_FAST_RETRIES:
                        backoff = min(backoff * 2, MAX_BACKOFF_S)
                else:
                    backoff = min(backoff * 2, MAX_BACKOFF_S)
    finally:
        nlog.info(
            "node %d: supervisor stopped after %d packets "
            "(missed=%d dup=%d repeats=%d dropped=%d bad_len=%d bad_crc=%d bad_type=%d "
            "sent=%d cmd_failed=%d stale=%d echoed=%d)",
            node.node_id,
            stats.received,
            stats.missed,
            stats.duplicates,
            stats.repeats,
            stats.dropped,
            stats.bad_len,
            stats.bad_crc,
            stats.bad_type,
            stats.sent,
            stats.cmd_failed,
            stats.stale,
            stats.echoed,
        )


def make_outboxes(nodes):
    """One CMD queue per board, keyed by node id.

    Made up front so a keypress can be queued even while that board is disconnected
    (it is then discarded on reconnect).
    """
    return {node.node_id: asyncio.Queue(maxsize=OUTBOX_MAXSIZE) for node in nodes}


async def run_all(nodes, queue, outboxes, stop):
    """Run one run_node supervisor per board, all at the same time.

    They share one lock so only one board is being set up at a time; streaming is concurrent.
    """
    setup_lock = asyncio.Lock()
    results = await asyncio.gather(
        *(run_node(node, queue, outboxes[node.node_id], stop, setup_lock) for node in nodes),
        return_exceptions=True,
    )

    for node, result in zip(nodes, results):
        if isinstance(result, BaseException):
            node_log(node).warning("node %d: reader ended with %r", node.node_id, result)


async def cmd_ticker(nodes, outboxes, stop, interval):
    """Standalone test: send one random CMD to every board every `interval` seconds.

    Stands in for the keystroke reader when this file is run on its own.
    """
    seq = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        else:
            return  # stop was set rather than the interval elapsing

        for node in nodes:
            packet = build_cmd_packet(seq, random_cmd_fields())
            if post_command(outboxes[node.node_id], packet):
                node_log(node).warning(
                    "node %d: outbox full, oldest CMD dropped to queue seq=%d", node.node_id, seq
                )
        seq = (seq + 1) & 0xFF


async def drain(queue, stop):
    """Standalone test: take packets off the queue, count them and report the rate.

    Stands in for pipeline.forward_uplink, which sends them to the Ultra96 instead.
    """
    count = 0
    window = RateWindow()

    while not stop.is_set():
        try:
            packet = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        count += 1
        # Only format the packet when -v is on: this runs 200 times a second.
        if log.isEnabledFor(logging.DEBUG):
            log.debug("drained: %s", format_packet(packet))

        report = window.tick(packet.t_recv, HEADER_LEN + len(packet.payload))
        if report:
            rate, kbps = report
            # queued: rising means this loop is falling behind the BLE readers.
            log.info(
                "drain: %d packets total, %.1f Hz aggregate, %.1f kbps, queued=%d",
                count,
                rate,
                kbps,
                queue.qsize(),
                extra=PROGRESS,
            )


async def run_standalone(nodes, cmd_every=0.0):
    """Standalone test: run the BLE boards into drain() until cancelled."""
    queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    outboxes = make_outboxes(nodes)
    stop = asyncio.Event()
    restore_exc_handler = install_gatt_cancel_error_filter()
    tasks = [
        asyncio.create_task(run_all(nodes, queue, outboxes, stop)),
        asyncio.create_task(drain(queue, stop)),
    ]
    if cmd_every > 0:
        tasks.append(asyncio.create_task(cmd_ticker(nodes, outboxes, stop, cmd_every)))

    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        restore_exc_handler()  # after the gather; see the filter's docstring


def select_nodes(spec):
    """Resolve a comma-separated id list like "1,2" to NodeConfigs, in NODES order.

    Raises:
        argparse.ArgumentTypeError: unparseable list, or an id not in NODES.
    """
    by_id = {node.node_id: node for node in NODES}
    try:
        wanted = {int(part) for part in spec.split(",")}
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a comma-separated id list: {spec!r}")

    unknown = sorted(wanted - by_id.keys())
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown node ids: {unknown}")

    return tuple(node for node in NODES if node.node_id in wanted)


def main():
    """Standalone entry point. `-v` logs every packet; `--debug-libs` adds bleak's own logs."""
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true", help="log every packet")
    parser.add_argument(
        "--debug-libs", action="store_true", help="also log bleak/asyncssh internals"
    )
    parser.add_argument(
        "--nodes",
        type=select_nodes,
        default=NODES,
        metavar="IDS",
        help="comma-separated node ids to run, e.g. 1 (default: all)",
    )
    parser.add_argument(
        "--cmd-every",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="standalone test: write a random CMD packet to every node this often, "
        "standing in for the keystroke reader (default: off)",
    )
    args = parser.parse_args()

    log_file = setup_logging(LOG_FILE, args.verbose, debug_libs=args.debug_libs)
    suppress_queue_get_shutdown_warning()
    log.info("logging to %s", log_file)

    try:
        asyncio.run(run_standalone(args.nodes, args.cmd_every))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
