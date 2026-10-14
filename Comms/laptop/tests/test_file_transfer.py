import asyncio

import pytest

import file_transfer
from ble_client import NODES
from ble_packet import TYPE_SENSOR
from file_transfer import (
    FILE_CONTROL_LEN,
    FILE_HEADER_LEN,
    MAX_FILE_DATA_LEN,
    MAX_FILE_PACKET_LEN,
    TYPE_FILE_DATA,
    TYPE_FILE_END,
    TYPE_FILE_START,
    build_file_data,
    build_file_end,
    build_file_start,
    chunk_size_for_mtu,
    default_output_path,
    files_equal,
    make_file_notify_handler,
    parse_file_packet,
    round_trip_packet,
    transfer_file,
    wait_for_matching_echo,
)


def test_file_control_packets_carry_uint64_size():
    size = (1 << 40) + 7
    for packet, packet_type in (
        (build_file_start(4, size), TYPE_FILE_START),
        (build_file_end(5, size), TYPE_FILE_END),
    ):
        assert len(packet) == FILE_CONTROL_LEN
        assert parse_file_packet(packet) == (packet_type, packet[1], size, b"")


def test_file_data_is_binary_safe_and_offset_addressed():
    data = bytes(range(256))[:MAX_FILE_DATA_LEN]
    packet = build_file_data(260, 123_456, data)

    assert len(packet) <= MAX_FILE_PACKET_LEN
    assert parse_file_packet(packet) == (TYPE_FILE_DATA, 4, 123_456, data)


@pytest.mark.parametrize(
    "packet, message",
    [
        (b"\x10", "too short"),
        (bytes((0x7F, 0)) + b"\0" * 8, "not a file packet"),
        (bytes((TYPE_FILE_START, 0)) + b"\0" * 9, "control packet"),
        (bytes((TYPE_FILE_DATA, 0)) + b"\0" * 8, "has no data"),
    ],
)
def test_parse_file_packet_rejects_malformed_frames(packet, message):
    with pytest.raises(ValueError, match=message):
        parse_file_packet(packet)


def test_build_file_data_rejects_empty_or_oversized_payload():
    with pytest.raises(ValueError, match="cannot be empty"):
        build_file_data(0, 0, b"")
    with pytest.raises(ValueError, match="maximum"):
        build_file_data(0, 0, b"x" * (MAX_FILE_DATA_LEN + 1))


def test_chunk_size_uses_negotiated_mtu_and_firmware_minimum():
    assert chunk_size_for_mtu(128) == MAX_FILE_DATA_LEN
    assert chunk_size_for_mtu(64) == 64 - 3 - FILE_HEADER_LEN
    with pytest.raises(ValueError, match="firmware"):
        chunk_size_for_mtu(23)


@pytest.mark.asyncio
async def test_notify_handler_ignores_sensor_packets_and_queues_file_replies():
    echoes = asyncio.Queue()
    handler = make_file_notify_handler(echoes)
    file_packet = build_file_data(1, 0, b"abc")

    handler(None, bytes((TYPE_SENSOR, 0)) + b"\0" * 24)
    handler(None, file_packet)

    assert echoes.qsize() == 1
    assert echoes.get_nowait() == file_packet


@pytest.mark.asyncio
async def test_wait_for_matching_echo_discards_a_delayed_duplicate():
    echoes = asyncio.Queue()
    expected = build_file_data(2, 20, b"new")
    echoes.put_nowait(build_file_data(1, 0, b"old"))
    echoes.put_nowait(expected)

    assert await wait_for_matching_echo(echoes, expected, timeout=0.1) == expected
    assert echoes.empty()


class EchoClient:
    """Bleak stand-in that returns each write through the notify queue."""

    def __init__(self, echoes, drop_writes=0, mutate_data=False):
        self.echoes = echoes
        self.drop_writes = drop_writes
        self.mutate_data = mutate_data
        self.writes = []

    async def write_gatt_char(self, uuid, data, response=True):
        self.writes.append((uuid, bytes(data), response))
        if self.drop_writes:
            self.drop_writes -= 1
            return
        reply = bytes(data)
        if self.mutate_data and reply[0] == TYPE_FILE_DATA:
            reply = reply[:-1] + bytes((reply[-1] ^ 0xFF,))
        self.echoes.put_nowait(reply)


@pytest.mark.asyncio
async def test_round_trip_packet_retries_a_missing_reply(monkeypatch):
    echoes = asyncio.Queue()
    client = EchoClient(echoes, drop_writes=1)
    packet = build_file_start(0, 12)
    monkeypatch.setattr(file_transfer, "ECHO_TIMEOUT_S", 0.01)

    returned = await round_trip_packet(client, NODES[0], echoes, packet)

    assert returned == packet
    assert len(client.writes) == 2
    assert all(write[0] == NODES[0].cmd_char_uuid for write in client.writes)


@pytest.mark.asyncio
async def test_transfer_streams_and_reconstructs_arbitrary_bytes(tmp_path):
    source = tmp_path / "sample.pdf"
    destination = tmp_path / "sample.returned.pdf"
    content = bytes(range(256)) * 3 + b"\x00\xffPDF-tail"
    source.write_bytes(content)
    echoes = asyncio.Queue()
    client = EchoClient(echoes)

    await transfer_file(client, NODES[0], source, destination, echoes, chunk_size=37)

    assert destination.read_bytes() == content
    assert files_equal(source, destination)
    packet_types = [data[0] for _, data, _ in client.writes]
    assert packet_types[0] == TYPE_FILE_START
    assert packet_types[-1] == TYPE_FILE_END
    assert set(packet_types[1:-1]) == {TYPE_FILE_DATA}


@pytest.mark.asyncio
async def test_transfer_removes_a_corrupt_partial_output(tmp_path, monkeypatch):
    source = tmp_path / "input.bin"
    destination = tmp_path / "output.bin"
    source.write_bytes(b"contents")
    echoes = asyncio.Queue()
    client = EchoClient(echoes, mutate_data=True)
    monkeypatch.setattr(file_transfer, "ECHO_TIMEOUT_S", 0.01)

    with pytest.raises(TimeoutError, match="no matching reply"):
        await transfer_file(client, NODES[0], source, destination, echoes, chunk_size=16)

    assert not destination.exists()


def test_files_equal_detects_same_size_content_change(tmp_path):
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"abc")
    second.write_bytes(b"abd")

    assert files_equal(first, second) is False


def test_default_output_path_keeps_the_extension(tmp_path):
    source = tmp_path / "demo.mp4"
    assert default_output_path(source) == tmp_path / "demo.returned.mp4"


@pytest.mark.asyncio
@pytest.mark.parametrize("hung_step", ["connect", "subscribe"])
async def test_run_transfer_reports_setup_timeout_and_cleans_up(monkeypatch, tmp_path, hung_step):
    disconnected = False
    handler_restored = False

    class SetupClient:
        mtu_size = 128

        def __init__(self, _device):
            pass

        async def connect(self):
            if hung_step == "connect":
                await asyncio.Event().wait()

        async def start_notify(self, _uuid, _handler):
            if hung_step == "subscribe":
                await asyncio.Event().wait()

    async def find_device(_node):
        return object()

    async def disconnect_client(_client, _node):
        nonlocal disconnected
        disconnected = True

    def install_filter():
        def restore():
            nonlocal handler_restored
            handler_restored = True

        return restore

    monkeypatch.setattr(file_transfer.ble_client, "find_device", find_device)
    monkeypatch.setattr(file_transfer.ble_client, "disconnect_client", disconnect_client)
    monkeypatch.setattr(file_transfer.ble_client, "SETUP_STEP_TIMEOUT_S", 0.01)
    monkeypatch.setattr(file_transfer, "BleakClient", SetupClient)
    monkeypatch.setattr(file_transfer, "install_gatt_cancel_error_filter", install_filter)

    expected_step = "connect" if hung_step == "connect" else "subscribe"
    with pytest.raises(file_transfer.BleakError, match=rf"{expected_step} timed out after 0s"):
        await file_transfer.run_transfer(
            NODES[0], tmp_path / "input.bin", tmp_path / "output.bin"
        )

    assert disconnected is True
    assert handler_restored is True
