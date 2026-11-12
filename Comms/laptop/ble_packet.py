"""Encode and decode the fixed 20-byte BLE packets (SENSOR, CMD, ECHO).

    byte 0      type
    byte 1      seq (0-255, wraps)
    bytes 2-17  8 x int16 little-endian: ax ay az gx gy gz, then 2 reserved
    bytes 18-19 CRC-16/CCITT-FALSE of bytes 0-17, little-endian

In a SENSOR packet the first reserved field is the IMU status (firebeetle/imu.h),
which ImuCheck turns into warnings. In a CMD it says what kind of command it is, and a
VIBRATE puts its pattern in field 0.

No bleak or asyncio here, so the format can be tested without Bluetooth.
"""

import binascii
from dataclasses import dataclass


TYPE_SENSOR = 0x01  # uplink, free-running stream
TYPE_CMD = 0x03  # downlink, laptop -> board
TYPE_ECHO = 0x04  # uplink, the board's reply to a CMD

TYPE_NAMES = {
    TYPE_SENSOR: "sensor",
    TYPE_CMD: "cmd",
    TYPE_ECHO: "echo",
}

HEADER_LEN = 2
IMU_FIELD_COUNT = 6
IMU_FIELD_NAMES = ("ax", "ay", "az", "gx", "gy", "gz")
RESERVED_FIELD_COUNT = 2
DATA_FIELD_COUNT = IMU_FIELD_COUNT + RESERVED_FIELD_COUNT
FIELD_LEN = 2
DATA_LEN = DATA_FIELD_COUNT * FIELD_LEN
CRC_LEN = 2
CRC_OFFSET = HEADER_LEN + DATA_LEN
PACKET_LEN = CRC_OFFSET + CRC_LEN

CRC_INIT = 0xFFFF

FIXED_LEN_TYPES = (TYPE_SENSOR, TYPE_ECHO, TYPE_CMD)

# IMU status values, the same as ImuStatus in firebeetle/imu.h
IMU_UNREPORTED = 0  # firmware from before the IMU
IMU_OK = 1
IMU_MISSING = 2
IMU_RECOVERING = 3
IMU_STATUS_OFFSET = IMU_FIELD_COUNT * FIELD_LEN  # reserved[0], counted from the payload start

IMU_PROBLEMS = {
    IMU_MISSING: "IMU missing, sending zeros",
    IMU_RECOVERING: "IMU readings restarting after a reset or a gap",
}

# reserved[0] of a CMD says what kind it is, 0 for keystrokes
# the board only negates the IMU fields, so its ECHO brings the kind back unchanged
CMD_KIND_FIELD = IMU_FIELD_COUNT
CMD_VIBRATE = 1
VIBRATE_RECOGNISED = 1  # field 0 of a VIBRATE, meaning the board classified a gesture


class PacketLengthError(ValueError):
    """A packet cannot have the fixed wire shape expected for its type."""


class PacketCrcError(ValueError):
    """A fixed-shape packet's received CRC does not match its contents."""


@dataclass
class BlePacket:
    """One decoded notification, tagged with its connection and receive time."""

    node_id: int
    type: int
    seq: int
    payload: bytes
    t_recv: float


def decode(data):
    """Check one notification and split it into ``(type, seq, payload)``.

    Raises:
        PacketLengthError: shorter than ``HEADER_LEN``, or a fixed-length type
            whose length is not ``PACKET_LEN``.
        PacketCrcError: a 20-byte fixed-shape packet fails its CRC check.
    """
    if len(data) < HEADER_LEN:
        raise PacketLengthError(f"packet too short: {len(data)} < {HEADER_LEN}")

    packet_type, seq = data[0], data[1]

    if packet_type in FIXED_LEN_TYPES and len(data) != PACKET_LEN:
        name = TYPE_NAMES.get(packet_type, f"0x{packet_type:02x}")
        raise PacketLengthError(
            f"invalid {name} packet length: {len(data)} != {PACKET_LEN}"
        )

    # CRC-check every 20-byte packet, whatever its type byte says, so a corrupted
    # type byte cannot skip the check.
    if len(data) == PACKET_LEN:
        received_crc = int.from_bytes(data[CRC_OFFSET:PACKET_LEN], "little")
        expected_crc = calculate_crc(data[:CRC_OFFSET])
        if received_crc != expected_crc:
            name = TYPE_NAMES.get(packet_type, f"0x{packet_type:02x}")
            raise PacketCrcError(
                f"invalid {name} CRC: received 0x{received_crc:04x}, "
                f"expected 0x{expected_crc:04x}"
            )

    payload = bytes(data[HEADER_LEN:])  # bleak hands over a mutable bytearray

    return packet_type, seq, payload


def calculate_crc(data):
    """Return CRC-16/CCITT-FALSE (poly 0x1021, init 0xffff) for ``data``."""
    return binascii.crc_hqx(bytes(data), CRC_INIT)


def unpack_fields(payload):
    """Decode the 8 data fields, excluding a fixed payload's trailing CRC."""
    field_bytes = payload[:DATA_LEN] if len(payload) == DATA_LEN + CRC_LEN else payload
    return [
        int.from_bytes(field_bytes[i : i + FIELD_LEN], "little", signed=True)
        for i in range(0, len(field_bytes) - 1, FIELD_LEN)
    ]


def unpack_crc(payload):
    """Read the little-endian CRC from a fixed-size packet payload."""
    if len(payload) != DATA_LEN + CRC_LEN:
        raise PacketLengthError(
            f"invalid fixed payload length: {len(payload)} != {DATA_LEN + CRC_LEN}"
        )
    return int.from_bytes(payload[DATA_LEN:], "little")


def imu_status(payload):
    """Return the IMU status a SENSOR payload carries in reserved[0]."""
    status = payload[IMU_STATUS_OFFSET : IMU_STATUS_OFFSET + FIELD_LEN]
    return int.from_bytes(status, "little", signed=True)


class ImuCheck:
    """Warn once when a node's IMU stops giving real readings, and log when it is back.

    Make one per connection. Status 0 means old firmware with nothing to report, so it
    never warns.
    """

    def __init__(self, node_id, log):
        self.node_id = node_id
        self.log = log
        self.problem_since = None  # when the current problem started, None while healthy

    def update(self, status, now):
        """Take the status of one SENSOR packet received at `now`, in seconds."""
        healthy = status in (IMU_UNREPORTED, IMU_OK)
        if not healthy and self.problem_since is None:
            self.problem_since = now
            problem = IMU_PROBLEMS.get(status, f"unknown IMU status {status}")
            self.log.warning("node %d: %s", self.node_id, problem)
        elif healthy and self.problem_since is not None:
            self.log.info(
                "node %d: IMU OK again after %.1fs", self.node_id, now - self.problem_since
            )
            self.problem_since = None


def build_fixed_packet(packet_type, seq, fields):
    """Serialise a fixed SENSOR, CMD or ECHO packet and append its CRC.

    Raises:
        ValueError: packet type is not fixed, or the number of fields is wrong.
        OverflowError: a field is outside the signed 16-bit range.
    """
    if packet_type not in FIXED_LEN_TYPES:
        raise ValueError(f"type 0x{packet_type:02x} does not use the fixed packet format")
    if len(fields) != DATA_FIELD_COUNT:
        raise ValueError(f"need {DATA_FIELD_COUNT} fields, got {len(fields)}")

    packet = bytearray((packet_type, seq & 0xFF))
    for value in fields:
        packet += int(value).to_bytes(FIELD_LEN, "little", signed=True)
    packet += calculate_crc(packet).to_bytes(CRC_LEN, "little")
    return bytes(packet)


def build_cmd_packet(seq, fields):
    """Serialise one CMD using the fixed packet shape.

    Raises:
        ValueError: wrong number of fields.
        OverflowError: a field outside the signed 16-bit range.
    """
    return build_fixed_packet(TYPE_CMD, seq, fields)


def build_vibrate_packet(seq, pattern):
    """Build a VIBRATE, which is a CMD with `pattern` in field 0.

    Raises:
        OverflowError: `pattern` does not fit in an int16.
    """
    fields = [0] * DATA_FIELD_COUNT
    fields[0] = pattern
    fields[CMD_KIND_FIELD] = CMD_VIBRATE
    return build_cmd_packet(seq, fields)


def is_vibrate_echo(payload):
    """Return True if this ECHO is the board's reply to a VIBRATE."""
    return unpack_fields(payload)[CMD_KIND_FIELD] == CMD_VIBRATE


def format_packet(packet):
    """One ``BlePacket`` as a readable log line instead of raw hex.

    Shows node, type, seq, the named IMU values, reserved fields and CRC.
    """
    name = TYPE_NAMES.get(packet.type, f"0x{packet.type:02x}").upper()
    head = f"node={packet.node_id} {name} seq={packet.seq}"
    fields = unpack_fields(packet.payload)

    if packet.type not in FIXED_LEN_TYPES or len(packet.payload) != DATA_LEN + CRC_LEN:
        return f"{head} fields={fields}"

    imu = " ".join(f"{n}={v}" for n, v in zip(IMU_FIELD_NAMES, fields[:IMU_FIELD_COUNT]))
    return (
        f"{head} {imu} reserved={fields[IMU_FIELD_COUNT:]} "
        f"crc=0x{unpack_crc(packet.payload):04x}"
    )


def packet_to_message(packet):
    """Turn a ``BlePacket`` into the dict sent to the Ultra96 as JSON over TCP.

    ``node`` comes from the BLE connection, not the packet. Values are decoded
    (not raw bytes) so both ends can log them readably.
    """
    fields = unpack_fields(packet.payload)
    message = {
        "type": TYPE_NAMES.get(packet.type, f"0x{packet.type:02x}").upper(),
        "node": packet.node_id,
        "seq": packet.seq,
        "timestamp": packet.t_recv,
        "fields": fields,
    }

    if packet.type in FIXED_LEN_TYPES:
        message["imu"] = dict(zip(IMU_FIELD_NAMES, fields[:IMU_FIELD_COUNT]))
        message["reserved"] = fields[IMU_FIELD_COUNT:]
        message["crc"] = unpack_crc(packet.payload)

    return message
