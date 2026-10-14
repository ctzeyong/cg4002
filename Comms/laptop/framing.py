"""Length-prefixed JSON messages over an asyncio TCP stream.

Each message is [4-byte big-endian length | that many bytes of UTF-8 JSON].
TCP may split or merge messages, so recv_message reads exactly the length it is given.
Design report sections 4.4.1 and 4.6.1.

This file is duplicated at comms/ultra96/framing.py so each side is
self-contained for deployment. Keep the two copies in sync.
"""

import asyncio
import json
import struct
from typing import Any

LEN_PREFIX_BYTES = 4
MAX_MESSAGE_BYTES = 1_048_576  # 1 MiB cap, so a corrupted length cannot demand a huge read

_HEADER_STRUCT = struct.Struct(">I")


class FramingError(ValueError):
    """Bad length prefix, or a payload that is not valid JSON.

    After this we can no longer find where the next message starts, so the
    caller must close the connection instead of reading again.
    """


async def send_message(writer: asyncio.StreamWriter, obj: Any) -> None:
    """Encode `obj` as JSON and send it as one length-prefixed message.

    Raises:
        FramingError: the encoded message is bigger than MAX_MESSAGE_BYTES.
        OSError: the connection failed.
    """
    payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        # Refuse here rather than send something the other side must reject.
        raise FramingError(f"message of {len(payload)} bytes exceeds cap {MAX_MESSAGE_BYTES}")
    # Header and body in ONE write(), so two tasks sharing this writer cannot mix halves.
    writer.write(_HEADER_STRUCT.pack(len(payload)) + payload)
    await writer.drain()


async def recv_message(reader: asyncio.StreamReader) -> Any:
    """Read exactly one message and return the decoded JSON.

    readexactly() keeps reading until every byte has arrived, however TCP split them.

    Raises:
        FramingError: length over the cap, or the payload is not valid JSON.
            Close the connection after this.
        asyncio.IncompleteReadError: the connection closed partway through a message.
    """
    header = await reader.readexactly(LEN_PREFIX_BYTES)
    (length,) = _HEADER_STRUCT.unpack(header)
    if length > MAX_MESSAGE_BYTES:
        raise FramingError(f"framed length {length} exceeds cap {MAX_MESSAGE_BYTES}")
    payload = await reader.readexactly(length)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FramingError(f"undecodable payload ({length} bytes): {exc}") from exc
