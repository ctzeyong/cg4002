"""Round-trip a file through one FireBeetle over the existing BLE link.

Used only for the individual subsystem test, and not used in the final system. The
firmware still accepts these packets: see "BLE file-transfer packets" in Comms/README.md.
"""

import argparse
import asyncio
import logging
from pathlib import Path

from bleak import BleakClient
from bleak.exc import BleakError

import ble_client
import ble_packet
from logging_setup import setup_logging
from plumbing import install_gatt_cancel_error_filter


TYPE_FILE_START = 0x10
TYPE_FILE_DATA = 0x11
TYPE_FILE_END = 0x12
FILE_TYPES = (TYPE_FILE_START, TYPE_FILE_DATA, TYPE_FILE_END)

# Every file packet starts with type, sequence, then a uint64 size or offset.
FILE_HEADER_LEN = 10
FILE_CONTROL_LEN = FILE_HEADER_LEN
PREFERRED_MTU = 128
MAX_FILE_PACKET_LEN = PREFERRED_MTU - 3
MAX_FILE_DATA_LEN = MAX_FILE_PACKET_LEN - FILE_HEADER_LEN
UINT64_MAX = (1 << 64) - 1

ECHO_TIMEOUT_S = 2.0
MAX_SEND_ATTEMPTS = 3
LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "file_transfer.log"

log = logging.getLogger("file_transfer")


def _uint64_bytes(value, name):
    """Encode a non-negative uint64 with a useful validation error."""
    if not 0 <= value <= UINT64_MAX:
        raise ValueError(f"{name} must fit in an unsigned 64-bit integer")
    return value.to_bytes(8, "little")


def build_file_start(seq, file_size):
    """Build a FILE_START packet carrying the total byte count."""
    return bytes((TYPE_FILE_START, seq & 0xFF)) + _uint64_bytes(file_size, "file size")


def build_file_data(seq, offset, data):
    """Build one offset-addressed raw-byte FILE_DATA packet."""
    data = bytes(data)
    if not data:
        raise ValueError("file data packet cannot be empty")
    if len(data) > MAX_FILE_DATA_LEN:
        raise ValueError(f"file data is {len(data)} bytes; maximum is {MAX_FILE_DATA_LEN}")
    return bytes((TYPE_FILE_DATA, seq & 0xFF)) + _uint64_bytes(offset, "offset") + data


def build_file_end(seq, file_size):
    """Build a FILE_END packet repeating the expected total byte count."""
    return bytes((TYPE_FILE_END, seq & 0xFF)) + _uint64_bytes(file_size, "file size")


def parse_file_packet(packet):
    """Return (type, seq, size_or_offset, data) for one file packet."""
    packet = bytes(packet)
    if len(packet) < FILE_HEADER_LEN:
        raise ValueError(f"file packet too short: {len(packet)} < {FILE_HEADER_LEN}")

    packet_type, seq = packet[:2]
    if packet_type not in FILE_TYPES:
        raise ValueError(f"not a file packet: 0x{packet_type:02x}")
    if packet_type in (TYPE_FILE_START, TYPE_FILE_END) and len(packet) != FILE_CONTROL_LEN:
        raise ValueError(f"file control packet must be {FILE_CONTROL_LEN} bytes")
    if packet_type == TYPE_FILE_DATA and len(packet) == FILE_HEADER_LEN:
        raise ValueError("file data packet has no data")
    if len(packet) > MAX_FILE_PACKET_LEN:
        raise ValueError(f"file packet exceeds {MAX_FILE_PACKET_LEN} bytes")

    value = int.from_bytes(packet[2:FILE_HEADER_LEN], "little")
    return packet_type, seq, value, packet[FILE_HEADER_LEN:]


def make_file_notify_handler(echoes):
    """Queue valid file replies and ignore the concurrent sensor stream."""

    def on_notify(_char, data):
        if not data or data[0] not in FILE_TYPES:
            return
        try:
            parse_file_packet(data)
        except ValueError as exc:
            log.warning("invalid file reply ignored: %s", exc)
            return
        echoes.put_nowait(bytes(data))

    return on_notify


async def wait_for_matching_echo(echoes, expected, timeout=None):
    """Wait for the exact returned frame, discarding delayed duplicate replies."""
    if timeout is None:
        timeout = ECHO_TIMEOUT_S
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        received = await asyncio.wait_for(echoes.get(), timeout=remaining)
        if received == expected:
            return received
        log.warning("unexpected file reply ignored: type=0x%02x seq=%d", received[0], received[1])


async def round_trip_packet(client, node, echoes, packet):
    """Write one frame and require its byte-exact notification reply."""
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            await client.write_gatt_char(node.cmd_char_uuid, packet, response=True)
            return await wait_for_matching_echo(echoes, packet)
        except (BleakError, OSError, asyncio.TimeoutError) as exc:
            if attempt == MAX_SEND_ATTEMPTS:
                raise TimeoutError(
                    f"no matching reply for type=0x{packet[0]:02x} seq={packet[1]} "
                    f"after {attempt} attempts"
                ) from exc
            log.warning(
                "type=0x%02x seq=%d attempt %d failed; retrying",
                packet[0],
                packet[1],
                attempt,
            )


def chunk_size_for_mtu(mtu):
    """Return the largest file payload fitting one ATT message."""
    mtu = int(mtu)
    if mtu < ble_packet.PACKET_LEN + 3:
        raise ValueError(
            f"negotiated MTU {mtu} is below the firmware's {ble_packet.PACKET_LEN + 3}-byte minimum"
        )
    return min(mtu - 3, MAX_FILE_PACKET_LEN) - FILE_HEADER_LEN


def files_equal(first, second, block_size=64 * 1024):
    """Compare two files byte-for-byte without loading either into memory."""
    if first.stat().st_size != second.stat().st_size:
        return False
    with first.open("rb") as left, second.open("rb") as right:
        while True:
            left_block = left.read(block_size)
            right_block = right.read(block_size)
            if left_block != right_block:
                return False
            if not left_block:
                return True


def default_output_path(source):
    """Keep the original extension while marking the file as returned."""
    return source.with_name(f"{source.stem}.returned{source.suffix}")


async def transfer_file(client, node, source, destination, echoes, chunk_size):
    """Stream a file through the board, reconstruct it, then compare it exactly."""
    file_size = source.stat().st_size
    if file_size > UINT64_MAX:
        raise ValueError("file is too large for the uint64 offset field")
    if destination.exists():
        raise FileExistsError(f"output already exists: {destination}")

    destination_created = False
    offset = 0
    next_progress = 10
    try:
        with source.open("rb") as incoming, destination.open("xb") as returned_file:
            destination_created = True
            seq = 0
            await round_trip_packet(client, node, echoes, build_file_start(seq, file_size))
            seq = (seq + 1) & 0xFF

            while True:
                data = incoming.read(chunk_size)
                if not data:
                    break

                reply = await round_trip_packet(
                    client, node, echoes, build_file_data(seq, offset, data)
                )
                returned_data = parse_file_packet(reply)[3]
                returned_file.write(returned_data)

                offset += len(returned_data)
                seq = (seq + 1) & 0xFF
                if file_size:
                    progress = offset * 100 // file_size
                    if progress >= next_progress:
                        log.info("progress: %d%% (%d/%d bytes)", progress, offset, file_size)
                        next_progress = (progress // 10 + 1) * 10

        await round_trip_packet(client, node, echoes, build_file_end(seq, file_size))
        if offset != file_size or not files_equal(source, destination):
            raise RuntimeError("returned file differs from the original")
    except BaseException:
        if destination_created:
            destination.unlink(missing_ok=True)
        raise

    log.info("PASS: %s matches %s byte-for-byte (%d bytes)", destination, source, file_size)


async def run_transfer(node, source, destination):
    """Connect to one board, transfer one file, and cleanly disconnect."""
    restore_exc_handler = install_gatt_cancel_error_filter()
    client = None
    echoes = asyncio.Queue()
    subscribed = False
    try:
        device = await ble_client.find_device(node)
        if device is None:
            raise BleakError(f"node {node.node_id} was not found")

        client = BleakClient(device)
        try:
            await asyncio.wait_for(client.connect(), timeout=ble_client.SETUP_STEP_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise BleakError(
                f"connect timed out after {ble_client.SETUP_STEP_TIMEOUT_S:.0f}s"
            ) from exc

        try:
            await asyncio.wait_for(
                client.start_notify(node.sensor_char_uuid, make_file_notify_handler(echoes)),
                timeout=ble_client.SETUP_STEP_TIMEOUT_S,
            )
        except asyncio.TimeoutError as exc:
            raise BleakError(
                f"subscribe timed out after {ble_client.SETUP_STEP_TIMEOUT_S:.0f}s"
            ) from exc
        subscribed = True

        mtu = getattr(client, "mtu_size", 23)
        chunk_size = chunk_size_for_mtu(mtu)
        log.info(
            "node %d connected; mtu=%d, file payload=%d bytes/packet",
            node.node_id,
            mtu,
            chunk_size,
        )
        await transfer_file(client, node, source, destination, echoes, chunk_size)
    finally:
        try:
            if client is not None:
                if subscribed:
                    try:
                        await client.stop_notify(node.sensor_char_uuid)
                    except (BleakError, OSError):
                        pass
                await ble_client.disconnect_client(client, node)
        finally:
            # Keep the filter active through teardown, where late WinRT callbacks occur.
            restore_exc_handler()


def parse_args():
    """Parse the standalone file-transfer command line."""
    parser = argparse.ArgumentParser(
        description="Send a file to one FireBeetle and reconstruct its returned chunks."
    )
    parser.add_argument("file", type=Path, help="file to send")
    parser.add_argument("--node", type=int, choices=(1, 2), default=1)
    parser.add_argument("--output", type=Path, help="returned path (default: *.returned.*)")
    return parser.parse_args()


def main():
    """Run the standalone transfer and return a process exit code."""
    args = parse_args()
    log_file = setup_logging(LOG_FILE, verbose=False)
    log.info("logging to %s", log_file)

    source = args.file.resolve()
    if not source.is_file():
        log.error("input is not a file: %s", source)
        return 2
    destination = (args.output or default_output_path(source)).resolve()
    if destination == source:
        log.error("output path must differ from the input path")
        return 2

    node = next(node for node in ble_client.NODES if node.node_id == args.node)
    log.info("round-trip transfer: %s -> node %d -> %s", source, node.node_id, destination)
    try:
        asyncio.run(run_transfer(node, source, destination))
    except KeyboardInterrupt:
        return 130
    except (BleakError, OSError, ValueError, RuntimeError) as exc:
        log.error("file transfer failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
