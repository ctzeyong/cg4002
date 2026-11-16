"""Ultra96 relay with the broker on this board: uplink over TCP, events out over MQTT.

Use this or `tcp_client.py` (the laptop-broker setup's client), never both.

    laptop broker  board --TCP--> laptop --MQTT--> broker on the laptop --> phones
    this client    board --MQTT--> broker on this board --> phones

In both, sensor data still comes up from the laptop over TCP. Here the board
publishes its own AI events, so they never go back down the tunnel. Only a VIBRATE for
the player's glove does, after each classified gesture. The TCP side is reused from
`tcp_client.py`.

The board also publishes whether each glove is ready, judged from the SENSOR packets it
receives (`GloveStatus`), so the phones can show a glove that is down.

    python3 tcp_client_mqtt.py --host 127.0.0.1 --port 8888

Needs a broker on this board and `aiomqtt` in the venv.
"""

import argparse
import asyncio
import collections
import logging
import signal
import time

import ai_events
import envelope
import gesture
import mqtt_publisher
from board_logging import DEFAULT_LOG_FILE, setup_logging
from framing import send_message
from tcp_client import (
    CONNECT_TIMEOUT_S,
    RECONNECT_BACKOFF_S,
    TICKER_PLAYER,
    add_gesture_flag,
    event_ticker,
    receiver,
    sender,
    time_field,
)

log = logging.getLogger("tcp_client_mqtt")

# Separate from tcp_client.py's log, so you can tell which client was running.
LOG_FILE = DEFAULT_LOG_FILE.with_name("tcp_client_mqtt.log")

# sent to the laptop after each classified gesture, so it buzzes that player's glove
VIBRATE = "VIBRATE"
VIBRATE_RECOGNISED = 1  # must match VIBRATE_RECOGNISED in the laptop's ble_packet.py

# packets with an IMU reading, counted over the last READY_WINDOW_S
READY_MIN_PACKETS = 50  # half of the glove's 100 Hz
DOWN_BELOW_PACKETS = 25  # lower than ready, so a glove near 50 Hz doesn't flicker
READY_WINDOW_S = 1.0
GLOVE_CHECK_S = 0.25  # how often the gloves' state is checked for a change


class GloveStatus:
    """Judges whether each glove is ready, from when its SENSOR packets reach the board.

    One check covers BLE, the TCP leg and the IMU. It is kept across TCP sessions, so a
    dropped leg reads as silence and both gloves go down.
    """

    def __init__(self):
        self._arrivals = {player: collections.deque() for player in envelope.PLAYERS}
        self._ready = dict.fromkeys(envelope.PLAYERS, False)

    def add(self, msg, now):
        """Count one SENSOR message received at `now` (monotonic), if it has an IMU reading."""
        node = msg.get("node")
        if isinstance(node, bool) or not isinstance(node, int) or node not in self._arrivals:
            return
        if gesture.imu_status(msg) == gesture.IMU_OK:
            self._arrivals[node].append(now)
            self._forget_old(node, now)

    def state(self, now):
        """Return each glove's state, ready or down, as the status/gloves payload.

        Between the two thresholds a glove keeps the state it had at the last call.
        """
        states = {}
        for player, arrivals in self._arrivals.items():
            self._forget_old(player, now)
            count = len(arrivals)
            if count >= READY_MIN_PACKETS:
                self._ready[player] = True
            elif count < DOWN_BELOW_PACKETS:
                self._ready[player] = False
            states[player] = envelope.READY if self._ready[player] else envelope.DOWN
        return envelope.gloves_payload(states)

    def _forget_old(self, player, now):
        arrivals = self._arrivals[player]
        while arrivals and now - arrivals[0] > READY_WINDOW_S:
            arrivals.popleft()


class MqttEventSource:
    """Makes AI events and queues them for MQTT.

    Drop-in for `tcp_client.EventSource`, so `receiver` and `event_ticker` work
    unchanged. Keep `emit`'s signature the same. `writer` is only used to send the
    VIBRATE after a classified gesture.
    """

    def __init__(self, outbound, stats):
        self.outbound = outbound
        self.stats = stats
        self.seq = 0
        self.sent = 0

    async def emit(self, writer, player, key=None, result=None):
        """Make one AI event for `player` and queue it to publish.

        `result` is the classifier's answer. If there is none, the event is a dummy.
        Queuing never blocks or raises, so a broker outage cannot stall the TCP side.
        A classified gesture also sends a VIBRATE for `player` to the laptop.

        Raises:
            OSError, FramingError: sending the VIBRATE failed. `receiver` then ends the session.
        """
        event = ai_events.make_event(player, self.seq, key, result)

        # The player goes in the topic. An event with no player gets a default one.
        player_id = player if player is not None else TICKER_PLAYER
        topic = envelope.gesture_topic(player_id)
        mqtt_publisher.enqueue_event(
            self.outbound, topic, envelope.build_envelope(event, self.seq), self.stats
        )

        self.seq += 1
        self.sent += 1
        # t_event: the event's own timestamp, as in tcp_client.EventSource.
        log.info(
            "AI event seq=%d player=%s key=%r gesture=%s t_event=%s%s -> %s",
            event["seq"],
            event["player"],
            key,
            event["gesture"],
            time_field(event["timestamp"]),
            gesture.log_fields(result),
            topic,
        )
        if result is not None:
            await send_message(
                writer, {"type": VIBRATE, "node": player, "pattern": VIBRATE_RECOGNISED}
            )
            log.info("VIBRATE node=%d pattern=%d sent", player, VIBRATE_RECOGNISED)
        return event


async def watch_gloves(gloves, slot):
    """Hand the gloves' state to the publisher whenever it changes. Runs until cancelled."""
    while True:
        state = gloves.state(time.monotonic())
        if state != slot.state:
            log.info("gloves: %s", " ".join(f"{name}={value}" for name, value in state.items()))
            slot.set(state)
        await asyncio.sleep(GLOVE_CHECK_S)


def stop_on_interrupt(stop):
    """Make Ctrl-C set `stop`, so the publisher marks the gloves offline before it leaves.

    A second Ctrl-C force-quits. Returns a function that restores the old handler.
    """
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)

    def handle(signum, frame):
        signal.signal(signal.SIGINT, previous)  # first, so a second Ctrl-C force-quits
        log.info("interrupt received; shutting down (Ctrl-C again to force)")
        loop.call_soon_threadsafe(stop.set)

    signal.signal(signal.SIGINT, handle)
    return lambda: signal.signal(signal.SIGINT, previous)


async def one_session(
    host, port, events, event_every=0.0, classifier=None, gesture_flag=False, gloves=None
):
    """Run one TCP connection until it ends.

    MQTT is not in here; `run` keeps it separate, so one failing leaves the other up.
    """
    log.info("connecting to %s:%d ...", host, port)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT_S
    )
    log.info("connected")
    stop = asyncio.Event()
    # new per session, so a reconnect starts with no gesture in progress
    windows = None
    if gesture_flag:
        windows = gesture.Windows(classifier or gesture.load_classifier())

    tasks = [
        asyncio.create_task(sender(writer, stop)),
        asyncio.create_task(receiver(reader, writer, stop, events, windows, gloves)),
    ]
    if event_every > 0:
        tasks.append(asyncio.create_task(event_ticker(writer, stop, events, event_every)))
    try:
        # End the session as soon as any task finishes (gather() would wait for all).
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # re-raise the error, if there was one
    finally:
        stop.set()  # always before cancelling
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        log.info("session ended")


async def run_tcp(host, port, outbound, stats, event_every=0.0, gesture_flag=False, gloves=None,
                  vocab=None, bitstream=None, pl_mhz=None):
    """Keep the TCP connection up, reconnecting forever. Same as `tcp_client.run`."""
    # One source for the whole run: a new one would restart seq at 0, and the phone
    # would drop the repeated event_ids as duplicates.
    events = MqttEventSource(outbound, stats)

    classifier = (gesture.load_classifier(vocab=vocab, bitstream=bitstream, pl_mhz=pl_mhz)
                  if gesture_flag else None)
    if pl_mhz is not None and not gesture_flag:
        log.warning("--pl-mhz ignored: without --gesture-flag no overlay is loaded")
    if classifier is not None:
        log.info("classifier: %s", type(classifier).__name__)
    while True:
        try:
            await one_session(
                host,
                port,
                events,
                event_every,
                classifier,
                gesture_flag=gesture_flag,
                gloves=gloves,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            log.warning("connect failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Log and reconnect; an error here must not kill the board client.
            log.exception("unexpected session error")
        log.info("retrying in %.1fs ...", RECONNECT_BACKOFF_S)
        await asyncio.sleep(RECONNECT_BACKOFF_S)


async def run(host, port, broker_host, broker_port, event_every=0.0, gesture_flag=False,
              vocab=None, bitstream=None, pl_mhz=None):
    """Run TCP, the MQTT publisher and the gloves' watcher.

    All three run continuously. Ctrl-C stops the publisher, and the relay stops once the
    publisher has marked the gloves offline.
    """
    outbound = asyncio.Queue(maxsize=mqtt_publisher.OUTBOUND_MAXSIZE)
    stats = mqtt_publisher.PublishStats()
    stop = asyncio.Event()
    gloves = GloveStatus()
    slot = mqtt_publisher.GloveSlot()

    tasks = [
        asyncio.create_task(
            run_tcp(
                host,
                port,
                outbound,
                stats,
                event_every,
                gesture_flag=gesture_flag,
                gloves=gloves,
                vocab=vocab,
                bitstream=bitstream,
                pl_mhz=pl_mhz,
            )
        ),
        asyncio.create_task(
            mqtt_publisher.run_publisher(broker_host, broker_port, outbound, stop, stats, slot)
        ),
        asyncio.create_task(watch_gloves(gloves, slot)),
    ]
    restore_interrupt = stop_on_interrupt(stop)
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        restore_interrupt()
        stop.set()  # always signal to exit before cancelling
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("relay stopped: %s", stats.describe())


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1", help="laptop end of the tunnel")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument(
        "--broker-host",
        default=envelope.LOOPBACK_HOST,
        help="this board's broker (default %(default)s; loopback on purpose)",
    )
    parser.add_argument("--broker-port", type=int, default=envelope.PLAINTEXT_PORT)
    parser.add_argument(
        "--event-every",
        type=float,
        default=0.0,
        help="emit a dummy AI event every N seconds, with no ECHO trigger",
    )
    add_gesture_flag(parser)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    log_file = setup_logging(args.verbose, LOG_FILE)
    log.info("logging to %s", log_file)
    try:
        asyncio.run(
            run(
                args.host,
                args.port,
                args.broker_host,
                args.broker_port,
                args.event_every,
                gesture_flag=args.gesture_flag,
                vocab=args.vocab,
                bitstream=args.bitstream,
                pl_mhz=args.pl_mhz,
            )
        )
    except KeyboardInterrupt:
        log.info("interrupted")


if __name__ == "__main__":
    main()
