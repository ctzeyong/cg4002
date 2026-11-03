"""Publish the board's AI events and the gloves' status to the MQTT broker on this board
(configuration B).

Publish-only; nothing here subscribes. notes/tunnelled_visualiser.md.

Rules for AI events (why: notes/shutdown_and_ordering.md):
- A failed publish is kept and retried after reconnecting, so order is kept.
- An event that cannot be turned into JSON is thrown away; it would fail every time.
- An event older than EVENT_TTL_S is thrown away rather than shown late.

The gloves' status is retained and never expires. Only the latest one matters, so it waits in
a slot rather than the queue. The client's last will marks both gloves offline if it dies.
"""

import asyncio
import json
import logging
import time

import aiomqtt

import envelope as envelope_contract

QOS_GAME_GESTURE = 1  # at least once: the broker must acknowledge (PUBACK)
QOS_GLOVES = 1
KEEPALIVE_S = 15
OFFLINE_TIMEOUT_S = 2.0  # how long a clean stop waits to mark the gloves offline

OUTBOUND_MAXSIZE = 256  # events come one per keypress, so this is plenty

EVENT_TTL_S = 2.0  # older events are too late to show, so they are thrown away

# Reconnect delay: doubles after each quick failure, resets after a long healthy
# session, never more than 60 s.
MIN_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0
GOOD_RUN_S = 60.0

CLIENT_ID = envelope_contract.CLIENT_ID_ULTRA96

log = logging.getLogger("mqtt_publisher")


class PublishStats:
    """Counters for the publisher. No lock needed: only one task updates them."""

    def __init__(self):
        self.published = 0
        self.dropped = 0  # queue full, oldest thrown away
        self.stale = 0  # too old to publish, thrown away
        self.undeliverable = 0  # could not be turned into JSON, thrown away
        self.sessions = 0
        self.reconnects = 0

    def describe(self):
        """All counters on one line, for the log."""
        return (
            f"published={self.published} dropped={self.dropped} stale={self.stale} "
            f"undeliverable={self.undeliverable} sessions={self.sessions} "
            f"reconnects={self.reconnects}"
        )


class GloveSlot:
    """The gloves' latest state, waiting to be published to status/gloves.

    A new state replaces one not yet sent. Each envelope gets the next seq of its own source.
    """

    def __init__(self):
        self.state = None  # e.g. {"player1": "ready", "player2": "down"}
        self.unsent = False
        self.changed = asyncio.Event()
        self._seq = 0

    def set(self, state):
        self.state = state
        self.unsent = True
        self.changed.set()

    def envelope(self, state):
        env = envelope_contract.build_envelope(
            state, self._seq, source=envelope_contract.GLOVES_SOURCE
        )
        self._seq += 1
        return env

    def offline_payload(self):
        """Return both gloves offline as JSON, for the last will and for a clean stop."""
        states = dict.fromkeys(envelope_contract.PLAYERS, envelope_contract.OFFLINE)
        return json.dumps(self.envelope(envelope_contract.gloves_payload(states)))


def enqueue_event(outbound, topic, env, stats):
    """Queue an event to publish. Never blocks; if the queue is full, the oldest is thrown away.

    Each item records when it was queued, so its age can be checked before publishing.
    """
    item = (topic, env, time.monotonic())
    try:
        outbound.put_nowait(item)
    except asyncio.QueueFull:
        try:
            outbound.get_nowait()
            stats.dropped += 1
        except asyncio.QueueEmpty:  # pragma: no cover - emptied between the two calls
            pass
        outbound.put_nowait(item)


async def _publish_loop(client, outbound, pending, stats, stop):
    """Take events off the queue and publish them one at a time, oldest first.

    The event being sent sits in `pending` until the broker acknowledges it.
    """
    while not stop.is_set():
        # Always yield once per pass, so a stuck `pending` cannot freeze the board.
        # Do not remove; see notes/tunnelled_visualiser.md.
        await asyncio.sleep(0)

        if not pending:
            try:
                pending.append(await asyncio.wait_for(outbound.get(), timeout=1.0))
            except asyncio.TimeoutError:
                continue

        topic, env, queued_at = pending[0]

        # Checked on every attempt, so a retry after a reconnect is checked too.
        age = time.monotonic() - queued_at
        if age > EVENT_TTL_S:
            stats.stale += 1
            event_id = env.get("event_id", "<no id>") if isinstance(env, dict) else "<no id>"
            log.warning("discarding stale %s on %s: %.2fs old", event_id, topic, age)
            pending.clear()
            continue

        try:
            payload = json.dumps(env)
        except (TypeError, ValueError) as exc:
            # Throw it away: it would fail the same way on every retry.
            log.error("dropping unserialisable event on %s: %s", topic, exc)
            stats.undeliverable += 1
            pending.clear()
            continue

        try:
            await client.publish(topic, payload=payload, qos=QOS_GAME_GESTURE)
        except aiomqtt.MqttError:
            # Keep it in `pending`; it is retried after the reconnect if still fresh.
            raise

        stats.published += 1
        pending.clear()
        # `.get`, so an event with no id cannot crash the loop after publishing.
        # `t=` is the PUBACK time on the board clock; notes/latency.md 3.2.
        log.info("published %s to %s t=%.6f", env.get("event_id", "<no id>"), topic, time.time())


async def _publish_gloves(client, slot, stop):
    """Publish the gloves' state, retained, whenever it changes, until `stop` is set.

    The current state is sent again at the start of every session, since the last will may
    have replaced it while the client was away.
    """
    if slot.state is not None:
        slot.unsent = True
    while not stop.is_set():
        slot.changed.clear()
        if not slot.unsent:
            try:
                await asyncio.wait_for(slot.changed.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            continue

        state = slot.state
        payload = json.dumps(slot.envelope(state))
        await client.publish(
            envelope_contract.GLOVES_TOPIC, payload=payload, qos=QOS_GLOVES, retain=True
        )
        # worded unlike the events' "published" line, so the latency report never reads it
        log.debug("glove status sent: %s", payload)
        if slot.state is state:  # else a newer state came in meanwhile, and goes next
            slot.unsent = False


async def _publish_offline(client, slot):
    """Mark both gloves offline before a clean disconnect, which would suppress the will."""
    try:
        await client.publish(
            envelope_contract.GLOVES_TOPIC,
            payload=slot.offline_payload(),
            qos=QOS_GLOVES,
            retain=True,
            timeout=OFFLINE_TIMEOUT_S,
        )
        log.info("glove status: both offline")
    except aiomqtt.MqttError as exc:
        log.warning("could not mark the gloves offline: %s", exc)


async def _raise_on_disconnect(client):
    """Raise MqttError as soon as the broker connection drops, not at the next publish.

    Nothing is subscribed, so the loop only ends when the connection drops. Without it, an
    idle client would never notice, and never resend the gloves' state.
    """
    async for _ in client.messages:
        pass


async def one_session(host, port, outbound, pending, stats, stop, slot):
    """Connect to the broker once and publish until `stop` is set or the link fails.

    AI events and the gloves' state go out from two loops on the one client, so neither
    waits behind the other. If the client dies, the broker publishes its will instead.

    Raises:
        aiomqtt.MqttError: the link failed; `run_publisher` will reconnect.
    """
    will = aiomqtt.Will(
        envelope_contract.GLOVES_TOPIC, slot.offline_payload(), qos=QOS_GLOVES, retain=True
    )
    async with aiomqtt.Client(
        hostname=host,
        port=port,
        identifier=CLIENT_ID,
        keepalive=KEEPALIVE_S,
        will=will,
    ) as client:
        stats.sessions += 1
        log.info("connected to mqtt://%s:%d as %s", host, port, CLIENT_ID)

        ended = asyncio.Event()  # ends both loops, so neither is cancelled mid-publish
        loops = [
            asyncio.create_task(_publish_loop(client, outbound, pending, stats, ended)),
            asyncio.create_task(_publish_gloves(client, slot, ended)),
        ]
        watchers = [
            asyncio.create_task(stop.wait()),
            asyncio.create_task(_raise_on_disconnect(client)),
        ]
        try:
            await asyncio.wait([*loops, *watchers], return_when=asyncio.FIRST_COMPLETED)
        finally:
            ended.set()  # before cancelling, as everywhere
            slot.changed.set()  # wake the gloves loop now, or its 1 s wait delays the reconnect
            for task in watchers:
                task.cancel()
            await asyncio.gather(*loops, *watchers, return_exceptions=True)
        for task in [*loops, *watchers]:
            if not task.cancelled():
                task.result()  # re-raise a failure

        # no loop failed, so this is a clean stop
        await _publish_offline(client, slot)


async def run_publisher(host, port, outbound, stop, stats, slot):
    """Keep the publisher connected: connect, publish, wait, reconnect, until `stop`.

    `pending` lives here so an unsent event survives a reconnect.
    """
    pending = []
    backoff = MIN_BACKOFF_S

    while not stop.is_set():
        started = asyncio.get_running_loop().time()
        try:
            await one_session(host, port, outbound, pending, stats, stop, slot)
        except aiomqtt.MqttError as exc:
            log.warning("mqtt session failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Log and reconnect; an unexpected error must not stop the publisher.
            log.exception("unexpected mqtt session error")

        if stop.is_set():
            break

        stats.reconnects += 1
        elapsed = asyncio.get_running_loop().time() - started
        if elapsed >= GOOD_RUN_S:
            backoff = MIN_BACKOFF_S

        log.info("reconnecting to the broker in %.0fs", backoff)
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass

        if elapsed < GOOD_RUN_S:
            backoff = min(backoff * 2, MAX_BACKOFF_S)

    log.info("publisher stopped: %s", stats.describe())
