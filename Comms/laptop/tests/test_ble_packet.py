"""Pure wire-format tests for BLE SENSOR, CMD and ECHO packets."""

import pathlib
import re

import pytest

from ble_packet import (
    CMD_KIND_FIELD,
    CMD_VIBRATE,
    CRC_LEN,
    CRC_OFFSET,
    DATA_FIELD_COUNT,
    HEADER_LEN,
    IMU_FIELD_COUNT,
    PACKET_LEN,
    RESERVED_FIELD_COUNT,
    TYPE_CMD,
    TYPE_ECHO,
    TYPE_SENSOR,
    VIBRATE_RECOGNISED,
    BlePacket,
    PacketCrcError,
    build_cmd_packet,
    build_fixed_packet,
    build_vibrate_packet,
    calculate_crc,
    decode,
    format_packet,
    imu_status,
    is_vibrate_echo,
    packet_to_message,
    unpack_crc,
    unpack_fields,
)


def make_packet(packet_type, seq, fields=None):
    """Build a fixed-size packet as the firmware would put it on air."""
    fields = list(range(DATA_FIELD_COUNT)) if fields is None else fields
    return build_fixed_packet(packet_type, seq, fields)


def test_fixed_packet_layout_remains_26_bytes():
    assert IMU_FIELD_COUNT == 6
    assert RESERVED_FIELD_COUNT == 5
    assert DATA_FIELD_COUNT == 11
    assert CRC_LEN == 2
    assert CRC_OFFSET == 24
    assert PACKET_LEN == 26


def test_crc_matches_ccitt_false_check_value():
    """The published check value prevents two matching but wrong implementations."""
    assert calculate_crc(b"123456789") == 0x29B1


def test_decode_sensor_roundtrip():
    packet = make_packet(TYPE_SENSOR, 7)
    packet_type, seq, payload = decode(packet)

    assert (packet_type, seq) == (TYPE_SENSOR, 7)
    assert unpack_fields(payload) == list(range(DATA_FIELD_COUNT))
    assert unpack_crc(payload) == calculate_crc(packet[:CRC_OFFSET])


def test_decode_rejects_short_packet():
    with pytest.raises(ValueError, match="packet too short"):
        decode(b"\x01")


def test_decode_rejects_truncated_sensor():
    """The MTU-23 truncation case: 26 bytes sent, 20 arrive."""
    with pytest.raises(ValueError, match=r"invalid sensor packet length: 20 != 26"):
        decode(make_packet(TYPE_SENSOR, 1)[:20])


def test_decode_rejects_wrong_length_echo():
    with pytest.raises(ValueError, match=r"invalid echo packet length"):
        decode(make_packet(TYPE_ECHO, 1)[:24])


@pytest.mark.parametrize("corrupt_index", [0, 1, HEADER_LEN + 3, PACKET_LEN - 1])
def test_decode_rejects_corruption_in_header_data_or_crc(corrupt_index):
    packet = bytearray(make_packet(TYPE_SENSOR, 9))
    packet[corrupt_index] ^= 0x01

    with pytest.raises(PacketCrcError, match=r"invalid .* CRC"):
        decode(packet)


def test_decode_passes_an_unknown_type_on_at_any_length():
    """The client counts it as unknown, so decode doesn't reject it on length."""
    packet_type, seq, payload = decode(bytes((0x09, 3, 0xAB)))

    assert (packet_type, seq, payload) == (0x09, 3, b"\xab")


def test_decode_copies_payload_off_the_bleak_buffer():
    """The payload must not alias the mutable bytearray supplied by bleak."""
    buffer = bytearray(make_packet(TYPE_SENSOR, 1, [9] * DATA_FIELD_COUNT))
    _, _, payload = decode(buffer)
    buffer[HEADER_LEN] = 0xFF

    assert unpack_fields(payload)[0] == 9


def test_build_cmd_packet_shape_and_crc_byte_order():
    packet = build_cmd_packet(5, list(range(DATA_FIELD_COUNT)))

    assert len(packet) == PACKET_LEN
    assert packet[:2] == bytes((TYPE_CMD, 5))
    assert int.from_bytes(packet[CRC_OFFSET:], "little") == calculate_crc(
        packet[:CRC_OFFSET]
    )


def test_build_cmd_packet_is_decodable():
    fields = [1, -2, 300, -30000, 0, 7, 0, 0, 0, 0, 0]
    packet_type, seq, payload = decode(build_cmd_packet(200, fields))

    assert (packet_type, seq) == (TYPE_CMD, 200)
    assert unpack_fields(payload) == fields


def test_build_cmd_packet_wraps_seq():
    assert build_cmd_packet(256, [0] * DATA_FIELD_COUNT)[1] == 0


def test_build_cmd_packet_rejects_wrong_field_count():
    with pytest.raises(ValueError, match="need 11 fields, got 3"):
        build_cmd_packet(1, [1, 2, 3])


def test_build_cmd_packet_rejects_out_of_range_field():
    with pytest.raises(OverflowError):
        build_cmd_packet(1, [40000] + [0] * (DATA_FIELD_COUNT - 1))


def test_echo_matches_firmware_transformation_and_crc_refresh():
    """Firmware negates six active fields, preserves five reserved fields."""
    fields = [1, -2, 300, -30000, 0, 7, 10, 20, 30, 40, 50]
    echo_fields = [-value for value in fields[:IMU_FIELD_COUNT]] + fields[IMU_FIELD_COUNT:]
    echo = make_packet(TYPE_ECHO, 42, echo_fields)

    packet_type, seq, payload = decode(echo)

    assert (packet_type, seq) == (TYPE_ECHO, 42)
    assert unpack_fields(payload) == echo_fields
    assert unpack_crc(payload) == calculate_crc(echo[:CRC_OFFSET])


def firmware_echo(cmd):
    """Return the payload the firmware sends back for `cmd`, with the IMU fields negated."""
    _, seq, payload = decode(cmd)
    fields = unpack_fields(payload)
    echo_fields = [-value for value in fields[:IMU_FIELD_COUNT]] + fields[IMU_FIELD_COUNT:]
    return decode(make_packet(TYPE_ECHO, seq, echo_fields))[2]


def test_vibrate_packet_carries_the_pattern_and_its_kind():
    packet_type, seq, payload = decode(build_vibrate_packet(3, VIBRATE_RECOGNISED))

    assert (packet_type, seq) == (TYPE_CMD, 3)
    fields = unpack_fields(payload)
    assert fields[0] == VIBRATE_RECOGNISED
    assert fields[CMD_KIND_FIELD] == CMD_VIBRATE
    assert fields.count(0) == DATA_FIELD_COUNT - 2


def test_only_a_vibrates_echo_is_recognised_as_one():
    """The kind is in a reserved field, so the firmware's negation leaves it alone."""
    assert is_vibrate_echo(firmware_echo(build_vibrate_packet(0, VIBRATE_RECOGNISED)))
    # a keystroke CMD, as pipeline.keystroke_reader builds it
    keystroke = build_cmd_packet(0, [ord("1")] + [0] * (DATA_FIELD_COUNT - 1))
    assert not is_vibrate_echo(firmware_echo(keystroke))


def firmware_constants():
    """Return the sketch's `static constexpr` integers, resolving one that names another."""
    sketch = pathlib.Path(__file__).resolve().parents[2] / "firebeetle" / "firebeetle.ino"
    found = re.findall(r"static constexpr \w+ (\w+) = (\w+);", sketch.read_text(encoding="utf-8"))
    constants = {}
    for name, value in found:
        constants[name] = int(value, 0) if value[0].isdigit() else constants.get(value)
    return constants


def test_firmware_and_laptop_agree_on_the_vibrate_command():
    """The glove only buzzes if it reads the kind and pattern where the laptop writes them."""
    firmware = firmware_constants()

    assert firmware["CMD_KIND_FIELD"] == CMD_KIND_FIELD
    assert firmware["CMD_VIBRATE"] == CMD_VIBRATE
    assert firmware["VIBRATE_RECOGNISED"] == VIBRATE_RECOGNISED


def test_format_packet_is_readable_and_shows_named_fields_and_crc():
    """Guideline 7: log files carry decoded values and a CRC, not raw hex."""
    raw = make_packet(TYPE_SENSOR, 9)
    _, _, payload = decode(raw)

    line = format_packet(BlePacket(2, TYPE_SENSOR, 9, payload, 1234.5))

    assert line == (
        "node=2 SENSOR seq=9 ax=0 ay=1 az=2 gx=3 gy=4 gz=5 "
        "reserved=[6, 7, 8, 9, 10] "
        f"crc=0x{int.from_bytes(raw[CRC_OFFSET:], 'little'):04x}"
    )
    # the defect this replaces: the BlePacket repr rendered the payload as raw bytes
    assert "\\x" not in line


def test_format_packet_falls_back_for_an_unknown_type():
    """An unknown type has no fixed layout, so there are no named fields or CRC to show."""
    line = format_packet(BlePacket(1, 0x09, 3, b"\x01\x00\x02\x00", 0.0))

    assert line == "node=1 0X09 seq=3 fields=[1, 2]"


def test_packet_to_message_names_imu_reserved_fields_and_crc():
    raw = make_packet(TYPE_SENSOR, 9)
    _, _, payload = decode(raw)

    message = packet_to_message(BlePacket(2, TYPE_SENSOR, 9, payload, 1234.5))

    assert message == {
        "type": "SENSOR",
        "node": 2,
        "seq": 9,
        "timestamp": 1234.5,
        "fields": list(range(DATA_FIELD_COUNT)),
        "imu": {"ax": 0, "ay": 1, "az": 2, "gx": 3, "gy": 4, "gz": 5},
        "reserved": [6, 7, 8, 9, 10],
        "crc": int.from_bytes(raw[CRC_OFFSET:], "little"),
    }


def test_imu_status_is_read_from_the_first_reserved_field():
    # the fields either side (gz=6, reserved[1]=-1) must not leak in
    fields = [1, 2, 3, 4, 5, 6, 3, -1, 0, 0, 0]
    _, _, payload = decode(build_fixed_packet(TYPE_SENSOR, 0, fields))

    assert imu_status(payload) == 3
