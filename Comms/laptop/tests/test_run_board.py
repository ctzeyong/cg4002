"""Tests for scripts/run_board.py: the remote command it builds, and relog.

The two SSH hops are not exercised here, for the same reason `test_tunnel.py`
skips `one_session`: faking them would only test the fake. What is worth pinning
is the shell it composes, since a wrong branch there fails silently on the board.
"""

import asyncio
import logging
import re
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import DEADLINE_S, wait_until

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import board_logging  # noqa: E402
import run_board  # noqa: E402
import run_board_a  # noqa: E402


def launch_branch(command):
    """Only the else-branch, bounded by its `fi`.

    Bounded on purpose: taking everything after `else` also swallowed the
    attach-fallback glob that follows the `fi`, which made this read as a failure.
    """
    return command.split("else", 1)[1].split("fi", 1)[0]


def attach_branch(command):
    """Only the then-branch, where a client is already running."""
    return command.split("then", 1)[1].split("else", 1)[0]


def test_launch_tails_the_capture_it_created_not_the_newest():
    """A launch that died would otherwise tail a PREVIOUS run's capture.

    `ls -t | head -1` looked equivalent to `$cap` and is not: on a board that has
    run before, a failed start would stream stale output as though it were live.
    """
    branch = launch_branch(run_board.remote_command(8888, [], restart=False))

    assert 'latest="$cap"' in branch
    assert "ls -t" not in branch


def test_the_launch_branch_never_globs_but_the_attach_branch_must():
    """Globbing is correct when attaching -- the capture belongs to the live run.

    Not asserted as a count: the attach fallback after the `fi` globs too, which
    is deliberate and is what a hand-started client needs.
    """
    command = run_board.remote_command(8888, [], restart=False)

    assert "ls -t ~/comms/logs/console_*.log" in attach_branch(command)
    assert "ls -t" not in launch_branch(command)


def test_sync_kills_the_previous_client_before_relaunching():
    """Attaching after a sync would run the code you just replaced."""
    command = run_board.remote_command(8888, [], restart=True)

    assert command.index("pkill") < command.index("pgrep")


def test_a_plain_start_does_not_kill_bs_own_client():
    """Only --sync and --stop may kill B's own client; a normal start must be able to
    attach. Stopping a configuration A client is allowed: it would block B's anyway."""
    command = run_board.remote_command(8888, [], restart=False)
    assert f'pkill -f "{run_board.PROCESS_PATTERN}"' not in command
    assert command.count("pkill") == 1  # A's, and nothing else


def test_client_arguments_reach_the_client_and_not_the_shell():
    """Forwarded args belong after the client path, inside the nohup line."""
    command = run_board.remote_command(8888, ["-v", "--event-every", "2"], restart=False)

    assert "tcp_client_mqtt.py --host 127.0.0.1 --port 8888 -v --event-every 2" in command


def test_relog_strips_the_boards_prefix_instead_of_nesting_it(caplog):
    """Re-logging verbatim would print two timestamps and two names per line."""
    line = "2026-09-22 09:39:58,123Z [tcp_client_mqtt] AI event seq=0 player=1"

    with caplog.at_level(logging.INFO):
        run_board.relog(line)

    record = caplog.records[-1]
    assert record.getMessage() == "AI event seq=0 player=1"
    # The board's logger name is kept, so logging_setup gives it that leg's colour.
    assert record.name == "tcp_client_mqtt"


def test_relog_only_honours_logger_names_it_has_a_colour_for(caplog):
    """An unmapped name must not become a logger; remote text is not trusted."""
    line = "2026-09-22 09:39:58,123Z [something_else] hello"

    with caplog.at_level(logging.INFO):
        run_board.relog(line)

    assert caplog.records[-1].name == "board"
    assert caplog.records[-1].getMessage() == "hello"


def test_relog_passes_through_a_line_it_cannot_parse(caplog):
    """A traceback from a failed launch has no prefix and must survive intact."""
    with caplog.at_level(logging.INFO):
        run_board.relog("ModuleNotFoundError: No module named 'aiomqtt'")

    assert "aiomqtt" in caplog.records[-1].getMessage()


SHELL_CMDLINE = (
    "bash -c if ! ss -ltn | grep -q 127.0.0.1:8888 ; then echo WARN; fi; "
    "nohup ~/comms-venv/bin/python ~/comms/tcp_client_mqtt.py --port 8888"
)
CLIENT_CMDLINES = [
    "/home/xilinx/comms-venv/bin/python /home/xilinx/comms/tcp_client_mqtt.py --port 8888",
    "python3 tcp_client_mqtt.py",
]


def matches(cmdline):
    """Approximate `pgrep -f`: the pattern as an ERE against a whole command line."""
    return re.search(run_board.PROCESS_PATTERN, cmdline) is not None


def test_the_process_pattern_does_not_match_our_own_remote_shell():
    """The bug from the first hardware run, 2026-09-22.

    The shell running our command has that command in its own argv, so the bare
    script name matched it: pgrep always said "already running" and then nothing
    could be tailed. Worse, `pkill -f` would have killed that shell.
    """
    assert not matches(SHELL_CMDLINE)


@pytest.mark.parametrize("cmdline", CLIENT_CMDLINES)
def test_the_process_pattern_still_matches_a_real_client(cmdline):
    """Both the venv launch and a hand-started python3 must be found."""
    assert matches(cmdline)


def test_the_process_pattern_ignores_a_leftover_tail():
    """A stale `tail -f console_*.log` names the client but is not one."""
    assert not matches("tail -n 200 -f /home/xilinx/comms/logs/console_20260922T102632Z.log")


def test_attach_falls_back_to_the_clients_own_log():
    """A client started by hand has no console capture; that is not an error.

    The first hardware run died on `ERROR: no console capture found` in exactly
    this state, so the fallback is the fix rather than a nicety.
    """
    command = run_board.remote_command(8888, [], restart=False)

    assert "tcp_client_mqtt_*.log" in command
    assert command.index("console_*.log") < command.index("tcp_client_mqtt_*.log")


def test_attach_reports_which_process_it_matched():
    """`pgrep -af` is what made the self-match visible; keep it printing."""
    assert "pgrep -af" in run_board.remote_command(8888, [], restart=False)


def test_our_flags_are_consumed_and_the_rest_is_forwarded():
    """run_board.ps1 passes arguments through bare, so this is its contract."""
    args = run_board.parse_args(["--sync", "-v", "--event-every", "2"])

    assert args.sync is True
    assert args.client_args == ["-v", "--event-every", "2"]


def test_v_goes_to_the_board_and_is_not_eaten_locally():
    """`-v` means "dump per-packet records" on the board; there is no local -v.

    An argparse `-v` here would swallow it and the board would log nothing extra,
    with no error to say why.
    """
    assert run_board.parse_args(["-v"]).client_args == ["-v"]


def test_stop_is_not_forwarded_to_the_client():
    """--stop is ours; forwarding it would make tcp_client_mqtt.py reject it."""
    args = run_board.parse_args(["--stop"])

    assert args.stop is True
    assert args.client_args == []


def test_fetch_logs_defaults_to_todays_utc_folder_and_is_not_forwarded():
    """The board names its log folders by UTC date. Singapore is UTC+8, so a run
    before 08:00 local sits in the previous day's folder."""
    today = run_board.parse_args(["--fetch-logs"])
    named = run_board.parse_args(["--fetch-logs", "2026-10-01"])

    assert re.fullmatch(r"\d{4}-\d\d-\d\d", today.fetch_logs)
    assert named.fetch_logs == "2026-10-01"
    assert today.client_args == [] and named.client_args == []
    assert run_board.parse_args([]).fetch_logs is None


@pytest.mark.parametrize("port", [8888, 9001])
def test_the_preflight_checks_use_the_configured_port(port):
    """A port mismatch is the silent failure the check exists to catch.

    Asserted on the grep pattern, not on the message: the warning text names the
    port too, so a check hardcoded to 8888 passed a looser version of this test.
    """
    command = run_board.remote_command(port, [], restart=False)

    assert f"grep -q '127.0.0.1:{port} '" in command
    assert f"--port {port}" in command


# --- the client runs as root -------------------------------------------------
# The classifier loads a bitstream, which only root may do

LAUNCHERS = [run_board, run_board_a]


@pytest.mark.parametrize("module", LAUNCHERS)
def test_the_client_is_launched_as_root_and_keeps_home(module):
    """Each part is load-bearing:
    - setsid: off the SSH terminal, so a dropped session cannot reach the client through sudo;
    - sudo -n: fails at once instead of waiting for a password nobody can type;
    - HOME kept: the client logs to ~/comms/logs, where --fetch-logs and attach look, not /root;
    - XILINX_XRT: sudo drops it, and without it pynq raises "No Devices Found" (I1, 15 Oct).
    """
    branch = launch_branch(module.remote_command(8888, [], restart=False))

    expected = (
        'nohup setsid sudo -n env HOME="$HOME" XILINX_XRT=/usr '
        f'~/comms-venv/bin/python {module.CLIENT} '
    )
    assert expected in branch


@pytest.mark.parametrize("module", LAUNCHERS)
@pytest.mark.parametrize("restart", [False, True])
def test_every_kill_runs_as_root(module, restart):
    """A plain pkill cannot kill a root client. --sync would then attach to the old code, and
    the A and B launchers could not stop each other's client, which then evict each other."""
    for command in (module.remote_command(8888, [], restart=restart), module.stop_command()):
        assert command.count("pkill") == command.count("sudo -n pkill -f ") > 0


SUDO_CMDLINES = [
    # sudo stays alive as the client's parent, with the client in its own argv
    "sudo -n env HOME=/home/xilinx /home/xilinx/comms-venv/bin/python "
    "/home/xilinx/comms/tcp_client_mqtt.py --host 127.0.0.1 --port 8888",
    "sudo -n env HOME=/home/xilinx /home/xilinx/comms-venv/bin/python "
    "/home/xilinx/comms/tcp_client.py --host 127.0.0.1 --port 8888",
    # and the kill itself names the pattern
    'sudo -n pkill -f ^[^ ]*python[0-9.]*[ ]+[^ ]*tcp_client_mqtt\\.py',
]


@pytest.mark.parametrize("cmdline", SUDO_CMDLINES)
@pytest.mark.parametrize("pattern", [run_board.PROCESS_PATTERN, run_board_a.PROCESS_PATTERN])
def test_the_process_patterns_do_not_match_sudo(cmdline, pattern):
    """Matching sudo would make pgrep -af list each client twice."""
    assert re.search(pattern, cmdline) is None


@pytest.mark.parametrize("module", LAUNCHERS)
def test_todays_log_folder_is_made_before_the_root_client_starts(module):
    """If the root client made it, the folder would be root's, and a client not run as root
    (the A/B baseline from comms/) could not create its log there that day."""
    command = module.remote_command(8888, [], restart=False)

    assert command.index('mkdir -p ~/comms/logs/"$(date -u +%Y-%m-%d)"') < command.index("nohup")


def test_the_day_folder_is_the_one_board_logging_writes_to():
    """Same UTC date format, or the root client would still make its own folder."""
    stamp = time.strptime("2026-10-15 23:30", "%Y-%m-%d %H:%M")
    path = board_logging.run_log_path(Path("logs/tcp_client.log"), now=stamp)
    date_format = re.search(r"date -u \+(\S+?)\)", run_board.DAY_FOLDER).group(1)

    assert path.parent.name == time.strftime(date_format, stamp)


# --- Ctrl-C ----------------------------------------------------------------


class FakeBoard:
    """Stands in for the open SSH connection to the board."""

    def __init__(self, lines, hang):
        self.lines = lines
        self.hang = hang
        self.commands = []

    async def _stdout(self):
        for line in self.lines:
            yield line
        if self.hang:
            await asyncio.Event().wait()  # `tail -f` never ends by itself

    async def create_process(self, command, term_type=None):
        return SimpleNamespace(stdout=self._stdout())

    async def run(self, command, check=False):
        self.commands.append(command)
        return SimpleNamespace(stdout="STOPPED\n")


@pytest.fixture
def captured_stop(monkeypatch):
    """Replace the Ctrl-C handler and give the test the stop event it would set."""
    events = []

    def install(stop):
        events.append(stop)
        return lambda: None

    monkeypatch.setattr(run_board, "install_interrupt_handler", install)
    return events


@pytest.mark.asyncio
async def test_ctrl_c_stops_the_board_client_not_just_the_tail(captured_stop):
    board = FakeBoard(["STARTED"], hang=True)
    attached = asyncio.create_task(run_board.attach(board, "tail"))

    await wait_until(lambda: captured_stop)
    captured_stop[0].set()  # what the handler does on Ctrl-C
    await asyncio.wait_for(attached, DEADLINE_S)

    assert board.commands == [run_board.stop_command()]


@pytest.mark.asyncio
async def test_a_tail_that_ends_by_itself_leaves_the_client_alone(captured_stop):
    """If the log stream ends by itself, the client is not stopped."""
    board = FakeBoard(["ERROR: nothing to tail and no client running"], hang=False)

    await asyncio.wait_for(run_board.attach(board, "tail"), DEADLINE_S)

    assert board.commands == []


@pytest.mark.asyncio
async def test_the_interrupt_handler_sets_stop_then_hands_back_a_second_ctrl_c():
    """A second Ctrl-C falls back to the default, so a stuck stop can still be escaped."""
    stop = asyncio.Event()
    before = signal.getsignal(signal.SIGINT)
    restore = run_board.install_interrupt_handler(stop)
    try:
        handler = signal.getsignal(signal.SIGINT)
        assert handler is not before
        handler(signal.SIGINT, None)  # called directly: a real SIGINT would hit pytest
        await asyncio.wait_for(stop.wait(), DEADLINE_S)
        assert signal.getsignal(signal.SIGINT) is before
    finally:
        restore()
    assert signal.getsignal(signal.SIGINT) is before


@pytest.mark.asyncio
@pytest.mark.parametrize("module_name", ["pipeline", "run_board"])
async def test_both_panes_announce_shutdown_with_the_same_line(module_name, caplog):
    """Both windows print the same line when Ctrl-C starts a shutdown."""
    module = __import__(module_name)
    restore = module.install_interrupt_handler(asyncio.Event())
    try:
        with caplog.at_level("INFO"):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
    finally:
        restore()

    announced = [r.getMessage() for r in caplog.records if "interrupt" in r.getMessage()]
    assert announced == ["interrupt received; shutting down (Ctrl-C again to force)"]


@pytest.mark.asyncio
async def test_sync_also_copies_the_ai_teams_module_that_gesture_imports():
    """gesture.py needs AI's two modules and their bitstreams beside it to load either model."""
    put = []

    class Sftp:
        async def makedirs(self, path, exist_ok=False):
            pass

        async def put(self, paths, remote):
            put.extend(Path(p).name for p in paths)

    class Opened:
        async def __aenter__(self):
            return Sftp()

        async def __aexit__(self, *exc):
            return False

    conn = SimpleNamespace(start_sftp_client=lambda: Opened())
    await asyncio.wait_for(run_board.sync_sources(conn), timeout=DEADLINE_S)

    assert {"gesture.py", "tcp_client.py", "wk10_integration.py", "baseline_improved.xsa"} <= set(put)


def test_sync_copies_the_model_and_bitstream_gesture_loads():
    import gesture

    assert run_board.AI_MODULE.stem == gesture.AI_MODULE
    assert run_board.AI_BITSTREAM.name == gesture.AI_BITSTREAM
