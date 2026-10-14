"""Shared test plumbing: real loopback servers, connections and polling.

These bind a real socket on an ephemeral port instead of mocking the transport.
Everything that has actually broken on the TCP leg — a reconnecting Ultra96
superseding its own stale session, a peer that goes silent with the socket still
open, a session task dying without telling its sibling — is transport behaviour
that a mock would have to fake, and a faked version of a bug is not evidence the
real one is fixed.
"""

import asyncio
import contextlib
import json
import struct

# Every await in the suite is wrapped in a deadline. A regression on this leg
# presents as a hang, not an exception, so an unbounded await would turn a
# failing test into a stuck suite.
DEADLINE_S = 2.0


@contextlib.asynccontextmanager
async def serving(handler):
    """Run `handler` on an ephemeral loopback port for the duration of the block.

    Port 0 rather than 8888: the tests must not collide with a real server the
    developer happens to have running, nor with each other.
    """
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.close()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server.wait_closed(), timeout=DEADLINE_S)


@contextlib.asynccontextmanager
async def connected(port):
    """Open a client connection to `port` and guarantee it is closed after."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        yield reader, writer
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def wait_until(predicate, timeout=DEADLINE_S):
    """Poll `predicate` until it is true, or fail the test.

    Beats a fixed sleep in both directions: no arbitrary delay on a fast
    machine, no flake on a loaded one.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(f"condition still false after {timeout:.1f}s")
        await asyncio.sleep(0.01)


def frame(obj):
    """Encode `obj` the way send_message would, for tests that need raw bytes."""
    payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(payload)) + payload
