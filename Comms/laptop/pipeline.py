"""The whole relay in one process: BLE, TCP and MQTT on a single event loop.

Each leg is supervised by its own module and reconnects on its own backoff; this
file owns nothing but the queues between them and the keyboard at the front. It
is the only module that imports all three legs.

    keyboard ──> outboxes ──> ble_client.drain_outbox ──> CMD ──> FireBeetle
                 (per node)                                          │
                                                                 negates
                                                                     │
    FireBeetle ──notify──> ble_client.run_all ──> ble_queue <─────ECHO┘
                                                      │
                                               forward_uplink
                                                      │
                                          tcp_server.send_to_current
                                                      │
                                               (SSH tunnel) ──> Ultra96
                                                                    │
                                                          random AI event
                                                                    │
    phone <── MQTT ── mqtt_client.run_relay <── outbound <── forward_downlink
                             │                                      │
                          inbound ──> drain_inbound            downlink
                                                                    │
                                                       tcp_server.handle_client

Run it:

    python pipeline.py                 # all nodes, broker from env, loopback TCP
    python pipeline.py --nodes 1 -v    # one FireBeetle, per-packet logging
    python pipeline.py --tunnel        # also hold the SSH tunnel to the Ultra96

Each leg also still has its own entry point (`ble_client.py`, `tcp_server.py`,
`mqtt_client.py`) for debugging one link in isolation.
"""

import argparse
import asyncio
import logging
import signal
import time
from pathlib import Path

import ble_client
import ble_packet
import mqtt_client
import tcp_server
import tunnel
from logging_setup import PROGRESS, setup_logging
from mqtt_config import UNATTRIBUTED_PLAYER, gesture_topic, port_number, resolve_broker
from plumbing import (
    RateWindow,
    install_gatt_cancel_error_filter,
    suppress_queue_get_shutdown_warning,
)

try:
    import msvcrt  # Windows-only, and the relay laptop is Windows
except ImportError:  # pragma: no cover - the tests monkeypatch read_key
    msvcrt = None

KEY_POLL_S = 0.02  # imperceptible to a typist, negligible to the loop
QUIT_KEYS = ("\x03", "\x1b")  # Ctrl-C, Esc
SPECIAL_KEY_PREFIXES = ("\x00", "\xe0")  # first character of a function or arrow key

LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "pipeline.log"

log = logging.getLogger("pipeline")


async def forward_uplink(uplink, stop):
    """Send each BLE packet to the Ultra96 over TCP, and log Hz/kbps once a second.

    Every board's packets pass through here, so this is where the total rate is measured.
    With no Ultra96 connected a packet is counted as `unsent` and dropped, not held:
    old sensor readings are useless by the time the link comes back.
    """
    window = RateWindow()
    forwarded = 0
    # "unsent" (nowhere to send it) is not "dropped" (lost on the BLE side); notes/tcp_leg.md.
    unsent = 0
    linked = None  # None until the first packet shows whether the Ultra96 is there

    while not stop.is_set():
        try:
            packet = await asyncio.wait_for(uplink.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        # Only format the packet when -v is on: this runs 200 times a second.
        if log.isEnabledFor(logging.DEBUG):
            log.debug("uplink: %s", ble_packet.format_packet(packet))

        sent = await tcp_server.send_to_current(ble_packet.packet_to_message(packet))
        if sent:
            forwarded += 1
        else:
            unsent += 1

        # Log only when the Ultra96 link comes or goes, not on every packet.
        if sent != linked:
            log.log(
                logging.INFO if sent else logging.WARNING,
                "uplink %s",
                "sink connected; forwarding to the Ultra96"
                if sent
                else "has no Ultra96 connected; packets counted as unsent, not lost",
            )
            linked = sent

        # Timed by BLE arrival, so the rate shows the BLE link, not this task's timing.
        report = window.tick(packet.t_recv, ble_packet.HEADER_LEN + len(packet.payload))
        if report:
            rate, kbps = report
            # queued: rising means this task is falling behind the BLE readers.
            log.info(
                "uplink: forwarded=%d unsent=%d %.1f Hz %.1f kbps queued=%d",
                forwarded,
                unsent,
                rate,
                kbps,
                uplink.qsize(),
                extra=PROGRESS,  # a 1 Hz counter: dimmed on the console
            )


async def forward_downlink(downlink, outbound, stats, stop):
    """Wrap Ultra96 responses in the MQTT envelope and queue them for publishing.

    The envelope (event_id / source / seq / ts / payload) is the contract with the
    visualiser. The Ultra96's message goes inside `payload` untouched, so the AI
    event schema can move without the visualiser moving with it.

    This is also where the publish topic is chosen. The player id is known here,
    having ridden all the way from the FireBeetle's node id, through the ECHO, into
    the Ultra96's AI event, so the relay never has to open a payload to route one.

    `source` stays the laptop relay even though the Ultra96 produced the content,
    because `event_id` has to be unique per *publisher* and the laptop is what
    publishes.
    """
    seq = 0
    while not stop.is_set():
        try:
            msg = await asyncio.wait_for(downlink.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        player = msg.get("player") if isinstance(msg, dict) else None
        if not isinstance(player, int) or isinstance(player, bool):
            log.warning("downlink event has no usable player id: %s", msg)
            player = UNATTRIBUTED_PLAYER

        topic = gesture_topic(player)
        mqtt_client.enqueue_event(outbound, topic, mqtt_client.build_envelope(msg, seq), stats)
        # INFO: once per AI event. scripts/latency_report.py reads `t=`; notes/latency.md.
        log.info("downlink seq=%d t=%.6f -> %s: %s", seq, time.perf_counter(), topic, msg)
        seq += 1


def read_key():
    """Return one waiting keypress, or None. Never blocks the event loop.

    msvcrt reads single keys without Enter and without a thread. Arrow and function
    keys arrive as two characters, so both are read as one key. Always None off Windows.
    """
    if msvcrt is None or not msvcrt.kbhit():
        return None
    key = msvcrt.getwch()
    if key in SPECIAL_KEY_PREFIXES:
        key += msvcrt.getwch()
    return key


async def keystroke_reader(nodes, outboxes, stop, poll_interval=KEY_POLL_S):
    """Turn each keypress into one random dummy CMD packet. Esc quits.

    Only letters and digits send. `1`/`2` send to that board only; any other letter
    or digit sends to every board. The key goes in field 0 so the Ultra96 can map it
    to a gesture (notes/tcp_leg.md).
    """
    if msvcrt is None:
        log.warning("no console keyboard on this platform; keystroke reader is idle")

    by_key = {str(node.node_id): node for node in nodes}
    seq = 0
    log.info(
        "keyboard ready: letters and digits send a dummy packet, 1/2 target one node, "
        "Esc quits"
    )

    while not stop.is_set():
        key = read_key()
        if key is None:
            await asyncio.sleep(poll_interval)
            continue

        if key in QUIT_KEYS:
            log.info("quit key pressed; shutting down")
            stop.set()
            return

        if not (len(key) == 1 and key.isascii() and key.isalnum()):
            name = "function/arrow" if key[:1] in SPECIAL_KEY_PREFIXES else repr(key)
            log.info("key %s ignored: only letters and digits send a packet", name)
            await asyncio.sleep(0)  # let other tasks run, as at the end of the loop
            continue

        targeted = by_key.get(key)
        targets = (targeted,) if targeted else nodes
        packet = ble_packet.build_cmd_packet(seq, ble_client.key_cmd_fields(key))
        for node in targets:
            if ble_client.post_command(outboxes[node.node_id], packet):
                log.warning(
                    "node %d: outbox full, oldest CMD dropped to queue seq=%d", node.node_id, seq
                )

        log.info(
            "key %r -> CMD seq=%d queued for node(s) %s",
            key,
            seq,
            [node.node_id for node in targets],
        )
        seq = (seq + 1) & 0xFF
        # Yield, so a held-down key or a paste cannot starve the BLE and TCP tasks.
        await asyncio.sleep(0)


def install_interrupt_handler(stop):
    """Make Ctrl-C set `stop` for a clean shutdown, instead of raising inside a task.

    On Windows, Ctrl-C arrives as a signal, not a key, so keystroke_reader never sees it.
    A second Ctrl-C force-quits. Returns a function that restores the old handler.
    See notes/shutdown_and_ordering.md.
    """
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)

    def handle(signum, frame):
        # Restore first, so a second Ctrl-C force-quits if shutdown gets stuck.
        signal.signal(signal.SIGINT, previous)
        log.info("interrupt received; shutting down (Ctrl-C again to force)")
        # Signal handlers run outside the loop, so ask the loop to set the event.
        loop.call_soon_threadsafe(stop.set)

    try:
        signal.signal(signal.SIGINT, handle)
    except ValueError:
        # signal.signal only works on the main thread; carry on without the handler.
        log.warning("not on the main thread; Ctrl-C will not shut down cleanly")
        return lambda: None

    return lambda: signal.signal(signal.SIGINT, previous)


async def run_pipeline(
    nodes, broker, tcp_host, tcp_port, tunnel_config=None, tunnel_password=None
):
    """Run the three legs and the forwarders between them until cancelled.

    Every queue is created here, so the wiring is readable in one place. Each leg
    keeps its own supervisor and backoff, which is what makes start order
    irrelevant: a leg whose peer is not up yet just retries, and no single link
    failure reaches this level.

    `tunnel_config` adds the SSH reverse tunnel as a fourth supervised leg. The
    password rides separately rather than inside the config so no repr of the
    config can print it.
    """
    ble_queue = asyncio.Queue(maxsize=ble_client.QUEUE_MAXSIZE)
    outboxes = ble_client.make_outboxes(nodes)
    downlink = asyncio.Queue(maxsize=tcp_server.DOWNLINK_MAXSIZE)
    outbound = asyncio.Queue(maxsize=mqtt_client.OUTBOUND_MAXSIZE)
    inbound = asyncio.Queue(maxsize=mqtt_client.INBOUND_MAXSIZE)
    mqtt_stats = mqtt_client.MqttStats()
    stop = asyncio.Event()
    restore_interrupt = install_interrupt_handler(stop)
    restore_exc_handler = install_gatt_cancel_error_filter()

    tasks = [
        asyncio.create_task(ble_client.run_all(nodes, ble_queue, outboxes, stop)),
        asyncio.create_task(tcp_server.run(tcp_host, tcp_port, downlink)),
        asyncio.create_task(mqtt_client.run_relay(broker, outbound, inbound, stop, mqtt_stats)),
        asyncio.create_task(forward_uplink(ble_queue, stop)),
        asyncio.create_task(forward_downlink(downlink, outbound, mqtt_stats, stop)),
        # Phone -> laptop traffic is logged and goes no further. Whether it
        # continues on to the Ultra96 depends on where the game engine ends up
        # running, which is not settled; see notes/schedule.md.
        asyncio.create_task(mqtt_client.drain_inbound(inbound, stop)),
        asyncio.create_task(keystroke_reader(nodes, outboxes, stop)),
    ]
    if tunnel_config is not None:
        # Opt-in, because it prompts for a password and only the Ultra96 leg needs
        # it. Loopback and phone-only runs should not have to.
        tasks.append(
            asyncio.create_task(tunnel.run_tunnel(tunnel_config, tunnel_password, stop))
        )
    try:
        # FIRST_COMPLETED, not gather(). gather() waits for *every* task, and
        # tcp_server.run() serves forever with no `stop` to watch, so a quit key
        # would set `stop`, every other leg would retire, and shutdown would block
        # forever on the one task that never finishes. Any leg finishing means the
        # run is over: either the keyboard asked to stop, or something died and
        # .result() re-raises it here instead of leaving the process wedged.
        # Same rule, and the same reason, as mqtt_client.one_session.
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        restore_interrupt()
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # After the gather, not before: the GATT completions this filters arrive
        # *during* teardown, as the BLE legs are cancelled out of stop_notify and
        # disconnect. Restoring any earlier would put the default handler back just
        # in time to print the thing the filter exists to suppress.
        restore_exc_handler()


def main():
    """Entry point for the whole relay."""
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
        "--tunnel",
        action="store_true",
        help="also supervise the SSH reverse tunnel here; prompts for the jump-host password",
    )
    args = parser.parse_args()

    log_file = setup_logging(LOG_FILE, args.verbose, debug_libs=args.debug_libs)
    suppress_queue_get_shutdown_warning()
    broker = resolve_broker(args.host, args.port, args.tls)

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
        "broker %s; TCP %s:%d; nodes %s; logging to %s",
        broker.describe(),
        args.tcp_host,
        args.tcp_port,
        [node.node_id for node in args.nodes],
        log_file,
    )

    # Before the loop starts: getpass blocks, and it must not block the loop.
    # Read once here and hand it down, so every later reconnect is unattended.
    if tunnel_config is not None:
        log.info("tunnel %s", tunnel_config.describe())
        tunnel_password = tunnel.load_password(tunnel_config.jump_host)

    # From an entry point only, never at import: forcing a process-wide policy from
    # a library module is how this resurfaces as an unexplained bleak failure (see
    # mqtt_client.use_selector_event_loop).
    #
    # Three libraries share this loop, and each was checked separately:
    #   aiomqtt   needs the selector loop, which is why this call exists.
    #   bleak     verified here 2026-09-12 (2966 packets, 100 Hz, no loss).
    #   asyncssh  only its *socket layer* is verified on the selector loop. Its
    #             standalone proof run, VPN-drop recovery included, was on the
    #             default Proactor loop, and a real session with a remote port
    #             forward has not been run here. Treat --tunnel as unproven until
    #             it has.
    mqtt_client.use_selector_event_loop()

    try:
        asyncio.run(
            run_pipeline(
                args.nodes,
                broker,
                args.tcp_host,
                args.tcp_port,
                tunnel_config,
                tunnel_password,
            )
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
