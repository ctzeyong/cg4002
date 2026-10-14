"""Framing tests: prove recv_message reassembles correctly no matter how the
underlying byte stream is fragmented or coalesced by TCP.
"""

import asyncio

import pytest

from framing import MAX_MESSAGE_BYTES, FramingError, recv_message, send_message


class FakeWriter:
    """Minimal StreamWriter stand-in that just accumulates bytes.

    Lets the tests inspect the exact wire bytes send_message produced, and
    confirms a rejected send writes nothing at all.
    """

    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        return


def make_reader(chunks: list[bytes]) -> asyncio.StreamReader:
    """Build a StreamReader pre-loaded with `chunks`, then EOF.

    Each chunk models one arrival, so the split points are exactly the
    fragmentation pattern under test.
    """
    reader = asyncio.StreamReader()
    for c in chunks:
        reader.feed_data(c)
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_roundtrip_single_chunk():
    """Baseline: what send_message writes, recv_message reads back."""
    writer = FakeWriter()
    await send_message(writer, {"hello": "world", "n": 1})
    reader = make_reader([bytes(writer.buf)])
    assert await recv_message(reader) == {"hello": "world", "n": 1}


@pytest.mark.asyncio
async def test_byte_by_byte_fragmentation():
    """Every byte in its own chunk — worst-case fragmentation."""
    writer = FakeWriter()
    await send_message(writer, {"a": [1, 2, 3, 4, 5], "b": "hi"})
    chunks = [bytes([b]) for b in writer.buf]
    reader = make_reader(chunks)
    assert await recv_message(reader) == {"a": [1, 2, 3, 4, 5], "b": "hi"}


@pytest.mark.asyncio
async def test_two_messages_coalesced():
    """Two messages arrive in a single read — must not conflate them."""
    writer = FakeWriter()
    await send_message(writer, {"m": 1})
    await send_message(writer, {"m": 2})
    reader = make_reader([bytes(writer.buf)])
    assert await recv_message(reader) == {"m": 1}
    assert await recv_message(reader) == {"m": 2}


@pytest.mark.asyncio
async def test_header_split_across_chunks():
    """4-byte length prefix itself is split across two reads."""
    writer = FakeWriter()
    await send_message(writer, {"x": "y"})
    data = bytes(writer.buf)
    reader = make_reader([data[:2], data[2:]])
    assert await recv_message(reader) == {"x": "y"}


@pytest.mark.asyncio
async def test_oversized_length_rejected():
    """Corrupted / malicious length prefix must not trigger huge alloc."""
    bad_len = (MAX_MESSAGE_BYTES + 1).to_bytes(4, "big")
    reader = make_reader([bad_len])
    with pytest.raises(ValueError):
        await recv_message(reader)


@pytest.mark.asyncio
async def test_undecodable_payload_raises_framing_error():
    """A well-framed but non-JSON payload must raise FramingError, not bare
    JSONDecodeError — the client's reconnect loop keys off FramingError."""
    body = b"\xff\xfe not json"
    reader = make_reader([len(body).to_bytes(4, "big") + body])
    with pytest.raises(FramingError):
        await recv_message(reader)


@pytest.mark.asyncio
async def test_oversized_send_rejected_locally():
    """Don't emit a frame the peer is obliged to reject."""
    writer = FakeWriter()
    with pytest.raises(FramingError):
        await send_message(writer, {"x": "y" * (MAX_MESSAGE_BYTES + 1)})
    assert writer.buf == bytearray()


@pytest.mark.asyncio
async def test_incomplete_stream_raises():
    """Truncated payload should raise IncompleteReadError, not hang."""
    writer = FakeWriter()
    await send_message(writer, {"x": "y"})
    data = bytes(writer.buf)
    # feed only first half
    reader = make_reader([data[: len(data) // 2]])
    with pytest.raises(asyncio.IncompleteReadError):
        await recv_message(reader)
