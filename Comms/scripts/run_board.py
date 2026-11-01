"""Start configuration B's board client and game engine over asyncssh, with no password prompt.

Opens both SSH hops, starts tcp_client_mqtt.py and the game engine on the Ultra96
(or re-attaches to them), then shows the client's log here. Pane 2 of two; pane 1 is
run_pipeline.ps1, and run_board.ps1 wraps this file. Design: notes/tunnelled_visualiser.md.

    python run_board.py                  # start both, or re-attach if running
    python run_board.py --sync           # re-copy ultra96/*.py and the engine, then restart both
    python run_board.py --stop           # kill both and exit
    python run_board.py --fetch-logs     # copy today's (UTC) board logs here, then exit
    python run_board.py -v --event-every 2

Ctrl-C stops the board client and the engine. They run detached, so a dropped SSH
connection does not stop them; to stop watching but leave them running, close the window.
"""

import argparse
import asyncio
import logging
import re
import signal
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# This script lives in scripts/ but the laptop modules use flat imports.
sys.path.insert(0, str(REPO / "laptop"))

import asyncssh  # noqa: E402

import tunnel  # noqa: E402
from logging_setup import LINK_COLOURS, setup_logging  # noqa: E402

LOG_FILE = REPO / "logs" / "run_board.log"
# Where --fetch-logs puts the board's own log files, one folder per UTC day.
FETCHED_LOGS = REPO / "logs" / "board"
BOARD_SOURCES = REPO / "ultra96"
# AI's model and its bitstream, copied next to gesture.py on the board
# must match gesture.AI_MODULE and gesture.AI_BITSTREAM
AI_MODULE = REPO.parent / "AI" / "board" / "wk10_integration.py"
AI_BITSTREAM = REPO.parent / "AI" / "board" / "baseline_improved.xsa"

# Call the venv's python directly rather than relying on an activated venv.
VENV_PYTHON = "~/comms-venv/bin/python"
# Root, because pynq refuses to reach the FPGA without it ("Root permissions required"),
# for loading the bitstream and for every register access after.
# -n fails at once instead of waiting for a password nobody can type. HOME is kept so the
# client's own logs still go to ~/comms/logs, not /root. sudo also drops XILINX_XRT, which
# the login shell sets, and without it pynq finds no FPGA ("No Devices Found").
# setsid takes it off the SSH terminal, so a dropped session cannot reach it through sudo.
AS_ROOT = 'setsid sudo -n env HOME="$HOME" XILINX_XRT=/usr'
# The client runs as root, so stopping it needs root too.
PKILL = "sudo -n pkill"
CLIENT = "~/comms/tcp_client_mqtt.py"
# Must start at "python", or pgrep/pkill would also match our own remote shell.
PROCESS_PATTERN = r"^[^ ]*python[0-9.]*[ ]+[^ ]*tcp_client_mqtt\.py"
# Configuration A's client, stopped before B's starts. A copy of run_board_a.PROCESS_PATTERN,
# because that file imports this one; tests/test_run_board_a.py keeps the two equal.
A_PROCESS_PATTERN = r"^[^ ]*python[0-9.]*[ ]+[^ ]*tcp_client\.py"
# Today's (UTC) log folder, made here as xilinx before the launch.
# Same name as board_logging.run_log_path.
DAY_FOLDER = 'mkdir -p ~/comms/logs/"$(date -u +%Y-%m-%d)"'

# The game engine runs as the normal user, under the restart loop in ultra96/run_engine.sh.
ENGINE_LAUNCHER = BOARD_SOURCES / "run_engine.sh"
ENGINE_LOOP = "~/comms/run_engine.sh"
# The engine's core and rules.json, which Vis writes, copied to ~/comms/engine/.
ENGINE_CORE = REPO.parent / "SW_Visualiser" / "engine"
# Anchored at the interpreter like PROCESS_PATTERN, so neither matches our own remote shell.
ENGINE_LOOP_PATTERN = r"^[^ ]*bash[ ]+[^ ]*run_engine\.sh"
ENGINE_PATTERN = r"^[^ ]*python[0-9.]*[ ]+[^ ]*game_engine\.py"
# The loop first, or it would start the engine again.
ENGINE_KILL = (
    f'{PKILL} -f "{ENGINE_LOOP_PATTERN}" && echo "STOPPED: engine restart loop"; '
    f'{PKILL} -f "{ENGINE_PATTERN}" && echo "STOPPED: engine"'
)
# Starts the loop unless it's running, with its output in the day folder so --fetch-logs
# brings it back. If the loop has already stopped a second later, show why.
ENGINE_START = (
    f'if pgrep -f "{ENGINE_LOOP_PATTERN}" >/dev/null; then echo "ENGINE: already running"; '
    f"elif [ -f {ENGINE_LOOP} ]; then "
    'ecap=~/comms/logs/"$(date -u +%Y-%m-%d)"/run_engine_$(date -u +%Y%m%dT%H%M%SZ).log; '
    f'nohup setsid bash {ENGINE_LOOP} > "$ecap" 2>&1 & sleep 1; '
    f'if pgrep -f "{ENGINE_LOOP_PATTERN}" >/dev/null; then echo "ENGINE: started, log $ecap"; '
    'else echo "WARN: the engine stopped at once:"; cat "$ecap"; fi; '
    "else echo 'WARN: no run_engine.sh on the board, so no engine (--sync copies it)'; fi"
)

# A board log line, so its time/name prefix can be stripped before re-logging here.
# (The format has no level, so board warnings show here as INFO.)
BOARD_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}Z \[([\w.]+)\] (.*)$")

STOP_TIMEOUT_S = 10.0  # so Ctrl-C cannot hang on a dead connection

log = logging.getLogger("run_board")
remote_log = logging.getLogger("board")


def remote_command(port, client_args, restart):
    """Build one shell command for the board: checks, start or attach, then tail the log.

    The checks warn if the tunnel port or the broker is missing, or the broker is not loopback-only.
    """
    extra = (" " + " ".join(client_args)) if client_args else ""
    launch = (
        f"cap=~/comms/logs/console_$(date -u +%Y%m%dT%H%M%SZ).log; "
        f"nohup {AS_ROOT} {VENV_PYTHON} {CLIENT} --host 127.0.0.1 --port {port}{extra} "
        f'> "$cap" 2>&1 &'
    )
    steps = [
        # tcp_server keeps the newest client, so a leftover A client and this one would
        # evict each other every 2 s, with no error. Mirror of run_board_a.py.
        f'{PKILL} -f "{A_PROCESS_PATTERN}" && echo "STOPPED: configuration A client killed"',
    ]
    if restart:
        steps.append(f'{PKILL} -f "{PROCESS_PATTERN}" && echo "STOPPED: previous client killed"')
        steps.append(ENGINE_KILL)
        steps.append("sleep 1")
    steps += [
        f"if ! ss -ltn | grep -q '127.0.0.1:{port} '; then "
        f"echo 'WARN: nothing listening on 127.0.0.1:{port} -- is the laptop tunnel up?'; fi",
        "if ! ss -ltn | grep -q ':1883 '; then "
        "echo 'WARN: no broker on 1883 -- sudo systemctl start mosquitto'; fi",
        "ss -ltn | grep ':1883 ' | grep -qv '127.0.0.1\\|::1' "
        "&& echo 'WARN: the broker is bound wider than loopback'",
        DAY_FOLDER,
        # Tail the file this launch created, not just the newest (maybe an old run's).
        f'if pgrep -f "{PROCESS_PATTERN}" >/dev/null; then echo "ATTACH: client already running"; '
        f'pgrep -af "{PROCESS_PATTERN}"; '
        "latest=$(ls -t ~/comms/logs/console_*.log 2>/dev/null | head -1); "
        f'else {launch} sleep 2; echo "STARTED"; latest="$cap"; fi',
        # After the client's if, so the engine starts whether the client was running or not.
        ENGINE_START,
        # A client started by hand has no console capture; tail its own log file instead.
        'if [ -z "$latest" ]; then '
        "latest=$(ls -t ~/comms/logs/*/tcp_client_mqtt_*.log 2>/dev/null | head -1); "
        '[ -n "$latest" ] && echo "no console capture; tailing the per-run client log"; fi',
        'if [ -z "$latest" ]; then echo "ERROR: nothing to tail and no client running"; exit 1; fi',
        'echo "tailing $latest"',
        'exec tail -n 200 -f "$latest"',
    ]
    return "; ".join(steps)


def stop_command():
    """Kill the client and the engine, and say which were running."""
    return (
        f'{PKILL} -f "{PROCESS_PATTERN}" && echo "STOPPED" '
        '|| echo "nothing to stop: no client was running"; '
        + ENGINE_KILL
    )


def relog(line):
    """Show one board log line here, in that link's colour.

    Goes through logging (not print) so it stays in order with our own lines.
    """
    match = BOARD_LINE.match(line)
    if not match:
        remote_log.info("%s", line)
        return
    name, message = match.groups()
    target = logging.getLogger(name) if name in LINK_COLOURS else remote_log
    target.info("%s", message)


async def sync_sources(conn):
    """Copy every ultra96/*.py, plus AI's model and its bitstream, to ~/comms/ on the board.

    The whole folder, not a list of names, so a new file is never left behind.
    """
    files = sorted(BOARD_SOURCES.glob("*.py"))
    if not files:
        raise SystemExit(f"no sources found in {BOARD_SOURCES}")
    for needed in (AI_MODULE, AI_BITSTREAM):
        if not needed.exists():
            raise SystemExit(f"missing {needed}, which gesture.py needs on the board")
        files.append(needed)
    async with conn.start_sftp_client() as sftp:
        await sftp.makedirs("comms", exist_ok=True)
        await sftp.put([str(path) for path in files], "comms/")
    log.info("synced %d files to ~/comms/", len(files))


async def sync_engine(conn):
    """Copy run_engine.sh to ~/comms/ and the engine's core folder to ~/comms/engine/.

    The script goes with LF line endings, since bash on the board fails on a CRLF checkout.
    """
    if not ENGINE_CORE.is_dir():
        raise SystemExit(f"missing {ENGINE_CORE}, which the engine needs on the board")
    core = sorted(path for path in ENGINE_CORE.iterdir() if path.is_file())
    async with conn.start_sftp_client() as sftp:
        async with sftp.open("comms/run_engine.sh", "wb") as remote:
            await remote.write(ENGINE_LAUNCHER.read_bytes().replace(b"\r\n", b"\n"))
        await sftp.makedirs("comms/engine", exist_ok=True)
        await sftp.put([str(path) for path in core], "comms/engine/")
    log.info("synced run_engine.sh, and %d file(s) to ~/comms/engine/", len(core))


async def fetch_logs(conn, day):
    """Copy one UTC day's board logs into logs/board/<day>/. Only reads the board.

    The board's own files, not this script's relay of them: the relay re-stamps lines
    and delivers them in bunches. notes/latency.md 3.5.
    """
    remote = f"comms/logs/{day}"
    local = FETCHED_LOGS / day
    async with conn.start_sftp_client() as sftp:
        if not await sftp.isdir(remote):
            log.warning("no board logs for %s", day)
            return
        names = sorted(n for n in await sftp.listdir(remote) if n.endswith(".log"))
        local.mkdir(parents=True, exist_ok=True)
        await sftp.get([f"{remote}/{name}" for name in names], str(local))
    log.info("fetched %d board log(s) to %s", len(names), local)


async def stream(process):
    """Relay the remote output until it ends or this task is cancelled."""
    async for line in process.stdout:
        relog(line.rstrip("\r\n"))


def install_interrupt_handler(stop):
    """Make Ctrl-C set `stop` instead of raising; a second Ctrl-C exits as normal.

    Same as pipeline.install_interrupt_handler. Returns a function that restores the old handler.
    """
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)

    def handle(signum, frame):
        signal.signal(signal.SIGINT, previous)
        # Same text as the relay prints, so both windows match.
        log.info("interrupt received; shutting down (Ctrl-C again to force)")
        loop.call_soon_threadsafe(stop.set)

    signal.signal(signal.SIGINT, handle)
    return lambda: signal.signal(signal.SIGINT, previous)


async def stop_client(board):
    """Stop the board client and the engine over the open connection and log the result."""
    log.info("stopping the board client and the engine ...")
    try:
        result = await asyncio.wait_for(
            board.run(stop_command(), check=False), timeout=STOP_TIMEOUT_S
        )
    except (asyncio.TimeoutError, asyncssh.Error, OSError) as exc:
        log.warning("could not stop the board client and the engine (%s); --stop ends them", exc)
        return
    log.info("%s", result.stdout.strip() or "no output")


async def attach(board, command):
    """Show the client's log until Ctrl-C, then stop the client and the engine.

    If the log stream ends by itself (e.g. nothing to tail), both keep running.
    """
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
        # No key or password for the board: hop 2 is key-only. See tunnel.py.
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
                await sync_engine(board)
            await attach(board, remote_command(args.port, args.client_args, restart=args.sync))


def parse_args(argv=None):
    """Our three flags; every other argument (e.g. -v) is passed on to the board client."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="Any other argument is passed to tcp_client_mqtt.py, e.g. -v --event-every 2",
    )
    parser.add_argument(
        "--sync", action="store_true", help="re-copy ultra96/*.py and the engine, then restart both"
    )
    parser.add_argument("--stop", action="store_true", help="kill the client and the engine")
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
    log.info("board client via %s, port %d; logging to %s", config.jump_host, args.port, log_file)

    password = tunnel.load_password(config.jump_host)
    try:
        asyncio.run(run(config, password, args))
    except asyncssh.PermissionDenied as exc:
        # Never retried: repeating a bad password could lock the NUS account.
        raise SystemExit(f"authentication rejected: {exc.reason or exc}")
    except KeyboardInterrupt:
        # Ctrl-C before attach() took over, or a second Ctrl-C to force the exit.
        log.info("interrupted; the board client may still be running (--stop ends it)")


if __name__ == "__main__":
    main()
