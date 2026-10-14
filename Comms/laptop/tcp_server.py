"""Laptop-side TCP server. Accepts one client (the Ultra96) and carries both
directions of the Ultra96 leg.

  - **uplink**: BLE-sourced packets are pushed out through `send_to_current`,
    which the pipeline's forwarder calls as it drains the BLE queue.
  - **downlink**: whatever the Ultra96 sends back goes onto a queue for the MQTT
    bridge, except HEARTBEATs, which are answered here.

The protocol decisions behind this file (framing, the heartbeat contract, the
one-client rule, why the uplink does not buffer) are in notes/tcp_leg.md.

Run standalone:

    python tcp_server.py

which logs whatever arrives instead of publishing it.
"""

import argparse
import asyncio
import functools
import logging
import time
from pathlib import Path
from typing import Any, Optional

from framing import FramingError, recv_message, send_message
from logging_setup import PROGRESS, setup_logging
from plumbing import put_drop_oldest

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8888
HEARTBEAT_TIMEOUT_S = 5.0  # if nothing arrives in this long, drop the client
DOWNLINK_MAXSIZE = 256  # AI events are event-paced, not stream-paced
PROGRESS_EVERY = 10  # placeholder until the real packet progress bar lands

# Two FireBeetles at 100 Hz is 200 sends/s, so the budget is ~5 ms per send and
# anything at or above this is backpressure rather than jitter. See notes/tcp_leg.md
# for what the timing distinguishes.
SLOW_SEND_MS = 50.0
# A sustained stall at 200 sends/s would emit 200 warnings a second, and the logging
# would then be part of what it is measuring. Summarise instead.
SLOW_SEND_REPORT_S = 1.0

LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "tcp_server.log"

log = logging.getLogger("tcp_server")

# Stores the TCP connection to the currently connected Ultra96.
# The connection is shared by the asyncio tasks running in this event loop.
# If the Ultra96 reconnects, the new connection replaces the old one.
# This gives the BLE-to-TCP forwarder a single connection to send data to.
_current: Optional[asyncio.StreamWriter] = None

# Slow-send accounting, reset each time it is reported. Same no-lock reasoning.
_slow_sends = 0
_slow_worst_ms = 0.0
_slow_last_report = 0.0


async def send_to_current(obj: Any) -> bool:
    """Send one message to the connected Ultra96, if there is one.

    Messages are sent immediately without buffering. Send duration is monitored
    to detect slow TCP writes.

    Returns:
        True if the frame went out; False if no client is connected or the write
        failed. The caller counts the False cases: a link that is down is normal
        operation here, not an error.
    """
    global _slow_sends, _slow_worst_ms, _slow_last_report

    writer = _current
    if writer is None or writer.is_closing():
        return False

    started = time.perf_counter()
    try:
        await send_message(writer, obj)
    except (OSError, FramingError) as exc:
        log.warning("uplink send failed: %s", exc)
        return False
    elapsed_ms = (time.perf_counter() - started) * 1000

    if elapsed_ms >= SLOW_SEND_MS:
        _slow_sends += 1
        _slow_worst_ms = max(_slow_worst_ms, elapsed_ms)
        now = time.monotonic()
        if now - _slow_last_report >= SLOW_SEND_REPORT_S:
            _slow_last_report = now
            log.warning(
                "slow TCP uplink send: %d in the last %.0fs, worst %.1f ms, buffered=%d",
                _slow_sends,
                SLOW_SEND_REPORT_S,
                _slow_worst_ms,
                _write_buffer_size(writer),
            )
            _slow_sends = 0
            _slow_worst_ms = 0.0
    return True


def _write_buffer_size(writer: asyncio.StreamWriter) -> int:
    """Bytes queued in the transport, or -1 if the transport won't say.

    Rising buffer means the peer is not reading fast enough, while a slow send
    against an *empty* buffer means the delay was somewhere else in the loop.
    """
    try:
        return writer.transport.get_write_buffer_size()
    except (AttributeError, NotImplementedError):
        return -1


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    downlink: asyncio.Queue,
) -> None:
    """Serve one client connection until it goes quiet, errors, or hangs up.

    Every frame either is a HEARTBEAT, which is acknowledged and goes no further,
    or is an Ultra96 response, which goes onto `downlink` for the MQTT bridge. A
    new connection supersedes the previous one, so a reconnecting Ultra96 evicts
    its own stale session instead of the server waiting out HEARTBEAT_TIMEOUT_S.
    """
    global _current
    peer = writer.get_extra_info("peername")
    if _current is not None and not _current.is_closing():
        log.warning("new client %s supersedes %s", peer, _current.get_extra_info("peername"))
        _current.close()
    _current = writer
    log.info("client connected: %s", peer)
    count = 0  # frames received, heartbeats included
    heartbeats = 0  # answered here, not forwarded
    forwarded = 0  # handed on to the MQTT bridge
    dropped = 0  # shed because the downlink queue was full
    try:
        while True:
            try:
                msg = await asyncio.wait_for(recv_message(reader), timeout=HEARTBEAT_TIMEOUT_S)
            # TimeoutError must precede OSError: on Python 3.11+ asyncio.TimeoutError
            # IS the builtin TimeoutError, which subclasses OSError.
            except asyncio.TimeoutError:
                log.warning("no message in %.1fs; dropping client %s", HEARTBEAT_TIMEOUT_S, peer)
                break
            except asyncio.IncompleteReadError:
                log.info("client %s closed cleanly", peer)
                break
            except FramingError as exc:
                log.error("protocol error from %s: %s; dropping client", peer, exc)
                break
            except OSError as exc:
                log.warning("transport error from %s: %s", peer, exc)
                break
            count += 1
            log.debug("recv from %s: %s", peer, msg)

            # A frame that is not a JSON object cannot be one of ours, but it is
            # still valid JSON and the envelope's payload is untyped by design, so
            # forward it rather than call it a protocol error. Only a framing error
            # desynchronises the stream and ends the connection.
            if isinstance(msg, dict) and msg.get("type") == "HEARTBEAT":
                heartbeats += 1
                # t1 echoes the board's send time and t2 is ours, so the board can
                # estimate the clock offset. notes/tcp_leg.md -> Heartbeats.
                ack = {
                    "type": "HEARTBEAT_ACK",
                    "seq": msg.get("seq"),
                    "t1": msg.get("ts"),
                    "t2": time.perf_counter(),
                }
                try:
                    await send_message(writer, ack)
                except (OSError, FramingError) as exc:
                    log.warning("heartbeat ack to %s failed: %s", peer, exc)
                    break
            else:
                shed = put_drop_oldest(downlink, msg)
                if shed:
                    dropped += shed
                    log.warning("downlink queue full; dropped %d oldest", shed)
                forwarded += 1

            if count % PROGRESS_EVERY == 0:
                # heartbeats show the board link is still alive 
                log.info(
                    "board link: %d frames received (heartbeats=%d forwarded=%d dropped=%d)",
                    count,
                    heartbeats,
                    forwarded,
                    dropped,
                    extra=PROGRESS,  # a periodic counter: dimmed on the console
                )
    except Exception:
        log.exception("handler error for %s", peer)
    finally:
        if _current is writer:
            _current = None
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        log.info(
            "client disconnected: %s (%d frames: heartbeats=%d forwarded=%d dropped=%d)",
            peer,
            count,
            heartbeats,
            forwarded,
            dropped,
        )


async def run(host: str, port: int, downlink: asyncio.Queue) -> None:
    """Start the TCP server and handle connections from Ultra96.

    The server listens locally because the Ultra96 connects through an
    SSH reverse tunnel. Responses received from the Ultra96 are placed
    into the downlink queue.
    """
    server = await asyncio.start_server(
        functools.partial(handle_client, downlink=downlink), host, port
    )
    for sock in server.sockets or []:
        log.info("listening on %s", sock.getsockname())
    async with server:
        await server.serve_forever()


async def drain_downlink(downlink: asyncio.Queue, stop: asyncio.Event) -> None:
    """Standalone test: log whatever the Ultra96 sends back.

    Stands in for pipeline.forward_downlink, which wraps these in the MQTT
    envelope and publishes them to the visualiser.
    """
    while not stop.is_set():
        try:
            msg = await asyncio.wait_for(downlink.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        log.info("downlink: %s", msg)


async def run_standalone(host: str, port: int) -> None:
    """Wire run() to the standalone drain and run until cancelled."""
    downlink: asyncio.Queue = asyncio.Queue(maxsize=DOWNLINK_MAXSIZE)
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(run(host, port, downlink)),
        asyncio.create_task(drain_downlink(downlink, stop)),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    """CLI entry point: parse args, set log level, serve until Ctrl-C.

    `--verbose` adds the per-frame dump; see notes/logging.md for the levels.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("-v", "--verbose", action="store_true", help="log every packet")
    args = parser.parse_args()

    log_file = setup_logging(LOG_FILE, args.verbose)
    log.info("logging to %s", log_file)

    try:
        asyncio.run(run_standalone(args.host, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
