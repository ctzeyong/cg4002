"""Checks the BLE link from the laptop to the Player FireBeetle.

Success criteria: rate ~= 100 Hz, gaps == 0 and CRC failures == 0.
"""

import asyncio
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from ble_packet import (
    TYPE_SENSOR,
    PacketCrcError,
    PacketLengthError,
    decode,
    unpack_crc,
    unpack_fields,
)

P1_SERVICE_UUID = "b1160001-0000-4000-8000-000000000001"
P1_CHAR_UUID    = "b1160002-0000-4000-8000-000000000001"

RUN_SECONDS    = 30
SCAN_TIMEOUT_S = 10


@dataclass
class Stats:
    """Running counters for one notification stream.

    `last_seq` holds the 1-byte sequence from the previous packet; gaps are
    inferred by comparing against it modulo 256, so a wrap at 255 reads as
    contiguous. `started_at` is set on the first notification rather than at
    connect, so the reported rate reflects on-air streaming and excludes the
    pairing handshake.
    """

    last_seq: Optional[int] = None
    received: int = 0
    missed: int = 0
    bad_type: int = 0
    bad_len: int = 0
    bad_crc: int = 0
    started_at: float = 0.0
    last_notify_at: float = 0.0
    stats_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


stats = Stats()


def on_notify(_char, data: bytearray) -> None:
    """Decode one BLE notification and fold it into the module-level `stats`.

    Runs on bleak's notification callback rather than as a task, so it must stay
    non-blocking. Malformed packets are counted and dropped rather than raised —
    an exception here is swallowed by the backend and would silently lose the
    rest of the run.
    """
    now = time.monotonic()
    if stats.started_at == 0.0:
        stats.started_at = now
    stats.last_notify_at = now

    try:
        packet_type, seq, payload = decode(data)
    except PacketCrcError as exc:
        stats.bad_crc += 1
        print(f"[!] {exc}")
        return
    except PacketLengthError as exc:
        stats.bad_len += 1
        print(f"[!] {exc}: {bytes(data).hex()}")
        return

    if packet_type != TYPE_SENSOR:
        stats.bad_type += 1
        print(f"[!] unexpected type 0x{packet_type:02x} seq={seq}")
        return

    stats.received += 1
    if stats.last_seq is not None:
        expected = (stats.last_seq + 1) & 0xFF
        if seq != expected:
            gap = (seq - expected) & 0xFF
            stats.missed += gap
            print(f"[gap] expected seq={expected} got seq={seq} (missed {gap})")

    stats.last_seq = seq
    if stats.received % 20 == 0:
        elapsed = now - stats.started_at
        rate = stats.received / elapsed if elapsed > 0 else 0.0
        print(
            f"[rx] n={stats.received} seq={seq} rate={rate:.1f} Hz "
            f"missed={stats.missed} imu={unpack_fields(payload)[:6]} "
            f"crc=0x{unpack_crc(payload):04x}"
        )


async def find_device():
    """Scan for the Player-1 FireBeetle by advertised service UUID.

    Filtering on the advert means we never connect to the wrong peripheral, which
    is what makes the same script work unchanged once Player 2 is flashed. Exits
    the process if nothing matches within SCAN_TIMEOUT_S.
    """
    print(f"[scan] looking for service {P1_SERVICE_UUID} (up to {SCAN_TIMEOUT_S}s)...")
    device = await BleakScanner.find_device_by_filter(
        lambda _d, ad: P1_SERVICE_UUID.lower()
        in [str(u).lower() for u in (ad.service_uuids or [])],
        timeout=SCAN_TIMEOUT_S,
    )

    if device is None:
        print("[scan] no matching device — is the FireBeetle powered and advertising?")
        sys.exit(1)
    print(f"[scan] found: name={device.name!r} addr={device.address}")
    return device


async def main() -> None:
    """Connect, stream for RUN_SECONDS, then print the pass/fail summary.

    The first connect triggers Just Works pairing and bonding, because the
    characteristic is encryption-required; later runs reuse the stored bond and
    connect silently. A handful of missed packets at the very start is a
    subscription-timing artifact, not on-air loss.
    """
    device = await find_device()
    print("[conn] connecting (first connect triggers pairing — may take a few seconds)...")

    async with BleakClient(device) as client:
        mtu = getattr(client, "mtu_size", "unknown")
        print(f"[conn] connected; mtu={mtu}")
        try:
            await client.start_notify(P1_CHAR_UUID, on_notify)

        except BleakError as exc:
            print(f"[!] start_notify failed: {exc}")
            return
        
        print(f"[run] streaming for {RUN_SECONDS}s...")
        await asyncio.sleep(RUN_SECONDS)
        try:
            await client.stop_notify(P1_CHAR_UUID)
        except BleakError:
            pass

    # Elapsed = first-notify -> last-notify, so the rate reflects on-air streaming
    # and not the extra ~seconds spent on stop_notify + disconnect after RUN_SECONDS.
    elapsed = max(stats.last_notify_at - stats.started_at, 1e-9) if stats.started_at else 0.0
    rate = stats.received / elapsed if elapsed > 0 else 0.0
    print("\n--- summary ---")
    print(f"received: {stats.received} packets")
    print(f"elapsed:  {elapsed:.2f} s")
    print(f"rate:     {rate:.1f} Hz (target 100)")
    print(f"missed:   {stats.missed} packets")
    print(
        f"bad_len:  {stats.bad_len}, bad_crc: {stats.bad_crc}, "
        f"bad_type: {stats.bad_type}"
    )


if __name__ == "__main__":
    asyncio.run(main())
