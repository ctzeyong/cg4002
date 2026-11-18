"""Small helpers shared by the relay: rate measurement, a drop-oldest queue put,
and asyncio shutdown filters.
"""

import asyncio
import logging
import warnings

# Report once per second, not every N packets: BLE packets arrive in bursts, so
# counting packets gives jumpy rates.
PROGRESS_INTERVAL_S = 1.0


class RateWindow:
    """Turns packet arrivals into packets/s and kbps, reported once per interval.

    The first packet only starts the clock and is not counted, so connect and
    pairing time never drag the first rate down.
    """

    def __init__(self, interval=PROGRESS_INTERVAL_S):
        self._interval = interval
        self.reset()

    def reset(self):
        """Start over. Call on every new connection, or the outage is counted in the rate."""
        self._anchor = 0.0
        self._packets = 0
        self._bytes = 0

    def tick(self, now, nbytes=0):
        """Fold one packet in.

        Returns:
            (packets_per_second, kbps) when the window closes and the caller
            should log, otherwise None. The window then re-anchors on this
            packet.
        """
        if self._anchor == 0.0:
            self._anchor = now
            return None

        self._packets += 1
        self._bytes += nbytes

        elapsed = now - self._anchor
        if elapsed < self._interval:
            return None

        rate = self._packets / elapsed
        kbps = self._bytes * 8 / 1000 / elapsed
        self._anchor = now  # this packet anchors the next window
        self._packets = 0
        self._bytes = 0
        return rate, kbps


def put_drop_oldest(queue, item):
    """Put `item` on `queue`, evicting oldest entries until it fits.

    Never blocks and never raises, so it is safe from a synchronous callback
    (bleak's notification handler) as well as from a task.

    Returns:
        How many items were discarded to make room, normally 0, so callers can add
        it straight to their own counter.
    """
    dropped = 0
    while queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            break  # unreachable for a sized Queue, but not worth spinning on
        dropped += 1
    queue.put_nowait(item)
    return dropped


_log_listeners = []

log = logging.getLogger("plumbing")


def install_gatt_cancel_error_filter():
    """Hide one harmless traceback bleak prints when a BLE call is cancelled.

    Windows finishes the cancelled GATT call anyway, and bleak then hands its result
    (set_result) or its error (set_exception) to a future that is already cancelled,
    raising InvalidStateError. Seen at shutdown, and mid-run when a board drops during
    a CMD write. Only that exact error is hidden; every other error still reaches the
    old handler. Returns a function that restores it.
    """
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def handler(running_loop, context):
        exc = context.get("exception")
        # Match the callback's text too, so other InvalidStateErrors are not hidden.
        source = f"{context.get('handle', '')} {context.get('message', '')}"
        if isinstance(exc, asyncio.InvalidStateError) and (
            "set_result" in source or "set_exception" in source
        ):
            log.debug("ignored GATT completion after cancel: %s", context.get("message"))
            return
        if previous is None:
            running_loop.default_exception_handler(context)
        else:
            previous(running_loop, context)

    loop.set_exception_handler(handler)
    return lambda: loop.set_exception_handler(previous)


def suppress_queue_get_shutdown_warning():
    """Hide the harmless "Queue.get was never awaited" warning seen on Ctrl-C."""
    warnings.filterwarnings(
        "ignore",
        message=r"coroutine 'Queue\.get' was never awaited",
        category=RuntimeWarning,
    )

