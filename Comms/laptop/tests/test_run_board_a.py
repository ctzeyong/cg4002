"""Tests for scripts/run_board_a.py: how the laptop-broker board command differs from run_board.py.

The pair is duplicated on purpose (separate files per setup), so these pin the
points where the two must not converge or collide on the shared board.
"""

import fnmatch
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_board  # noqa: E402
import run_board_a  # noqa: E402

A_CMDLINE = "/home/u/comms-venv/bin/python /home/u/comms/tcp_client.py --host 127.0.0.1 --port 8888"
B_CMDLINE = "/home/u/comms-venv/bin/python /home/u/comms/tcp_client_mqtt.py --host 127.0.0.1"


def test_a_launches_tcp_client_and_never_bs_client():
    command = run_board_a.remote_command(8888, [], restart=False)
    launched = re.search(r"nohup .*?/bin/python (\S+)", command).group(1)
    assert launched == "~/comms/tcp_client.py"


def test_bs_client_is_stopped_before_a_starts():
    """tcp_server keeps the newest client: a live B client and A would evict each other."""
    for restart in (False, True):
        command = run_board_a.remote_command(8888, [], restart=restart)
        kill_b = command.index(f'pkill -f "{run_board.PROCESS_PATTERN}"')
        assert kill_b < command.index("nohup")


def test_the_two_process_patterns_never_match_each_others_client():
    """A --stop for one setup must never kill the other's client."""
    assert re.search(run_board_a.PROCESS_PATTERN, A_CMDLINE)
    assert not re.search(run_board_a.PROCESS_PATTERN, B_CMDLINE)
    assert re.search(run_board.PROCESS_PATTERN, B_CMDLINE)
    assert not re.search(run_board.PROCESS_PATTERN, A_CMDLINE)


def test_a_logs_are_never_picked_up_by_bs_attach():
    """run_board.py's attach tails the newest console_*.log, and its fallback the newest
    tcp_client_mqtt_*.log. This setup's capture must match neither, and its log pattern
    must not match the other's log."""
    a_capture = run_board_a.CAPTURE_PREFIX + "20261002T120000Z.log"
    assert not fnmatch.fnmatch(a_capture, "console_*.log")
    assert not fnmatch.fnmatch("tcp_client_mqtt_120000Z.log", "tcp_client_[0-9]*.log")
    assert fnmatch.fnmatch("tcp_client_120000Z.log", "tcp_client_[0-9]*.log")
    assert "tcp_client_[0-9]*.log" in run_board_a.remote_command(8888, [], restart=False)


def test_a_has_no_broker_check():
    """The broker is on the laptop here; a board-broker warning would send you to the wrong machine."""
    assert "1883" not in run_board_a.remote_command(8888, [], restart=False)


def test_flags_are_consumed_and_the_rest_is_forwarded():
    args = run_board_a.parse_args(["--sync", "-v", "--event-every", "2"])
    assert args.sync is True
    assert args.client_args == ["-v", "--event-every", "2"]
    assert run_board_a.parse_args(["--fetch-logs", "2026-10-01"]).fetch_logs == "2026-10-01"
    assert run_board_a.parse_args(["--stop"]).client_args == []


def test_bs_copy_of_as_pattern_matches_the_original():
    """run_board.py repeats this pattern because it cannot import run_board_a.py."""
    assert run_board.A_PROCESS_PATTERN == run_board_a.PROCESS_PATTERN


def test_as_client_is_stopped_before_b_starts():
    """The mirror of the test above it: run_board.py's command stops this client before launching."""
    for restart in (False, True):
        command = run_board.remote_command(8888, [], restart=restart)
        kill_a = command.index(f'pkill -f "{run_board.A_PROCESS_PATTERN}"')
        assert kill_a < command.index("nohup")


@pytest.mark.parametrize("module", [run_board, run_board_a])
def test_fetch_logs_defaults_to_the_utc_day_not_the_local_one(module, monkeypatch):
    """The board names log folders by UTC date. At 07:30 in Singapore, UTC is still yesterday."""
    utc = time.strptime("2026-10-01 23:30", "%Y-%m-%d %H:%M")
    local = time.strptime("2026-10-02 07:30", "%Y-%m-%d %H:%M")
    clock = SimpleNamespace(strftime=time.strftime, gmtime=lambda: utc, localtime=lambda: local)
    monkeypatch.setattr(module, "time", clock)

    assert module.parse_args(["--fetch-logs"]).fetch_logs == "2026-10-01"
