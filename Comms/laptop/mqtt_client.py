"""Laptop-side MQTT relay. One supervised session carrying both game topics.

Publishes AI gesture events to `game/gesture/<player>` for the phone visualisers,
and subscribes to `viz/ready/+` so a phone announcing itself shows up during
bring-up. The topic contract, including which topics have producers today, is in
notes/mqtt_topics.md.

Both directions share one broker connection and one supervisor, the same shape as
ble_client.run_node: connect, run, back off, retry, forever. The broker address
arrives as a BrokerConfig, so nothing here has to know where the broker is.

Messages are a JSON envelope; see build_envelope.

Run standalone to exercise the leg without the rest of the pipeline:

    python mqtt_client.py --publish-every 1.0

which publishes a dummy gesture event every second and logs anything arriving on
`viz/ready/+`, the role --cmd-every and drain() play in ble_client.
"""

import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import aiomqtt

from mqtt_config import (
    CLIENT_ID_LAPTOP_RELAY,
    GOOD_RUN_S,
    KEEPALIVE_S,
    MAX_BACKOFF_S,
    MIN_BACKOFF_S,
    QOS_GAME_GESTURE,
    QOS_VIZ_READY,
    TOPIC_VIZ_READY_FILTER,
    gesture_topic,
    player_from_topic,
    non_negative_float,
    port_number,
    resolve_broker,
    tls_params,
)
from logging_setup import setup_logging
from plumbing import put_drop_oldest

OUTBOUND_MAXSIZE = 256  # game events are event-paced, not stream-paced
INBOUND_MAXSIZE = 256
SEEN_CAPACITY = 4096  # ~an hour of events at the rates this leg carries

LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "mqtt_client.log"

log = logging.getLogger("mqtt_client")

# Fresh per process. event_id is "<source>:<session>:<seq>", so a relay restart
# starts its seq at 0 again without colliding with ids the phone has already
# seen and would otherwise discard as duplicates.
SESSION_ID = uuid.uuid4().hex[:8]


# Envelope fields that carry a type contract, as (name, accepted types, wording
# for the error). `payload` is checked separately: required, but untyped on
# purpose.
_ENVELOPE_FIELDS = (
    ("event_id", str, "a string"),
    ("source", str, "a string"),
    ("seq", int, "an integer"),
    ("ts", (int, float), "a number"),
)


class EnvelopeError(ValueError):
    """A payload that arrived on a game topic but is not a valid envelope.

    MQTT preserves message boundaries, so one bad message says nothing about the
    next one: log it, drop that message, keep the session. The TCP leg is the
    opposite, where a framing error desynchronises the stream and the connection
    has to go.
    """


def build_envelope(payload, seq, source=CLIENT_ID_LAPTOP_RELAY, session=SESSION_ID):
    """Wrap a game payload in the envelope every message on this leg carries.

    `event_id` is the source, the process generation and the position in the
    stream, e.g. "laptop-relay:3f2a1b9c:17".
    """
    return {
        "event_id": f"{source}:{session}:{seq}",
        "source": source,
        "seq": seq,
        "ts": time.time(),  # wall clock: this is displayed, not used for durations
        "payload": payload,
    }


def parse_envelope(raw):
    """Decode and validate one received MQTT payload.

    Args:
        raw: the bytes from `message.payload`.

    Returns:
        The envelope as a dict.

    Raises:
        EnvelopeError: not UTF-8 JSON, not an object, or a field that is
            missing, empty or of the wrong type.
    """
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnvelopeError(f"undecodable payload ({len(raw)} bytes): {exc}") from exc

    if not isinstance(obj, dict):
        raise EnvelopeError(f"envelope must be a JSON object, got {type(obj).__name__}")

    for key, types, described in _ENVELOPE_FIELDS:
        if key not in obj:
            raise EnvelopeError(f"envelope missing {key!r}")
        value = obj[key]
        # bool is a subclass of int, so `"seq": true` passes isinstance(_, int).
        # Reject it explicitly or a JSON boolean reads as a valid sequence number.
        if isinstance(value, bool) or not isinstance(value, types):
            raise EnvelopeError(f"{key} must be {described}, got {type(value).__name__}")

    # Presence only. payload is opaque to this layer by design, so it is not typed.
    if "payload" not in obj:
        raise EnvelopeError("envelope missing 'payload'")

    if not obj["event_id"]:
        raise EnvelopeError("event_id must not be empty")

    return obj


class SeenEvents:
    """Bounded set of event_ids already handled, for QoS 1 dedup.

    QoS 1 is at-least-once: a redelivery after a reconnect arrives with the same
    event_id, and without this the visualiser would render it twice.

    Bounded because an unbounded set is a slow leak over a demo-length run.
    Eviction is oldest-first, which suits a stream where a redelivery follows
    closely behind the original. A duplicate arriving more than SEEN_CAPACITY
    events later would slip through, but by then the broker has long since given
    up on it.
    """

    def __init__(self, capacity=SEEN_CAPACITY):
        self._capacity = capacity
        self._order = []
        self._ids = set()

    def __len__(self):
        return len(self._ids)

    def check(self, event_id):
        """Record `event_id` and report whether it is new.

        Returns True the first time an id is seen and False for a duplicate, so
        callers read as `if seen.check(id): handle(...)`.
        """
        if event_id in self._ids:
            return False
        self._ids.add(event_id)
        self._order.append(event_id)
        if len(self._order) > self._capacity:
            self._ids.discard(self._order.pop(0))
        return True


@dataclass
class MqttStats:
    """Counters for one relay, reported when the supervisor stops.

    No lock: like the rest of the relay this is one event loop, so the publish
    and subscribe tasks never touch these concurrently.
    """

    published: int = 0
    publish_failed: int = 0
    bad_payload: int = 0  # outbound envelope that would not serialise; dropped
    received: int = 0
    duplicates: int = 0
    bad_envelope: int = 0
    dropped: int = 0  # inbound queue full: the consumer is not keeping up
    sessions: int = 0
    reconnects: int = 0


def enqueue_event(outbound, topic, envelope, stats):
    """Post an envelope for publication, dropping the oldest if the queue is full.

    The *producer* names the topic, so `game/gesture/{player}` gets its player id
    from whoever built the event. That keeps this relay indifferent to the game
    schema.

    Drops the *oldest* rather than the newest: if the broker link has been down
    long enough to fill this queue, the stalest game state is the least worth
    sending, and a reconnecting visualiser wants the current state.
    """
    stats.dropped += put_drop_oldest(outbound, (topic, envelope))


async def _publish_loop(client, outbound, pending, stats, stop):
    """Publish queued `(topic, envelope)` items until the session ends or `stop` is set.

    `pending` is the single-slot holder for the envelope in flight. An envelope
    stays there until the broker has taken it, so a failed publish is retried at
    the head of the next session. run_relay owns it and documents why.

    Waits on the queue with a timeout rather than a plain `get()` so a stop set
    while the queue is idle is noticed promptly instead of on the next event.
    """
    while not stop.is_set():
        if not pending:
            try:
                pending.append(await asyncio.wait_for(outbound.get(), timeout=1.0))
            except asyncio.TimeoutError:
                continue

        topic, envelope = pending[0]
        try:
            payload = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            # Drop it, do NOT retry. The opposite call from the MqttError below,
            # because a serialisation failure is deterministic: the same envelope
            # fails the same way on every reconnect, forever. Left in `pending` it
            # is a poison pill that blocks every later event. The full argument is
            # in notes/shutdown_and_ordering.md.
            identifier = (
                envelope.get("event_id", "<no id>")
                if isinstance(envelope, dict)
                else f"<{type(envelope).__name__}>"
            )
            stats.bad_payload += 1
            log.error("dropping unserialisable envelope %s: %s", identifier, exc)
            pending.clear()
            continue

        try:
            await client.publish(topic, payload=payload, qos=QOS_GAME_GESTURE)
        except aiomqtt.MqttError:
            # Leave it in `pending` and surface the failure. The supervisor
            # reconnects and this envelope goes out first on the new session,
            # ahead of anything queued behind it.
            stats.publish_failed += 1
            raise
        pending.clear()
        stats.published += 1
        # INFO: once per game event. `.get` because a KeyError here would be a poison
        # pill -- the supervisor would republish and re-raise forever. `t=` is the
        # PUBACK time for scripts/tools/latency_report.py; notes/latency.md.
        log.info(
            "published %s to %s t=%.6f",
            envelope.get("event_id", "<no id>"),
            topic,
            time.perf_counter(),
        )


async def _subscribe_loop(client, inbound, seen, stats, stop):
    """Feed validated, de-duplicated `viz/ready/<player>` messages onto `inbound`.

    Items go onto `inbound` as `(player, envelope)`, mirroring the outbound queue's
    `(topic, envelope)`, so the envelope stays a fixed contract that gains no
    fields on the way through. The player comes off the topic rather than the
    payload; see mqtt_config.player_from_topic.

    A bad envelope is logged and dropped without ending the session; see
    EnvelopeError.
    """
    async for message in client.messages:
        if stop.is_set():
            return

        try:
            envelope = parse_envelope(message.payload)
        except EnvelopeError as exc:
            stats.bad_envelope += 1
            log.warning("bad envelope on %s: %s", message.topic, exc)
            continue

        stats.received += 1
        if not seen.check(envelope["event_id"]):
            stats.duplicates += 1
            # INFO: rare, and it is the QoS 1 dedupe contract working.
            log.info("duplicate %s dropped", envelope["event_id"])
            continue

        player = player_from_topic(message.topic)
        if player is None:
            # The visualiser published to a topic that does not end in a player id.
            # That is its bug, not a reason to drop the session, so pass the message
            # on unattributed where it is visible rather than silently swallowed.
            log.warning("no player id in topic %s", message.topic)

        try:
            inbound.put_nowait((player, envelope))
        except asyncio.QueueFull:
            # Drop the newest here: a readiness message that cannot be handed on
            # is not worth evicting one that already made it into the queue.
            stats.dropped += 1
            log.warning("inbound queue full, dropped %s", envelope["event_id"])


async def one_session(broker, outbound, inbound, pending, seen, stats, stop):
    """Hold one broker connection, publishing and subscribing concurrently.

    Returns when `stop` is set. Raises aiomqtt.MqttError when the link fails,
    which is the supervisor's cue to back off and reconnect.

    Subscribes *before* starting either task so no message can arrive on a
    topic the client has not yet asked for.
    """
    async with aiomqtt.Client(
        hostname=broker.host,
        port=broker.port,
        identifier=CLIENT_ID_LAPTOP_RELAY,
        keepalive=KEEPALIVE_S,
        tls_params=tls_params() if broker.tls else None,
    ) as client:
        stats.sessions += 1
        log.info("connected to %s as %s", broker.describe(), CLIENT_ID_LAPTOP_RELAY)
        await client.subscribe(TOPIC_VIZ_READY_FILTER, qos=QOS_VIZ_READY)
        log.info("subscribed to %s (qos %d)", TOPIC_VIZ_READY_FILTER, QOS_VIZ_READY)

        tasks = [
            asyncio.create_task(_publish_loop(client, outbound, pending, stats, stop)),
            asyncio.create_task(_subscribe_loop(client, inbound, seen, stats, stop)),
            asyncio.create_task(stop.wait()),
        ]
        try:
            # FIRST_COMPLETED, not FIRST_EXCEPTION. Setting `stop` completes
            # normally rather than raising, and FIRST_EXCEPTION degrades to
            # ALL_COMPLETED when nothing raises. On a quiet broker that would
            # leave _subscribe_loop parked in `async for` and hang shutdown until
            # some message happened to arrive. Any one of the three finishing
            # means this session is over, which is what we want.
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # re-raise MqttError, if that is why we woke
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def run_relay(broker, outbound, inbound, stop, stats=None):
    """Supervise the MQTT leg forever: connect, run, back off, retry.

    Returns only when `stop` is set. Same shape and backoff ladder as
    ble_client.run_node, for the same reason: a link that drops must not take the
    relay process down with it, and a broker that is not up yet must not make
    start order matter.
    """
    stats = stats or MqttStats()
    seen = SeenEvents()
    # Single-slot holder for the envelope currently in flight, drained by
    # _publish_loop. Owned here rather than by a session, so an envelope a failed
    # session was midway through survives the reconnect and goes out first on the
    # next one. Requeueing it instead would reorder it behind everything that
    # queued during the outage; see notes/shutdown_and_ordering.md.
    pending = []
    backoff = MIN_BACKOFF_S

    # finally, not a line after the loop: Ctrl-C cancels this task rather than
    # setting stop, and CancelledError slips past the excepts below.
    try:
        while not stop.is_set():
            started = time.monotonic()
            try:
                await one_session(broker, outbound, inbound, pending, seen, stats, stop)
            except aiomqtt.MqttError as exc:
                log.warning("session failed: %s", exc)
            except Exception:
                log.exception("unexpected session error")

            if stop.is_set():
                break

            stats.reconnects += 1
            elapsed = time.monotonic() - started
            if elapsed >= GOOD_RUN_S:
                log.info("session lasted %.0fs -> healthy run, backoff reset", elapsed)
                backoff = MIN_BACKOFF_S
            else:
                log.warning("session ended after %.0fs", elapsed)

            log.info("reconnecting in %.0fs", backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass

            if elapsed < GOOD_RUN_S:
                backoff = min(backoff * 2, MAX_BACKOFF_S)
    finally:
        log.info(
            "relay stopped: published=%d received=%d dup=%d bad_in=%d bad_out=%d "
            "dropped=%d failed=%d sessions=%d reconnects=%d",
            stats.published,
            stats.received,
            stats.duplicates,
            stats.bad_envelope,
            stats.bad_payload,
            stats.dropped,
            stats.publish_failed,
            stats.sessions,
            stats.reconnects,
        )


async def event_ticker(outbound, stats, stop, interval, player=1):
    """Standalone test: publish one dummy gesture event every `interval` seconds.

    Stands in for pipeline.forward_downlink, which feeds in the Ultra96's real AI
    events. The payload mirrors what ultra96/ai_events.py emits so the topic and
    the message agree; that file, not this one, is the editable dummy table.
    """
    seq = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        else:
            return  # stop was set rather than the interval elapsing

        enqueue_event(
            outbound,
            gesture_topic(player),
            build_envelope({"type": "AI_EVENT", "player": player, "tick": seq}, seq),
            stats,
        )
        seq += 1


async def drain_inbound(inbound, stop):
    """Log whatever the visualisers publish to `viz/ready/<player>`.

    Not just a stand-in: this is the consumer in the merged pipeline too. A phone
    announcing itself is worth seeing in the log during bring-up, and nothing
    downstream needs it. Whether phone-to-laptop traffic ever continues on to the
    Ultra96 is unsettled; see notes/mqtt_topics.md.
    """
    while not stop.is_set():
        try:
            player, envelope = await asyncio.wait_for(inbound.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        log.info("viz ready: player=%s %s from %s: %s",
                 player, envelope["event_id"], envelope["source"], envelope.get("payload"))


async def run_standalone(broker, publish_every):
    """Wire run_relay to the standalone ticker and drain, until cancelled."""
    outbound = asyncio.Queue(maxsize=OUTBOUND_MAXSIZE)
    inbound = asyncio.Queue(maxsize=INBOUND_MAXSIZE)
    stats = MqttStats()
    stop = asyncio.Event()

    tasks = [
        asyncio.create_task(run_relay(broker, outbound, inbound, stop, stats)),
        asyncio.create_task(drain_inbound(inbound, stop)),
    ]
    if publish_every > 0:
        tasks.append(asyncio.create_task(event_ticker(outbound, stats, stop, publish_every)))

    try:
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def use_selector_event_loop():
    """Select the SelectorEventLoop on Windows.

    aiomqtt drives paho through `loop.add_reader`, which the ProactorEventLoop
    (the default on Windows since Python 3.8) does not implement for sockets.
    Without this, the first connect dies with NotImplementedError and no hint as
    to why.

    Call this from an entry point only, never at import. The combined relay runs
    bleak in the same loop, and forcing a process-wide policy from an imported
    module is how that resurfaces later as an unexplained BLE failure. Where the
    two are merged, the choice belongs in the single entry point, with BLE
    verified to still connect under it.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def main():
    """Entry point for running the MQTT leg on its own."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true", help="log every message")
    parser.add_argument(
        "--host",
        default=None,
        help="broker host (default: $MQTT_BROKER_HOST, else 127.0.0.1)",
    )
    parser.add_argument("--port", type=port_number, default=None, help="broker port")
    parser.add_argument(
        "--tls",
        action="store_true",
        help="use the mTLS listener and comms/certs material (Week 6; not yet generated)",
    )
    parser.add_argument(
        "--publish-every",
        type=non_negative_float,
        default=0.0,
        metavar="SECONDS",
        help="standalone test: publish a dummy game event this often (default: off)",
    )
    args = parser.parse_args()

    log_file = setup_logging(LOG_FILE, args.verbose)
    broker = resolve_broker(args.host, args.port, args.tls)
    log.info("broker %s; logging to %s", broker.describe(), log_file)

    use_selector_event_loop()
    try:
        asyncio.run(run_standalone(broker, args.publish_every))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
