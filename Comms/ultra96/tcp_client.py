"""Ultra96-side TCP client. Connects to the laptop, sends heartbeats, answers
BLE packets with AI events.

Run on the same machine as tcp_server.py for a loopback check, or on the Ultra96
with the laptop reachable via the SSH reverse tunnel (the client dials
localhost:8888 on the Ultra96 -> forwarded to laptop 8888).

This is the far end of criterion 5's pipeline. A keystroke on the laptop becomes
a CMD to a FireBeetle, the board negates the fields and returns them as an ECHO,
the laptop forwards it here, and this generates one random AI event and sends it
back for the laptop to publish to the visualiser.

`--gesture-flag` also passes every SENSOR sample to AI, which classifies each gesture the
glove flags.

Auto-reconnects on any disconnect, per Comms Initial Design Report Table 4.3.
The protocol decisions behind this file are in notes/tcp_leg.md.
"""

import argparse
import asyncio
import logging
import time

import ai_events
import gesture
from framing import FramingError, recv_message, send_message
from board_logging import PROGRESS, setup_logging

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8888
HEARTBEAT_INTERVAL_S = 1.0
MISSED_HEARTBEAT_LIMIT = 3  # reconnect after this many silent intervals
RECV_TIMEOUT_S = HEARTBEAT_INTERVAL_S * MISSED_HEARTBEAT_LIMIT
RECONNECT_BACKOFF_S = 2.0
CONNECT_TIMEOUT_S = 10.0
# Report received throughput on a time interval, not a message count. At 200 msg/s
# a count-based report fires ~20x/s, and console logging on the board is then part
# of what is being measured.
PROGRESS_INTERVAL_S = 1.0

# Which uplink packet earns an AI event. ECHO is the keystroke-caused packet, so
# one keypress produces one event the examiner can follow end to end; firing on the
# SENSOR stream instead would bury it. See notes/tcp_leg.md.
AI_EVENT_TRIGGER = "ECHO"
HEARTBEAT_ACK = "HEARTBEAT_ACK"  # the laptop's reply; liveness, not uplink traffic
SENSOR = "SENSOR"  # passed to AI with --gesture-flag, and a trigger only when AI answers

TICKER_PLAYER = 1  # --event-every has no packet to attribute to; pick one

# Keep the best (lowest-RTT) heartbeat sample from this long ago. notes/latency.md 3.3.
OFFSET_WINDOW_S = 30.0

log = logging.getLogger("tcp_client")


def _is_int(value):
    """True for a real integer. bool is a subclass of int, so it is ruled out."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def time_field(value):
    """A time for a log field: six decimals, or `-` if there is no usable number."""
    return f"{value:.6f}" if _is_number(value) else "-"


class ClockOffset:
    """Board clock minus the laptop's `perf_counter`, from the heartbeat round trip.

    t1 = board sends HEARTBEAT, t2 = laptop answers, t3 = board receives the ACK.
    offset = (t1 + t3)/2 - t2, wrong by at most rtt/2. The lowest-RTT sample in the
    last OFFSET_WINDOW_S is kept, as the least delayed. notes/latency.md 3.3.
    """

    def __init__(self, window_s=OFFSET_WINDOW_S):
        self.window_s = window_s
        self._samples = []  # (monotonic time, offset, rtt)

    def add(self, t1, t2, t3, now=None):
        """Add one ACK's times. An ACK from an older laptop has no t1/t2: ignored."""
        if not (_is_number(t1) and _is_number(t2) and _is_number(t3)):
            return
        rtt = t3 - t1
        if rtt < 0:
            return  # the board's clock stepped back between send and receive
        now = time.monotonic() if now is None else now
        self._samples.append((now, (t1 + t3) / 2 - t2, rtt))
        self._samples = [s for s in self._samples if now - s[0] <= self.window_s]

    def best(self):
        """`(offset, rtt)` of the lowest-RTT sample, or None before the first ACK."""
        if not self._samples:
            return None
        _, offset, rtt = min(self._samples, key=lambda s: s[2])
        return offset, rtt

    def describe(self):
        best = self.best()
        if best is None:
            return "offset=- rtt=-"
        offset, rtt = best
        return f"offset={offset:.6f} rtt={rtt * 1000:.3f}ms"


def parse_echo(msg):
    """Return `(player, key)` from an ECHO. abs() undoes the FireBeetle's negation.

    `key` is None if field 0 is not a character (e.g. the dummy data was zeroed);
    the event still happens, with a random gesture. See notes/tcp_leg.md.

    Raises:
        ValueError: the message is malformed; the text says what is wrong.
    """
    node = msg.get("node")
    if not _is_int(node):
        raise ValueError(f"node is {node!r:.40}, not an integer")
    fields = msg.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ValueError("fields missing or empty")
    if not _is_int(fields[0]):
        raise ValueError(f"fields[0] is {fields[0]!r:.40}, not an integer")
    code = abs(fields[0])
    return node, chr(code) if 0 < code <= 0x10FFFF else None

class EventSource:
    """Issues AI events, numbering them from a single sequence.

    One counter shared by the ECHO-triggered path and the --event-every stand-in,
    so the visualiser sees one ordered stream rather than two interleaved ones.
    No lock: one event loop, and both callers run in it.
    """

    def __init__(self):
        self.seq = 0
        self.sent = 0

    async def emit(self, writer, player, key=None, result=None):
        """Build one AI event for `player` and send it to the laptop.

        `result` is the classifier's answer. If there is none, the event is a dummy and
        `key` (if any) picks the gesture.

        Raises:
            OSError: the transport failed.
            FramingError: the event would not serialise. Neither is handled here;
                the caller ends the session and `run` re-dials.
        """
        event = ai_events.make_event(player, self.seq, key, result)
        await send_message(writer, event)
        self.seq += 1
        self.sent += 1
        # t_event is the event's own timestamp, not this line's time: this line comes
        # after the TCP send, B's after a queue put. notes/latency.md 3.2.
        log.info(
            "AI event seq=%d player=%s key=%r gesture=%s t_event=%s%s",
            event["seq"],
            event["player"],
            key,
            event["gesture"],
            time_field(event["timestamp"]),
            gesture.log_fields(result),
        )
        return event


async def sender(writer: asyncio.StreamWriter, stop: asyncio.Event) -> None:
    """Emit a HEARTBEAT frame every HEARTBEAT_INTERVAL_S until `stop` is set.

    Does double duty: dummy traffic for the loopback, and the liveness signal the
    server times out on. Any send failure sets `stop` so the session unwinds and
    `run` re-dials rather than looping on a dead writer.
    """
    seq = 0
    while not stop.is_set():
        try:
            await send_message(writer, {"type": "HEARTBEAT", "seq": seq, "ts": time.time()})
        except FramingError as exc:
            log.error("refusing to send malformed message: %s", exc)
            stop.set()
            return
        except OSError as exc:
            log.warning("send failed (%s); signalling reconnect", exc)
            stop.set()
            return
        seq += 1
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_S)
        except asyncio.TimeoutError:
            pass


async def receiver(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    stop: asyncio.Event,
    events: EventSource,
    windows: gesture.Windows = None,
    gloves=None,
) -> None:
    """Receive messages from the laptop and handle AI event triggers.

    Heartbeat ACKS keep the connection alive. If no message is received 
    within the timeout period, the connection is considered lost and a 
    reconnect is requested. AI event triggers are processed and the
    resulting events are sent back to the laptop.

    An ECHO is always a trigger and gets a dummy gesture. With `windows`, every SENSOR
    packet goes to AI, and a packet is a trigger when AI answers with a gesture. With
    `gloves` (configuration B's GloveStatus), every SENSOR packet is also counted there.
    """
    count = 0
    window_count = 0
    window_started = time.monotonic()
    clock = ClockOffset()

    while not stop.is_set():
        try:
            msg = await asyncio.wait_for(recv_message(reader), timeout=RECV_TIMEOUT_S)
        # TimeoutError is a subclass of OSError so must be handled earlier
        except asyncio.TimeoutError:
            log.warning(
                "no message in %.1fs (%d missed heartbeats); reconnecting",
                RECV_TIMEOUT_S,
                MISSED_HEARTBEAT_LIMIT,
            )
            stop.set()
            return
        except FramingError as exc:
            log.error("protocol error: %s; dropping connection", exc)
            stop.set()
            return
        except (asyncio.IncompleteReadError, OSError):
            log.warning("server closed the connection")
            stop.set()
            return
        # Board clock: t3 for an ACK, and hop 3 for a trigger. notes/latency.md 3.2.
        received_at = time.time()

        # heartbeats confirm the connection is alive but are excluded from
        # the message rate calculation. Continue processing so the rate is
        # still reported during periods with no uplink traffic.
        if isinstance(msg, dict) and msg.get("type") == HEARTBEAT_ACK:
            clock.add(msg.get("t1"), msg.get("t2"), received_at)
        else:
            count += 1
            window_count += 1
        log.debug("recv: %s", msg)

        # periodically report incoming message rate and total AI event count
        now = time.monotonic()
        elapsed = now - window_started
        if elapsed >= PROGRESS_INTERVAL_S:
            log.info(
                "uplink rx: total=%d rate=%.1f msg/s, %d AI events sent, %s",
                count,
                window_count / elapsed,
                events.sent,
                clock.describe(),
                extra=PROGRESS,
            )
            window_count = 0
            window_started = now

        if not isinstance(msg, dict):
            continue
        if gloves is not None and msg.get("type") == SENSOR:
            gloves.add(msg, now)
        result = None
        if windows is not None and msg.get("type") == SENSOR:
            try:
                result = windows.feed(msg)
            except gesture.ClassifierError as exc:
                log.warning(
                    "no AI event: classifier failed on node %s (%s)", msg.get("node"), exc
                )
                continue
        if result is not None:
            player, key = msg["node"], None
        elif msg.get("type") == AI_EVENT_TRIGGER:
            try:
                player, key = parse_echo(msg)
            except ValueError as exc:
                log.warning("dropping malformed ECHO seq=%.40r: %s", msg.get("seq"), exc)
                continue
        else:
            continue
        # Logged here, not in emit, so A and B share it. The AI code should log the same line.
        seq = msg.get("seq")
        log.info(
            "trigger node=%d seq=%s t_laptop_rx=%s t_rx=%s",
            player,
            seq if _is_int(seq) else "-",
            time_field(msg.get("timestamp")),
            time_field(received_at),
        )
        try:
            await events.emit(writer, player, key, result)
        except (OSError, FramingError) as exc:
            log.warning("AI event send failed (%s); signalling reconnect", exc)
            stop.set()
            return


async def event_ticker(
    writer: asyncio.StreamWriter,
    stop: asyncio.Event,
    events: EventSource,
    interval: float,
) -> None:
    """Standalone test: emit an AI event every `interval` seconds. Off by default.

    Stands in for the ECHO trigger, like --cmd-every and --publish-every on the
    laptop: it makes the whole Ultra96 -> visualiser leg demonstrable on loopback
    with no FireBeetle on the desk. Not used in the live demo, where the trigger is
    a real keystroke.
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        else:
            return  # stop was set rather than the interval elapsing

        try:
            await events.emit(writer, TICKER_PLAYER)
        except (OSError, FramingError) as exc:
            log.warning("AI event send failed (%s); signalling reconnect", exc)
            stop.set()
            return


async def one_session(
    host: str,
    port: int,
    event_every: float = 0.0,
    classifier=None,
    gesture_flag: bool = False,
) -> None:
    """Run one TCP connection start to finish: dial, pump heartbeats, tear down.

    Starts the sender and receiver tasks and cleans them up when the session
    ends. The caller is responsible for reconnecting after a failure.

    Raises:
        OSError: the connection could not be established, or a session task
            failed with one.
        asyncio.TimeoutError: dialling exceeded CONNECT_TIMEOUT_S.
        Exception: whatever a session task died of, re-raised for `run` to log.
    """
    log.info("connecting to %s:%d ...", host, port)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT_S
    )
    log.info("connected")
    stop = asyncio.Event()
    # Fresh per session, so a reconnect restarts the AI event sequence
    events = EventSource()
    # new per session, so a reconnect starts with no gesture in progress
    windows = None
    if gesture_flag:
        windows = gesture.Windows(classifier or gesture.load_classifier())
    tasks = [
        asyncio.create_task(sender(writer, stop)),
        asyncio.create_task(receiver(reader, writer, stop, events, windows)),
    ]
    if event_every > 0:
        tasks.append(asyncio.create_task(event_ticker(writer, stop, events, event_every)))
    try:
        # end the session when either task ends, which prevents a failed task from
        # leaving its sibling running indefinitely
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # re-raise the real cause, if that is why we woke
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        log.info("session ended")


async def run(
    host: str,
    port: int,
    event_every: float = 0.0,
    gesture_flag: bool = False,
    ai_h5=None,
) -> None:
    """Maintain the Ultra96 TCP connection, reconnecting automatically after failures,
    waiting RECONNECT_BACKOFF_S between attempts.
    """
    use_flag = gesture_flag or bool(ai_h5)  # a Keras model always runs on the flag path
    classifier = gesture.load_classifier(ai_h5=ai_h5) if use_flag else None
    if classifier is not None:
        log.info("classifier: %s", type(classifier).__name__)
    while True:
        try:
            await one_session(host, port, event_every, classifier, gesture_flag=use_flag)
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("connect failed: %s", exc)
        except Exception:
            log.exception("unexpected session error")
        log.info("retrying in %.1fs ...", RECONNECT_BACKOFF_S)
        await asyncio.sleep(RECONNECT_BACKOFF_S)


def add_gesture_flag(parser):
    """Add --gesture-flag, which A and B share."""
    parser.add_argument(
        "--gesture-flag",
        action="store_true",
        help="pass every SENSOR packet to AI and emit an AI event for each gesture the "
        "glove flags (default: off)",
    )


def main() -> None:
    """CLI entry point: parse args, set log level, run until Ctrl-C.

    `--verbose` adds the per-frame dump; see notes/logging.md for the levels.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("-v", "--verbose", action="store_true", help="log every packet")
    parser.add_argument(
        "--event-every",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="standalone test: emit an AI event this often instead of waiting for an "
        "ECHO from a FireBeetle (default: off)",
    )
    parser.add_argument(
        "--ai-h5",
        default=None,
        metavar="PATH",
        help="local test: run a Keras .h5 model instead of the FPGA bitstream (implies "
        "--gesture-flag). See AI/board/keras_stream.py.",
    )
    add_gesture_flag(parser)
    args = parser.parse_args()
    log_file = setup_logging(args.verbose)
    log.info("logging to %s", log_file)
    try:
        asyncio.run(
            run(
                args.host,
                args.port,
                args.event_every,
                gesture_flag=args.gesture_flag,
                ai_h5=args.ai_h5,
            )
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
