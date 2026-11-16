"""Relay with the broker on the Ultra96: no MQTT leg on this laptop.

Alternative to `pipeline.py`, the laptop-broker setup, which was evaluated but is not
used in the final system. Run one or the other.

    laptop broker  BLE -> laptop -> TCP -> Ultra96 -> TCP -> laptop -> MQTT -> phones
    this relay     BLE -> laptop -> TCP -> Ultra96 -> MQTT -> phones
                                                     (published on the board's own loopback)

Here the Ultra96 publishes its own AI events (`ultra96/tcp_client_mqtt.py`), so
this laptop has **no MQTT role at all** -- no relay, no downlink forwarder, no
inbound drain, no broker address. Three legs instead of six, and this process
never touches aiomqtt.

The uplink stays TCP, unchanged. That was settled on measurement:
traffic is ~200:1 asymmetric and MQTT would add overhead on the congested
direction.

`tunnel.py` is used as-is and needs no second forward: nothing here publishes, so
there is nothing to forward a broker port for.

    python pipeline_board_broker.py --tunnel

Selector loop note: this process runs bleak and asyncssh but **not** aiomqtt, so
`use_selector_event_loop()` is deliberately not called. See main().
"""

import argparse
import asyncio
import logging
import time
from pathlib import Path

import ble_client
import ble_packet
import tcp_server
import tunnel
from mqtt_config import port_number
from pipeline import (
    forward_uplink,
    install_gatt_cancel_error_filter,
    install_interrupt_handler,
    keystroke_reader,
)
from logging_setup import setup_logging
from plumbing import suppress_queue_get_shutdown_warning

LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "pipeline_board_broker.log"

log = logging.getLogger("pipeline_board_broker")

VIBRATE = "VIBRATE"  # sent by the board after it classifies a gesture

LOOP_CHECK_S = 0.1
SLOW_LOOP_MS = 50.0  # an idle loop on Windows wakes up to about 21 ms late, so this stays quiet
SLOW_LOOP_REPORT_S = 1.0


def _is_int(value):
    """Return True for an int that is not a bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def post_vibrate(msg, outboxes, seq):
    """Turn a VIBRATE from the board into a CMD in that player's outbox.

    A malformed one is logged and dropped, since retrying would not fix it.
    Returns True if a CMD was queued.
    """
    node, pattern = msg.get("node"), msg.get("pattern")
    if not (_is_int(node) and node in outboxes and _is_int(pattern)):
        log.warning("dropping VIBRATE with node=%.20r pattern=%.20r", node, pattern)
        return False
    try:
        packet = ble_packet.build_vibrate_packet(seq, pattern)
    except OverflowError:
        log.warning("dropping VIBRATE for node %d: pattern %d is not an int16", node, pattern)
        return False
    if ble_client.post_command(outboxes[node], packet):
        log.warning("node %d: outbox full, oldest CMD dropped to queue seq=%d", node, seq)
    log.info("VIBRATE node=%d pattern=%d -> CMD seq=%d queued", node, pattern, seq)
    return True


async def drain_downlink(downlink, outboxes, stop):
    """Pass each VIBRATE from the board on to its glove, and log and discard anything else.

    In the laptop-broker setup this queue carried AI events into `forward_downlink`
    and on to MQTT. Here the board publishes those itself, so the only thing it sends
    down is a VIBRATE.

    HEARTBEATs never reach this queue: `tcp_server` answers them itself and they go
    no further, which is why omitting this leg's MQTT half does not make the tunnel
    flap.
    """
    seq = 0  # counts VIBRATEs only, separate from the keystroke CMDs
    seen = 0
    while not stop.is_set():
        try:
            msg = await asyncio.wait_for(downlink.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        if isinstance(msg, dict) and msg.get("type") == VIBRATE:
            if post_vibrate(msg, outboxes, seq):
                seq = (seq + 1) & 0xFF
            continue

        seen += 1
        if seen == 1:
            log.warning(
                "the board sent %r down the TCP leg; under configuration B it should "
                "publish events itself. Is it running tcp_client.py instead of "
                "tcp_client_mqtt.py?",
                msg.get("type") if isinstance(msg, dict) else type(msg).__name__,
            )
        elif seen % 100 == 0:
            log.warning("%d unexpected downlink messages discarded", seen)


async def watch_loop_lag(stop):
    """Warn when this task wakes up late, which means something held the event loop.

    Late wake-ups are reported at most once a second, with the worst one.
    """
    late = 0
    worst_ms = 0.0
    last_report = float("-inf")  # so the first late wake-up is reported at once
    while not stop.is_set():
        started = time.perf_counter()
        await asyncio.sleep(LOOP_CHECK_S)
        lag_ms = (time.perf_counter() - started - LOOP_CHECK_S) * 1000
        if lag_ms >= SLOW_LOOP_MS:
            late += 1
            worst_ms = max(worst_ms, lag_ms)
        now = time.monotonic()
        if late and now - last_report >= SLOW_LOOP_REPORT_S:
            log.warning(
                "slow event loop: %d wake-up(s) late by %.0f ms or more, worst %.0f ms",
                late,
                SLOW_LOOP_MS,
                worst_ms,
            )
            late = 0
            worst_ms = 0.0
            last_report = now


async def run_pipeline(nodes, tcp_host, tcp_port, tunnel_config=None, tunnel_password=None):
    """Run entire pipline (Firebeetle -> Laptop -> U96 -> Phone) until shutdown."""
    ble_queue = asyncio.Queue(maxsize=ble_client.QUEUE_MAXSIZE)
    outboxes = ble_client.make_outboxes(nodes)
    downlink = asyncio.Queue(maxsize=tcp_server.DOWNLINK_MAXSIZE)
    stop = asyncio.Event()
    restore_interrupt = install_interrupt_handler(stop)
    restore_exc_handler = install_gatt_cancel_error_filter()

    tasks = [
        asyncio.create_task(ble_client.run_all(nodes, ble_queue, outboxes, stop)),
        asyncio.create_task(tcp_server.run(tcp_host, tcp_port, downlink)),
        asyncio.create_task(forward_uplink(ble_queue, stop)),
        asyncio.create_task(drain_downlink(downlink, outboxes, stop)),
        asyncio.create_task(keystroke_reader(nodes, outboxes, stop)),
        asyncio.create_task(watch_loop_lag(stop)),
    ]
    if tunnel_config is not None:
        tasks.append(
            asyncio.create_task(tunnel.run_tunnel(tunnel_config, tunnel_password, stop))
        )
    try:
        # stop pipeline when any worker exits
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        restore_interrupt()
        stop.set()  # signal a shutdown to all tasks, so they can exit gracefully
        for task in tasks:
            task.cancel()
            
        # wait for all tasks to finish its cancellation/cleanup
        await asyncio.gather(*tasks, return_exceptions=True)
        # GATT cancellation errors can be emitted during BLE teardown, so keep
        # the exception filter installed until all tasks have been drained
        restore_exc_handler()


def main():
    """Entry point for the relay."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true", help="log every packet")
    parser.add_argument(
        "--debug-libs", action="store_true", help="also log bleak/asyncssh internals"
    )
    parser.add_argument(
        "--nodes",
        type=ble_client.select_nodes,
        default=ble_client.NODES,
        metavar="IDS",
        help="comma-separated node ids to run, e.g. 1 (default: all)",
    )
    parser.add_argument("--tcp-host", default=tcp_server.DEFAULT_HOST)
    parser.add_argument("--tcp-port", type=port_number, default=tcp_server.DEFAULT_PORT)
    parser.add_argument(
        "--tunnel",
        action="store_true",
        help="also supervise the SSH reverse tunnel here; prompts for the jump-host password",
    )
    args = parser.parse_args()

    log_file = setup_logging(LOG_FILE, args.verbose, debug_libs=args.debug_libs)
    suppress_queue_get_shutdown_warning()

    tunnel_config = tunnel_password = None
    if args.tunnel:
        tunnel_config = tunnel.load_config()
        if tunnel_config.port != args.tcp_port:
            # Both halves have to name the same port or the forward lands nowhere,
            # and the failure is silent: the board would connect to its own empty
            # localhost port and this server would never see a client.
            parser.error(
                f"--tcp-port {args.tcp_port} does not match TUNNEL_PORT "
                f"{tunnel_config.port} in .env; the tunnel would forward into nothing"
            )

    log.info(
        "TCP %s:%d; nodes %s; logging to %s",
        args.tcp_host,
        args.tcp_port,
        [node.node_id for node in args.nodes],
        log_file,
    )

    # Before the loop starts: getpass blocks, and it must not block the loop.
    if tunnel_config is not None:
        log.info("tunnel %s", tunnel_config.describe())
        tunnel_password = tunnel.load_password(tunnel_config.jump_host)

    # No use_selector_event_loop() here, unlike pipeline.py, and that is deliberate:
    # aiomqtt is what needs the selector loop and this process never imports it.
    # bleak works on either. asyncssh's standalone VPN-drop proof was on the default
    # Proactor loop, so this relay actually runs --tunnel on the loop that
    # proof was taken on, rather than the one it was re-verified on.

    try:
        asyncio.run(
            run_pipeline(
                args.nodes,
                args.tcp_host,
                args.tcp_port,
                tunnel_config,
                tunnel_password,
            )
        )
    except KeyboardInterrupt:
        log.info("interrupted")


if __name__ == "__main__":
    main()
