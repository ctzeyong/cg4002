"""Start configuration A's board client (tcp_client.py) over asyncssh, with no password prompt.

Pane 2 of two; pane 1 is run_pipeline_a.ps1, and run_board_a.ps1 wraps this file.
run_board.py does the same for configuration B, and the SSH plumbing comes from it.
What differs is what runs on the board, so that part lives here, like every A/B pair:

- A's client publishes nothing, so there is no broker check.
- B's client is always stopped first. tcp_server keeps the newest client, so a leftover
  B client and this one would keep evicting each other, every 2 s, with no error.

    python run_board_a.py                  # start it, or re-attach if running
    python run_board_a.py --sync           # re-copy ultra96/*.py, then restart it
    python run_board_a.py --stop           # kill it and exit
    python run_board_a.py --fetch-logs     # copy today's (UTC) board logs here, then exit

Ctrl-C stops the board client. Full-chain runbook: notes/demo_network.md.
"""

import argparse
import asyncio
import time

import asyncssh

import run_board
from run_board import (
    AS_ROOT,
    DAY_FOLDER,
    PKILL,
    REPO,
    STOP_TIMEOUT_S,
    VENV_PYTHON,
    fetch_logs,
    install_interrupt_handler,
    log,
    stream,
    sync_sources,
    tunnel,
)
from logging_setup import setup_logging

LOG_FILE = REPO / "logs" / "run_board_a.log"

CLIENT = "~/comms/tcp_client.py"
# Must start at "python", or pgrep/pkill would also match our own remote shell. Does not
# match tcp_client_mqtt.py.
PROCESS_PATTERN = r"^[^ ]*python[0-9.]*[ ]+[^ ]*tcp_client\.py"
# Its own prefix: B's attach picks the newest console_*.log, which must never be A's.
CAPTURE_PREFIX = "a_console_"


def remote_command(port, client_args, restart):
    """Build one shell command for the board: stop B, check, start or attach, then tail."""
    extra = (" " + " ".join(client_args)) if client_args else ""
    launch = (
        f"cap=~/comms/logs/{CAPTURE_PREFIX}$(date -u +%Y%m%dT%H%M%SZ).log; "
        f"nohup {AS_ROOT} {VENV_PYTHON} {CLIENT} --host 127.0.0.1 --port {port}{extra} "
        f'> "$cap" 2>&1 &'
    )
    steps = [
        f'{PKILL} -f "{run_board.PROCESS_PATTERN}" && echo "STOPPED: configuration B client killed"',
    ]
    if restart:
        steps.append(f'{PKILL} -f "{PROCESS_PATTERN}" && echo "STOPPED: previous client killed"')
    steps += [
        "sleep 1",
        f"if ! ss -ltn | grep -q '127.0.0.1:{port} '; then "
        f"echo 'WARN: nothing listening on 127.0.0.1:{port} -- is the laptop tunnel up?'; fi",
        DAY_FOLDER,
        f'if pgrep -f "{PROCESS_PATTERN}" >/dev/null; then echo "ATTACH: client already running"; '
        f'pgrep -af "{PROCESS_PATTERN}"; '
        f"latest=$(ls -t ~/comms/logs/{CAPTURE_PREFIX}*.log 2>/dev/null | head -1); "
        f'else {launch} sleep 2; echo "STARTED"; latest="$cap"; fi',
        # A client started by hand has no console capture; tail its own log file instead.
        # tcp_client_[0-9]* so B's tcp_client_mqtt_*.log never matches.
        'if [ -z "$latest" ]; then '
        "latest=$(ls -t ~/comms/logs/*/tcp_client_[0-9]*.log 2>/dev/null | head -1); "
        '[ -n "$latest" ] && echo "no console capture; tailing the per-run client log"; fi',
        'if [ -z "$latest" ]; then echo "ERROR: nothing to tail and no client running"; exit 1; fi',
        'echo "tailing $latest"',
        'exec tail -n 200 -f "$latest"',
    ]
    return "; ".join(steps)


def stop_command():
    """Kill the client and say whether there was one."""
    return (
        f'{PKILL} -f "{PROCESS_PATTERN}" && echo "STOPPED" '
        '|| echo "nothing to stop: no client was running"'
    )


async def stop_client(board):
    """Stop the board client over the open connection and log the result."""
    log.info("stopping the board client ...")
    try:
        result = await asyncio.wait_for(
            board.run(stop_command(), check=False), timeout=STOP_TIMEOUT_S
        )
    except (asyncio.TimeoutError, asyncssh.Error, OSError) as exc:
        log.warning("could not stop the board client (%s); --stop ends it", exc)
        return
    log.info("%s", result.stdout.strip() or "no output")


async def attach(board, command):
    """Show the client's log until Ctrl-C, then stop the client. Same as run_board.attach."""
    process = await board.create_process(command, term_type="xterm")
    stop = asyncio.Event()
    restore = install_interrupt_handler(stop)
    streaming = asyncio.create_task(stream(process))
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({streaming, stopping}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        restore()
        for task in (streaming, stopping):
            task.cancel()
        await asyncio.gather(streaming, stopping, return_exceptions=True)
    if stop.is_set():
        await stop_client(board)


async def run(config, password, args):
    """Open both hops, do the work, and stream until interrupted."""
    session = {"keepalive_interval": tunnel.KEEPALIVE_INTERVAL_S,
               "keepalive_count_max": tunnel.KEEPALIVE_COUNT_MAX}

    async with asyncssh.connect(
        config.jump_host, username=config.jump_user, password=password, **session
    ) as jump:
        log.info("authenticated to the jump host")
        async with asyncssh.connect(
            config.board_host, username=config.board_user, tunnel=jump, **session
        ) as board:
            log.info("connected to the board")
            if args.stop:
                await stop_client(board)
                return
            if args.fetch_logs:
                await fetch_logs(board, args.fetch_logs)
                return
            if args.sync:
                await sync_sources(board)
            await attach(board, remote_command(args.port, args.client_args, restart=args.sync))


def parse_args(argv=None):
    """Our flags; every other argument (e.g. -v) is passed on to the board client."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="Any other argument is passed to tcp_client.py, e.g. -v --event-every 2",
    )
    parser.add_argument("--sync", action="store_true", help="re-copy ultra96/*.py, then restart")
    parser.add_argument("--stop", action="store_true", help="kill the client and exit")
    parser.add_argument("--port", type=int, default=None, help="override TUNNEL_PORT")
    parser.add_argument(
        "--fetch-logs",
        nargs="?",
        const=time.strftime("%Y-%m-%d", time.gmtime()),
        metavar="YYYY-MM-DD",
        help="copy that UTC day's board logs to logs/board/ and exit (default: today)",
    )
    args, client_args = parser.parse_known_args(argv)
    args.client_args = client_args
    return args


def main():
    """Entry point: load .env settings and the password, then run until Ctrl-C."""
    args = parse_args()

    log_file = setup_logging(LOG_FILE)
    config = tunnel.load_config()
    if args.port is None:
        args.port = config.port

    # Hostnames on screen are fine; the NUSNET id is not.
    log.info(
        "configuration A board client via %s, port %d; logging to %s",
        config.jump_host,
        args.port,
        log_file,
    )

    password = tunnel.load_password(config.jump_host)
    try:
        asyncio.run(run(config, password, args))
    except asyncssh.PermissionDenied as exc:
        # Never retried: repeating a bad password could lock the NUS account.
        raise SystemExit(f"authentication rejected: {exc.reason or exc}")
    except KeyboardInterrupt:
        log.info("interrupted; the board client may still be running (--stop ends it)")


if __name__ == "__main__":
    main()
