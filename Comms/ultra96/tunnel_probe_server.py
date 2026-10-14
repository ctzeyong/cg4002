"""Loopback-only echo endpoint for the Unity nested-SSH tunnel spike.

Run this on the Ultra96.  The service deliberately binds to 127.0.0.1 so it
cannot bypass the board's port-22-only policy; a successful phone probe must
therefore have travelled through both SSH connections and the final local
forward.
"""

from __future__ import annotations

import argparse
import asyncio
import logging


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18080
MAX_LINE_BYTES = 256
READ_TIMEOUT_S = 5.0
REQUEST_PREFIX = "CG4002_TUNNEL_PROBE "
RESPONSE_PREFIX = "CG4002_TUNNEL_OK "

log = logging.getLogger("tunnel_probe_server")


async def handle_probe(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    peer = writer.get_extra_info("peername")
    try:
        raw = await asyncio.wait_for(
            reader.readline(), timeout=READ_TIMEOUT_S
        )
        if not raw or len(raw) > MAX_LINE_BYTES or not raw.endswith(b"\n"):
            raise ValueError("missing, oversized, or unterminated probe line")

        message = raw.decode("ascii").rstrip("\r\n")
        if not message.startswith(REQUEST_PREFIX):
            raise ValueError("unexpected probe prefix")

        nonce = message[len(REQUEST_PREFIX) :]
        if not nonce or len(nonce) > 64 or not nonce.isalnum():
            raise ValueError("invalid probe nonce")

        writer.write(f"{RESPONSE_PREFIX}{nonce}\n".encode("ascii"))
        await writer.drain()
        log.info("probe ok peer=%s nonce=%s", peer, nonce)
    except (UnicodeDecodeError, ValueError, asyncio.TimeoutError) as exc:
        log.warning("probe rejected peer=%s reason=%s", peer, exc)
        writer.write(b"CG4002_TUNNEL_ERROR\n")
        try:
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


async def run(host: str, port: int) -> None:
    server = await asyncio.start_server(handle_probe, host, port)
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    log.info("loopback tunnel probe listening on %s", addresses)
    async with server:
        await server.serve_forever()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(args.host, args.port))
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
