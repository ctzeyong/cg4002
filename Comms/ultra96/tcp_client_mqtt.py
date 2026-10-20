"""Ultra96 relay for configuration B: uplink over TCP, events out over MQTT.

Use this or `tcp_client.py` (configuration A), never both.

    A   board --TCP--> laptop --MQTT--> broker on the laptop --> phones
    B   board --MQTT--> broker on this board --> phones

In both, sensor data still comes up from the laptop over TCP. Under B the board
publishes its own AI events, so they never go back down the tunnel. Only a VIBRATE for
the player's glove does, after each classified gesture. The TCP side is reused from
`tcp_client.py` unchanged.

    python3 tcp_client_mqtt.py --host 127.0.0.1 --port 8888

Needs a broker on this board and `aiomqtt` in the venv.
"""

import argparse
import asyncio
import logging

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
    SensorTrigger,
    add_sensor_triggers,
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


async def one_session(
    host, port, events, event_every=0.0, sensor_every=0, classifier=None, gesture_flag=False
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
    sensor_trigger = SensorTrigger(sensor_every) if sensor_every > 0 else None
    # new per session, so a reconnect starts with empty gesture windows
    windows = None
    if sensor_every > 0 or gesture_flag:
        windows = gesture.Windows(
            classifier or gesture.load_classifier(gesture_flag), per_sample=gesture_flag
        )

    tasks = [
        asyncio.create_task(sender(writer, stop)),
        asyncio.create_task(receiver(reader, writer, stop, events, sensor_trigger, windows)),
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


async def run_tcp(
    host, port, outbound, stats, event_every=0.0, sensor_every=0, gesture_flag=False
):
    """Keep the TCP connection up, reconnecting forever. Same as `tcp_client.run`."""
    # One source for the whole run: a new one would restart seq at 0, and the phone
    # would drop the repeated event_ids as duplicates.
    events = MqttEventSource(outbound, stats)

    classifier = gesture.load_classifier(gesture_flag) if sensor_every > 0 or gesture_flag else None
    if classifier is not None:
        log.info("classifier: %s", type(classifier).__name__)
    while True:
        try:
            await one_session(
                host,
                port,
                events,
                event_every,
                sensor_every,
                classifier,
                gesture_flag=gesture_flag,
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


async def run(
    host, port, broker_host, broker_port, event_every=0.0, sensor_every=0, gesture_flag=False
):
    """Run TCP and the MQTT publisher.

    Both should run continuously.
    """
    outbound = asyncio.Queue(maxsize=mqtt_publisher.OUTBOUND_MAXSIZE)
    stats = mqtt_publisher.PublishStats()
    stop = asyncio.Event()

    tasks = [
        asyncio.create_task(
            run_tcp(
                host,
                port,
                outbound,
                stats,
                event_every,
                sensor_every,
                gesture_flag=gesture_flag,
            )
        ),
        asyncio.create_task(
            mqtt_publisher.run_publisher(broker_host, broker_port, outbound, stop, stats)
        ),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
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
    add_sensor_triggers(parser)
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
                args.sensor_trigger,
                gesture_flag=args.gesture_flag,
            )
        )
    except KeyboardInterrupt:
        log.info("interrupted")


if __name__ == "__main__":
    main()
