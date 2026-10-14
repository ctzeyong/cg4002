"""Tests for tunnel_tailscale.py: one direct hop, the right host, and tunnel.py's supervisor reused."""

import asyncio
import logging

import asyncssh
import pytest

import tunnel
import tunnel_tailscale
from helpers import DEADLINE_S

CONFIG = tunnel_tailscale.DirectConfig(board_user="xilinx", board_host="100.1.2.3", port=8888)


def write_env(tmp_path, text):
    env = tmp_path / ".env"
    env.write_text(text, encoding="utf-8")
    return env


class FakeBoard:
    """Stands in for an asyncssh connection; records the forward and closes when told."""

    def __init__(self):
        self.forwards = []
        self.closed = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def forward_remote_port(self, *args):
        self.forwards.append(args)

    async def wait_closed(self):
        await self.closed.wait()


def fake_connect(monkeypatch, outcome):
    """Patch asyncssh.connect; each call records (host, kwargs) and returns or raises `outcome`."""
    calls = []

    def connect(host, **kwargs):
        calls.append((host, kwargs))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(tunnel_tailscale.asyncssh, "connect", connect)
    return calls


# --- configuration ---------------------------------------------------------


def test_load_config_uses_the_tailscale_host_and_never_the_campus_one(tmp_path):
    env = write_env(
        tmp_path, "BOARD_HOST=campus.example\nTAILSCALE_BOARD_HOST=100.1.2.3\nTUNNEL_PORT=9999\n"
    )

    config = tunnel_tailscale.load_config(env)

    assert config.board_host == "100.1.2.3"
    assert config.port == 9999
    assert config.board_user == tunnel.DEFAULT_BOARD_USER


def test_load_config_names_the_missing_key_even_when_the_campus_host_is_set(tmp_path):
    env = write_env(tmp_path, "BOARD_HOST=campus.example\n")

    with pytest.raises(SystemExit, match="TAILSCALE_BOARD_HOST"):
        tunnel_tailscale.load_config(env)


def test_an_argument_beats_the_env_file(tmp_path):
    env = write_env(tmp_path, "TAILSCALE_BOARD_HOST=100.1.2.3\n")

    assert tunnel_tailscale.load_config(env, board_host="100.9.9.9").board_host == "100.9.9.9"


def test_describe_leaves_the_username_out():
    assert "xilinx" not in CONFIG.describe()
    assert "100.1.2.3" in CONFIG.describe()


# --- the session -----------------------------------------------------------


@pytest.mark.asyncio
async def test_one_session_is_one_direct_hop_with_no_password(monkeypatch):
    board = FakeBoard()
    calls = fake_connect(monkeypatch, board)
    stop = asyncio.Event()
    stop.set()  # hold returns at once once the forward is open

    await asyncio.wait_for(tunnel_tailscale.one_session(CONFIG, None, stop), timeout=DEADLINE_S)

    assert len(calls) == 1
    host, kwargs = calls[0]
    assert host == "100.1.2.3"
    assert kwargs["username"] == "xilinx"
    assert "tunnel" not in kwargs  # no jump host
    assert "password" not in kwargs  # the laptop's key, as on the campus board hop
    assert kwargs["keepalive_interval"] == tunnel.KEEPALIVE_INTERVAL_S
    assert board.forwards == [("localhost", 8888, "127.0.0.1", 8888)]


@pytest.mark.asyncio
async def test_run_tunnel_uses_the_session_it_is_given(monkeypatch):
    """The campus one_session must not run: it would dial the jump host."""
    monkeypatch.setattr(tunnel, "one_session", lambda *a: pytest.fail("campus session used"))
    stop = asyncio.Event()
    seen = []

    async def session(config, password, stop_event):
        seen.append(config)
        stop_event.set()

    await asyncio.wait_for(
        tunnel.run_tunnel(CONFIG, None, stop, open_session=session), timeout=DEADLINE_S
    )

    assert seen == [CONFIG]


@pytest.mark.asyncio
async def test_a_refused_key_is_fatal_and_says_which_key(monkeypatch, caplog):
    calls = fake_connect(monkeypatch, asyncssh.PermissionDenied("no"))
    monkeypatch.setattr(tunnel, "MIN_BACKOFF_S", 0.01)

    with caplog.at_level(logging.ERROR, logger="tunnel"):
        await asyncio.wait_for(
            tunnel.run_tunnel(
                CONFIG, None, asyncio.Event(), open_session=tunnel_tailscale.one_session
            ),
            timeout=DEADLINE_S,
        )

    assert len(calls) == 1
    assert "refused this laptop's SSH key" in caplog.text


@pytest.mark.asyncio
async def test_a_dropped_link_reconnects(monkeypatch):
    calls = fake_connect(monkeypatch, OSError("network is unreachable"))
    monkeypatch.setattr(tunnel, "MIN_BACKOFF_S", 0.01)
    stop = asyncio.Event()

    async def stop_after_retries():
        while len(calls) < 3:
            await asyncio.sleep(0.005)
        stop.set()

    await asyncio.wait_for(
        asyncio.gather(
            tunnel.run_tunnel(CONFIG, None, stop, open_session=tunnel_tailscale.one_session),
            stop_after_retries(),
        ),
        timeout=DEADLINE_S,
    )

    assert len(calls) >= 3


# --- path report -----------------------------------------------------------


def status(state="Running", **peer):
    base = {"HostName": "pynq", "DNSName": "pynq.tail1234.ts.net.",
            "TailscaleIPs": ["100.1.2.3"], "Online": True, "Relay": "sin", "CurAddr": ""}
    base.update(peer)
    return {"BackendState": state, "Peer": {"key1": base}}


@pytest.mark.parametrize(
    "data, host, expected",
    [
        (status(), "100.1.2.3", "pynq: relayed via DERP(sin)"),
        (status(CurAddr="1.2.3.4:41641"), "100.1.2.3", "pynq: direct via 1.2.3.4:41641"),
        (status(), "pynq", "pynq: relayed via DERP(sin)"),
        (status(), "pynq.tail1234.ts.net", "pynq: relayed via DERP(sin)"),
        (status(Online=False), "100.1.2.3", "pynq is offline in the tailnet"),
        (status(), "100.9.9.9", "100.9.9.9 is not in this tailnet"),
        (status(state="Stopped"), "100.1.2.3", "Tailscale is not running here (state Stopped)"),
    ],
)
def test_describe_path(data, host, expected):
    assert tunnel_tailscale.describe_path(data, host) == expected


def test_path_report_never_raises(monkeypatch):
    monkeypatch.setattr(tunnel_tailscale, "find_cli", lambda: None)
    assert tunnel_tailscale.path_report("100.1.2.3") == "tailscale command not found"

    monkeypatch.setattr(tunnel_tailscale, "find_cli", lambda: "tailscale")

    def broken(*args, **kwargs):
        raise OSError("gone")

    assert "could not read" in tunnel_tailscale.path_report("100.1.2.3", run=broken)

    class NotJson:
        stdout = "not json"

    assert "could not read" in tunnel_tailscale.path_report("100.1.2.3", run=lambda *a, **k: NotJson)
