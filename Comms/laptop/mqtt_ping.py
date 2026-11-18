"""Round-trip check for the MQTT leg: is the broker reachable, and how fast?

Publishes N messages to `comms/ping` while subscribed to the same topic, so each
one comes back through the broker. That measures the laptop -> broker -> laptop
hop on its own, with no phone, no Unity and no game pipeline involved — which is
what you want when the question is "is the broker up and reachable from here".

Two jobs:

  1. **Bring-up.** Point it at 127.0.0.1 before anything else exists. If this
     fails, nothing further on this leg is worth debugging.
  2. **The broker-location comparison.** `--count 100` against a laptop broker
     and against an Ultra96 broker gives the median and p95 that decision needs.

Usage:

    python mqtt_ping.py                          # 20 pings at 127.0.0.1:1883
    python mqtt_ping.py --count 100              # the comparison run
    python mqtt_ping.py --host 10.160.77.244     # from a second machine

To prove the *phone* leg, the phone must be the one connecting: run this on the
laptop to confirm the broker is healthy, then point an MQTT client app on the
phone at the same host and subscribe to `comms/ping` while this is running.
"""

import argparse
import asyncio
import json
import logging
import math
import statistics
import time

import aiomqtt

from mqtt_client import use_selector_event_loop
from mqtt_config import (
    CLIENT_ID_PING,
    KEEPALIVE_S,
    TOPIC_PING,
    non_negative_float,
    port_number,
    positive_int,
    resolve_broker,
    tls_params,
)

DEFAULT_COUNT = 20
DEFAULT_INTERVAL_S = 0.1
# How long to keep waiting after the last publish. Generous: a straggler that
# arrives at 2 s is a far more useful finding than one counted as lost.
DEFAULT_LINGER_S = 3.0

log = logging.getLogger("mqtt_ping")


def percentile(values, fraction):
    """Nearest-rank percentile of `values` (must be non-empty and sorted-able).

    Nearest-rank rather than interpolating: at n=20 an interpolated p95 is a
    blend of two samples and reports a latency that never happened. This returns
    a real measurement.

    `ceil`, not `round`. Nearest rank is defined as ceil(fraction * n), and the
    two disagree for most n in 11-19 and 30-39 — at n=30 rounding picks the 28th
    sample where the definition wants the 29th, so a 30-message run would report
    a p95 that is one sample too optimistic.
    """
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


async def _collect(client, rtts, expected, done):
    """Match returning pings to their send time and record the round trip."""
    async for message in client.messages:
        try:
            obj = json.loads(message.payload.decode("utf-8"))
            seq = obj["seq"]

            # `type(...) is not int` rather than isinstance: bool subclasses int,
            # so `"seq": true` would otherwise pass and then index `expected`.
            # Checked before the pop below, because an unhashable seq (a list, a
            # dict) raises TypeError from dict.pop itself.
            if type(seq) is not int or seq < 0:
                raise ValueError(f"invalid seq {seq!r}")

        # ValueError, not the narrower UnicodeDecodeError/JSONDecodeError: both
        # of those are ValueError *subclasses*, so listing them does not catch a
        # plain ValueError — including the one raised just above. Getting this
        # wrong kills the collector task mid-run, and the symptom is a ping
        # report that silently stops counting rather than an error.
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("unrecognised message on %s (%s), ignoring", message.topic, exc)
            continue

        sent = expected.pop(seq, None)
        if sent is None:
            # Either a QoS 1 redelivery of one already counted, or someone else
            # publishing on this topic. Neither should skew the numbers.
            log.debug("ping %s not outstanding (duplicate or foreign), ignoring", seq)
            continue

        # perf_counter: measuring an interval, and both ends of it are in this
        # process, so no clock skew and no wall-clock adjustment can enter.
        rtts.append((time.perf_counter() - sent) * 1000.0)
        if not expected:
            done.set()


async def run_ping(broker, count, interval, qos, linger):
    """Send `count` pings through the broker and report the round trips.

    Returns True if every ping came back, False otherwise, so the exit code can
    be used in a pre-demo check script.
    """
    rtts = []
    expected = {}
    done = asyncio.Event()

    log.info("pinging %s (%d messages, qos %d)", broker.describe(), count, qos)
    connect_started = time.perf_counter()

    async with aiomqtt.Client(
        hostname=broker.host,
        port=broker.port,
        identifier=CLIENT_ID_PING,
        keepalive=KEEPALIVE_S,
        tls_params=tls_params() if broker.tls else None,
    ) as client:
        log.info("connected in %.0f ms", (time.perf_counter() - connect_started) * 1000.0)
        await client.subscribe(TOPIC_PING, qos=qos)

        collector = asyncio.create_task(_collect(client, rtts, expected, done))
        try:
            for seq in range(count):
                payload = json.dumps({"seq": seq, "ts": time.time()}).encode("utf-8")
                # Record the send time before awaiting publish, so the measured
                # interval includes the whole outbound path rather than starting
                # after the broker has already acknowledged it.
                expected[seq] = time.perf_counter()
                await client.publish(TOPIC_PING, payload=payload, qos=qos)
                if interval > 0:
                    await asyncio.sleep(interval)

            try:
                await asyncio.wait_for(done.wait(), timeout=linger)
            except asyncio.TimeoutError:
                log.warning("%d of %d pings never came back", len(expected), count)
        finally:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)

    if not rtts:
        log.error("no pings returned — broker reachable but nothing round-tripped")
        return False

    log.info(
        "%d/%d returned | min %.1f ms | median %.1f ms | p95 %.1f ms | max %.1f ms",
        len(rtts),
        count,
        min(rtts),
        statistics.median(rtts),
        percentile(rtts, 0.95),
        max(rtts),
    )
    return len(rtts) == count


def main():
    """Entry point. Exit code 0 only if every ping returned."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--host", default=None, help="broker host (default: $MQTT_BROKER_HOST)")
    parser.add_argument("--port", type=port_number, default=None, help="broker port")
    parser.add_argument("--tls", action="store_true", help="use the mTLS listener")
    parser.add_argument("--count", type=positive_int, default=DEFAULT_COUNT)
    parser.add_argument(
        "--interval",
        type=non_negative_float,
        default=DEFAULT_INTERVAL_S,
        metavar="SECONDS",
        help="gap between pings; 0 sends them back to back (default: %(default)s)",
    )
    parser.add_argument("--qos", type=int, choices=(0, 1, 2), default=1)
    parser.add_argument(
        "--linger", type=non_negative_float, default=DEFAULT_LINGER_S, metavar="SECONDS"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
    )
    broker = resolve_broker(args.host, args.port, args.tls)

    use_selector_event_loop()
    try:
        ok = asyncio.run(run_ping(broker, args.count, args.interval, args.qos, args.linger))
    except aiomqtt.MqttError as exc:
        # The common failures all land here: broker not running, wrong address,
        # firewall dropping the inbound connection, VPN filtering the LAN.
        log.error("could not reach %s: %s", broker.describe(), exc)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)

    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
