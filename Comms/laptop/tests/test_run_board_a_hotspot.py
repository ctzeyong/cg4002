"""Tests for scripts/run_board_a_hotspot.py: A's board command, over one direct hop, no password."""

import asyncio
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_board_a  # noqa: E402
import run_board_a_hotspot as hotspot  # noqa: E402
import tunnel  # noqa: E402
import tunnel_tailscale  # noqa: E402

CONFIG = tunnel_tailscale.DirectConfig(board_user="xilinx", board_host="100.1.2.3", port=8888)


class FakeBoard:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def recorded(monkeypatch):
    """Patch the connection and every board action; return the ordered list of what ran."""
    events = []
    board = FakeBoard()

    def connect(host, **kwargs):
        events.append(("connect", host, kwargs))
        return board

    async def action(name, *args):
        events.append((name,) + args)

    monkeypatch.setattr(hotspot.asyncssh, "connect", connect)
    monkeypatch.setattr(hotspot, "sync_sources", lambda conn: action("sync"))
    monkeypatch.setattr(hotspot, "fetch_logs", lambda conn, day: action("fetch", day))
    monkeypatch.setattr(run_board_a, "stop_client", lambda conn: action("stop"))
    monkeypatch.setattr(run_board_a, "attach", lambda conn, command: action("attach", command))
    return events


def args_for(argv):
    args = run_board_a.parse_args(argv)
    args.port = 8888
    return args


@pytest.mark.asyncio
async def test_one_direct_hop_with_no_password(recorded):
    await hotspot.run(CONFIG, args_for([]))

    connects = [e for e in recorded if e[0] == "connect"]
    assert len(connects) == 1
    _, host, kwargs = connects[0]
    assert host == "100.1.2.3"
    assert kwargs["username"] == "xilinx"
    assert "tunnel" not in kwargs and "password" not in kwargs


@pytest.mark.asyncio
async def test_sync_then_as_own_board_command(recorded):
    """The command is run_board_a's, so it still stops B's client before starting A's."""
    await hotspot.run(CONFIG, args_for(["--sync", "--sensor-trigger", "100"]))

    expected = run_board_a.remote_command(8888, ["--sensor-trigger", "100"], restart=True)
    assert [e[0] for e in recorded] == ["connect", "sync", "attach"]
    assert recorded[-1] == ("attach", expected)


@pytest.mark.asyncio
async def test_stop_and_fetch_logs_do_only_that(recorded):
    await hotspot.run(CONFIG, args_for(["--stop"]))
    await hotspot.run(CONFIG, args_for(["--fetch-logs", "2026-10-02"]))

    assert [e[0] for e in recorded] == ["connect", "stop", "connect", "fetch"]
    assert recorded[-1] == ("fetch", "2026-10-02")


def test_main_never_asks_for_a_password(monkeypatch, tmp_path):
    def no_password(*args, **kwargs):
        pytest.fail("asked for a password")

    monkeypatch.setattr(tunnel, "load_password", no_password)
    monkeypatch.setattr(tunnel, "prompt_for_password", no_password)
    monkeypatch.setattr(tunnel, "load_config", lambda *a, **k: pytest.fail("campus config read"))
    monkeypatch.setattr(hotspot, "setup_logging", lambda path: tmp_path / "x.log")
    monkeypatch.setattr(hotspot.tunnel_tailscale, "load_config", lambda: CONFIG)
    monkeypatch.setattr(hotspot.tunnel_tailscale, "path_report", lambda host: "test")
    ran = []

    async def fake_run(config, args):
        ran.append((config, args.port))

    monkeypatch.setattr(hotspot, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["run_board_a_hotspot.py", "--fetch-logs"])

    hotspot.main()

    assert ran == [(CONFIG, 8888)]
