"""Start the board client and game engine over asyncssh, with no password prompt.

Opens both SSH hops, starts tcp_client_mqtt.py and the game engine on the Ultra96
(or re-attaches to them), then shows the client's log here. Pane 2 of two; pane 1 is
run_pipeline.ps1, and run_board.ps1 wraps this file.

    python run_board.py                  # start both, or re-attach if running
    python run_board.py --sync           # re-copy ultra96/*.py and the engine, then restart both
    python run_board.py --stop           # kill both and exit
    python run_board.py --fetch-logs     # copy today's (UTC) board logs here, then exit
    python run_board.py -v --event-every 2
    python run_board.py --power          # also record the board's power, idle vs playing
    python run_board.py --cpu-mhz 300 --cores 1 --pl-mhz 25   # tune the board, with --power

Ctrl-C stops the board client and the engine. They run detached, so a dropped SSH
connection does not stop them; to stop watching but leave them running, close the window.
"""

import argparse
import asyncio
import calendar
import csv
import logging
import re
import signal
import sys
import time
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parent.parent
# This script lives in scripts/ but the laptop modules use flat imports.
sys.path.insert(0, str(REPO / "laptop"))

import asyncssh  # noqa: E402

import tunnel  # noqa: E402
from logging_setup import LINK_COLOURS, setup_logging  # noqa: E402

LOG_FILE = REPO / "logs" / "run_board.log"
# Where --fetch-logs puts the board's own log files, one folder per UTC day.
FETCHED_LOGS = REPO / "logs" / "board"
# The logs, and the power monitor's readings and summary.
FETCHED_TYPES = (".log", ".csv")
BOARD_SOURCES = REPO / "ultra96"
# AI's model and overlays live here. The model is always synced; the overlay named by
# --bitstream is synced beside it (must match gesture.AI_MODULE).
AI_BOARD_DIR = REPO.parent / "AI" / "ultra96"
AI_MODULE = AI_BOARD_DIR / "wk10_integration.py"

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
# The laptop-broker client, stopped before this one starts. A copy of run_board_a.PROCESS_PATTERN,
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

# The power monitor (ultra96/power_monitor.py), as root like the client, since pynq needs it.
POWER_SCRIPT = "~/comms/power_monitor.py"
POWER_PATTERN = r"^[^ ]*python[0-9.]*[ ]+[^ ]*power_monitor\.py"
POWER_KILL = f'{PKILL} -f "{POWER_PATTERN}" && echo "STOPPED: power monitor"'
# The tuning flags: which process applies each on the board, and the values offered here
CPU_MHZ = (300, 400, 600, 1200)  # the board's scaling_available_frequencies, in MHz
CORES = (1, 2, 3, 4)
PL_MHZ_RANGE = (10.0, 100.0)  # never above the 100 MHz the overlays set


def power_start(monitor_args=""):
    """Start the monitor with `monitor_args` (its tuning flags), with its output in the day folder
    so --fetch-logs brings it back.

    One already running with the same flags is left alone, so a rerun does not split a session;
    one with other flags is stopped first, which puts its settings back, then started again.
    """
    same = f"{POWER_PATTERN}{monitor_args}$"
    return (
        f'if pgrep -f "{same}" >/dev/null; then echo "POWER: already running"; else '
        f'if pgrep -f "{POWER_PATTERN}" >/dev/null; then {PKILL} -f "{POWER_PATTERN}"; '
        'echo "POWER: restarting the monitor with the new settings"; '
        f'for i in $(seq 20); do pgrep -f "{POWER_PATTERN}" >/dev/null || break; sleep 0.5; '
        "done; fi; "
        'pcap=~/comms/logs/"$(date -u +%Y-%m-%d)"/power_console_$(date -u +%Y%m%dT%H%M%SZ).log; '
        f'nohup {AS_ROOT} {VENV_PYTHON} {POWER_SCRIPT}{monitor_args} > "$pcap" 2>&1 & sleep 2; '
        f'if pgrep -f "{POWER_PATTERN}" >/dev/null; then echo "POWER: recording, log $pcap"; '
        'else echo "WARN: the power monitor stopped at once:"; cat "$pcap"; fi; fi'
    )


POWER_START = power_start()
# After the stop: wait (up to 10 s) for the monitor to write its summary row, then print the
# newest power_summary.csv's path, header and last row.
POWER_REPORT = (
    f'for i in $(seq 20); do pgrep -f "{POWER_PATTERN}" >/dev/null || break; sleep 0.5; done; '
    "f=$(ls -t ~/comms/logs/*/power_summary.csv 2>/dev/null | head -1); "
    '[ -n "$f" ] && echo "$f" && head -n 1 "$f" && tail -n 1 "$f"'
)
POWER_REPORT_TIMEOUT_S = 20.0
POWER_FRESH_S = 120.0  # a summary row older than this is not the session just stopped
FETCH_TIMEOUT_S = 60.0

# A board log line, so its time/name prefix can be stripped before re-logging here.
# (The format has no level, so board warnings show here as INFO.)
BOARD_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}Z \[([\w.]+)\] (.*)$")

STOP_TIMEOUT_S = 10.0  # so Ctrl-C cannot hang on a dead connection

log = logging.getLogger("run_board")
remote_log = logging.getLogger("board")


def remote_command(port, client_args, restart, power=False, monitor_args=""):
    """Build one shell command for the board: checks, start or attach, then tail the log.

    The checks warn if the tunnel port or the broker is missing, or the broker is not loopback-only.
    `power` also starts the power monitor, with `monitor_args` (its tuning flags).
    """
    # the client sets the PL clock as it loads the overlay, so not when we only attach to it
    pl_note = ('echo "WARN: --pl-mhz only applies as the client starts; '
               'add --sync to restart it"; ' if "--pl-mhz" in client_args else "")
    extra = (" " + " ".join(client_args)) if client_args else ""
    launch = (
        f"cap=~/comms/logs/console_$(date -u +%Y%m%dT%H%M%SZ).log; "
        f"nohup {AS_ROOT} {VENV_PYTHON} {CLIENT} --host 127.0.0.1 --port {port}{extra} "
        f'> "$cap" 2>&1 &'
    )
    steps = [
        # tcp_server keeps the newest client, so a leftover laptop-broker client and this one would
        # evict each other every 2 s, with no error. Mirror of run_board_a.py.
        f'{PKILL} -f "{A_PROCESS_PATTERN}" && echo "STOPPED: configuration A client killed"',
    ]
    if restart:
        steps.append(f'{PKILL} -f "{PROCESS_PATTERN}" && echo "STOPPED: previous client killed"')
        steps.append(ENGINE_KILL)
        steps.append(POWER_KILL)
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
        f'{pl_note}pgrep -af "{PROCESS_PATTERN}"; '
        "latest=$(ls -t ~/comms/logs/console_*.log 2>/dev/null | head -1); "
        f'else {launch} sleep 2; echo "STARTED"; latest="$cap"; fi',
        # After the client's if, so the engine starts whether the client was running or not.
        ENGINE_START,
        *([power_start(monitor_args)] if power else []),
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
    """Kill the client, the engine and the power monitor, and say which were running."""
    return (
        f'{PKILL} -f "{PROCESS_PATTERN}" && echo "STOPPED" '
        '|| echo "nothing to stop: no client was running"; '
        + ENGINE_KILL
        + "; "
        + POWER_KILL
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


async def sync_sources(conn, bitstream=None):
    """Copy every ultra96/*.py and AI's model to ~/comms/ on the board, plus the overlay
    named by --bitstream when one was given.

    The whole folder, not a list of names, so a new file is never left behind.
    """
    files = sorted(BOARD_SOURCES.glob("*.py"))
    if not files:
        raise SystemExit(f"no sources found in {BOARD_SOURCES}")
    needed = [AI_MODULE]
    if bitstream:
        needed.append(AI_BOARD_DIR / bitstream)
    for path in needed:
        if not path.exists():
            raise SystemExit(f"missing {path}, which gesture.py needs on the board")
        files.append(path)
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
    and delivers them in bunches.
    """
    remote = f"comms/logs/{day}"
    local = FETCHED_LOGS / day
    async with conn.start_sftp_client() as sftp:
        if not await sftp.isdir(remote):
            log.warning("no board logs for %s", day)
            return
        names = sorted(n for n in await sftp.listdir(remote) if n.endswith(FETCHED_TYPES))
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
    # pkill prints nothing when there was nothing to kill, any stderr means the stop failed
    if (result.stderr or "").strip():
        log.warning("the board said: %s", result.stderr.strip())


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
    return stop.is_set()


def power_report(output, now=None):
    """(day folder, one-line summary) from POWER_REPORT's output, or None if it found no summary.

    The line warns when the newest row is not this session's, e.g. because the monitor died.
    """
    lines = output.strip().splitlines()
    if len(lines) < 3:
        return None
    path, header, last = lines[0], lines[1], lines[-1]
    row = next(csv.DictReader([header, last]))
    day = PurePosixPath(path).parent.name

    def part(state):
        seconds = float(row.get(f"{state}_s") or 0)
        total = row.get(f"{state}_TOTAL")
        return f"{state} {float(total):.3f} W over {seconds:.0f} s" if total else f"no {state} readings"

    line = f"power this session: {part('idle')}, {part('playing')}"
    if row.get("playing_minus_idle_W"):
        line += f", playing uses {float(row['playing_minus_idle_W']):+.3f} W"
    line += f", {row.get('gestures', '?')} gesture(s)"
    if row.get("cpu_mhz"):
        pl = f", PL {float(row['pl_mhz']):g} MHz" if row.get("pl_mhz") else ""
        line += f", at CPU {row['cpu_mhz']} MHz, {row.get('cores', '?')} core(s){pl}"
    ended = calendar.timegm(time.strptime(row["end_utc"][:19], "%Y-%m-%dT%H:%M:%S"))
    if (time.time() if now is None else now) - ended > POWER_FRESH_S:
        line += f" -- this is the newest summary, from {row['end_utc']}, not this session's"
    return day, line


async def report_power(board):
    """After Ctrl-C with --power: show the session's totals, then copy its day's files here."""
    try:
        result = await asyncio.wait_for(
            board.run(POWER_REPORT, check=False), timeout=POWER_REPORT_TIMEOUT_S
        )
    except (asyncio.TimeoutError, asyncssh.Error, OSError) as exc:
        log.warning("could not read the power summary (%s); --fetch-logs copies it", exc)
        return
    report = power_report(result.stdout or "")
    if report is None:
        log.warning("no power summary on the board; is power_monitor.py there (--sync)?")
        return
    day, line = report
    log.info("%s", line)
    try:
        await asyncio.wait_for(fetch_logs(board, day), timeout=FETCH_TIMEOUT_S)
    except (asyncio.TimeoutError, asyncssh.Error, OSError) as exc:
        log.warning("could not copy the power files (%s); --fetch-logs %s copies them", exc, day)


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
                await sync_sources(board, args.bitstream)
                await sync_engine(board)
            command = remote_command(
                args.port, args.client_args, restart=args.sync, power=args.power,
                monitor_args=args.monitor_args,
            )
            if await attach(board, command) and args.power:
                await report_power(board)


def bitstream_from_args(client_args):
    """The --bitstream NAME value among the args forwarded to the client, or None.

    Read here only to sync that overlay; it stays in client_args so the client gets it too.
    """
    for i, arg in enumerate(client_args):
        if arg == "--bitstream" and i + 1 < len(client_args):
            return client_args[i + 1]
        if arg.startswith("--bitstream="):
            return arg.split("=", 1)[1]
    return None


def pl_mhz(text):
    """argparse type for --pl-mhz: a number in PL_MHZ_RANGE."""
    value = float(text)
    low, high = PL_MHZ_RANGE
    if not low <= value <= high:
        raise argparse.ArgumentTypeError(f"{text}: choose {low:g} to {high:g} MHz")
    return value


def parse_args(argv=None):
    """Our flags; every other argument (e.g. -v, --bitstream) is passed on to the board client."""
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
    parser.add_argument(
        "--power",
        action="store_true",
        help="also record the board's power, split into idle and playing (power_monitor.py)",
    )
    tuning = parser.add_argument_group(
        "tuning the board for the session (each implies --power; all put back on Ctrl-C)")
    tuning.add_argument("--cpu-mhz", type=int, choices=CPU_MHZ, help="CPU clock")
    tuning.add_argument("--cores", type=int, choices=CORES, help="CPU cores left on (cpu0 stays)")
    tuning.add_argument("--pl-mhz", type=pl_mhz, metavar="MHZ",
                        help="FPGA clock, %g to %g; the client sets it as it loads the overlay"
                        % PL_MHZ_RANGE)
    args, client_args = parser.parse_known_args(argv)
    args.monitor_args = "".join(f" --{flag} {value}" for flag, value in
                                (("cpu-mhz", args.cpu_mhz), ("cores", args.cores))
                                if value is not None)
    if args.pl_mhz is not None:
        client_args += ["--pl-mhz", f"{args.pl_mhz:g}"]
    if args.monitor_args or args.pl_mhz is not None:
        args.power = True  # so the dashboard shows what the tuning saves
    args.client_args = client_args
    # --bitstream is forwarded to the client; read (not consumed) here to sync that overlay too
    args.bitstream = bitstream_from_args(client_args)
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
