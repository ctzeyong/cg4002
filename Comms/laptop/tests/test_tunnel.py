"""Tunnel supervisor tests: which failures retry, which are fatal, and the ladder.

`one_session` itself is not exercised here — it is two SSH handshakes against NUS
infrastructure, and faking that would only test the fake. It was measured for
real instead, by `ssh_auth_spike/probe_asyncssh_auth.py`, before the module was
written. What these cover is everything the supervisor decides *around* a session,
which is where the behaviour worth regressing lives.
"""

import asyncio
import logging

import asyncssh
import pytest

import tunnel
from helpers import DEADLINE_S

CONFIG = tunnel.TunnelConfig(
    jump_user="someone",
    jump_host="jump.example",
    board_user="xilinx",
    board_host="board.example",
    port=8888,
)


def sessions_that(*outcomes):
    """A fake `one_session` that replays `outcomes`, counting its calls.

    Each outcome is either an exception to raise or a callable taking `stop`.
    The final outcome repeats, so a test only lists what it cares about.
    """
    calls = []

    async def fake_session(config, password, stop):
        outcome = outcomes[min(len(calls), len(outcomes) - 1)]
        calls.append(outcome)
        if isinstance(outcome, BaseException):
            raise outcome
        outcome(stop)

    return fake_session, calls


@pytest.mark.asyncio
async def test_rejected_credentials_are_fatal(monkeypatch):
    """A bad password must not be replayed.

    Two reasons, either sufficient: it cannot succeed on a retry, and every retry
    is a failed authentication against shared SoC infrastructure, which is how an
    NUS account gets locked mid-demo.
    """
    fake_session, calls = sessions_that(asyncssh.PermissionDenied("bad password"))
    monkeypatch.setattr(tunnel, "one_session", fake_session)
    monkeypatch.setattr(tunnel, "MIN_BACKOFF_S", 0.01)

    stop = asyncio.Event()
    await asyncio.wait_for(tunnel.run_tunnel(CONFIG, "pw", stop), timeout=DEADLINE_S)

    assert len(calls) == 1
    assert not stop.is_set()  # the other legs keep running; only this one gives up


@pytest.mark.asyncio
async def test_taken_remote_port_retries_and_says_so(monkeypatch, caplog):
    """ChannelListenError must be recognised, not swallowed as "unexpected".

    It descends from Exception, *not* from asyncssh.Error, so the broad
    `except (OSError, asyncssh.Error)` clause misses it entirely. Without its own
    clause it still retries — via the catch-all — but reports a stack trace
    instead of naming the shared-board port conflict, which is the one cause an
    operator can actually act on mid-demo.
    """
    def third_call_stops(stop):
        stop.set()

    fake_session, calls = sessions_that(
        asyncssh.ChannelListenError("port in use"),
        asyncssh.ChannelListenError("port in use"),
        third_call_stops,
    )
    monkeypatch.setattr(tunnel, "one_session", fake_session)
    monkeypatch.setattr(tunnel, "MIN_BACKOFF_S", 0.01)
    caplog.set_level(logging.DEBUG, logger="tunnel")

    stop = asyncio.Event()
    await asyncio.wait_for(tunnel.run_tunnel(CONFIG, "pw", stop), timeout=DEADLINE_S)

    assert len(calls) == 3  # retried rather than given up on
    assert "remote port 8888 unavailable" in caplog.text
    assert "ss -ltnp" in caplog.text  # tells the operator where to look
    assert "unexpected session error" not in caplog.text


@pytest.mark.asyncio
async def test_transient_failure_retries(monkeypatch):
    """A down VPN is an OSError and must not end the supervisor."""
    def second_call_stops(stop):
        stop.set()

    fake_session, calls = sessions_that(OSError("network unreachable"), second_call_stops)
    monkeypatch.setattr(tunnel, "one_session", fake_session)
    monkeypatch.setattr(tunnel, "MIN_BACKOFF_S", 0.01)

    stop = asyncio.Event()
    await asyncio.wait_for(tunnel.run_tunnel(CONFIG, "pw", stop), timeout=DEADLINE_S)

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_stop_during_a_session_does_not_reconnect(monkeypatch, caplog):
    """Shutdown must not dial again, nor log that it is about to.

    Dropping the stop check does not cause a reconnect — the `while` catches it
    one statement later — but the supervisor first logs "session ended" and
    "reconnecting in 5s" on its way out. This log is read during the demo and in
    post-mortems, and a clean shutdown that announces a reconnect it will never
    make reads as a fault.
    """
    fake_session, calls = sessions_that(lambda stop: stop.set())
    monkeypatch.setattr(tunnel, "one_session", fake_session)
    monkeypatch.setattr(tunnel, "MIN_BACKOFF_S", 0.01)
    caplog.set_level(logging.DEBUG, logger="tunnel")

    stop = asyncio.Event()
    await asyncio.wait_for(tunnel.run_tunnel(CONFIG, "pw", stop), timeout=DEADLINE_S)

    assert len(calls) == 1
    assert "reconnecting" not in caplog.text


class ScriptedClock:
    """Stands in for the `time` module with sessions of scripted duration.

    run_tunnel reads the clock twice per attempt — once before the session and
    once after — so each pair of calls yields one scripted duration. Patched into
    tunnel's own namespace rather than onto the real `time` module, which pytest
    itself depends on.
    """

    def __init__(self, durations):
        self._durations = iter(durations)
        self._now = 0.0
        self._starting = True

    def monotonic(self):
        if self._starting:
            self._starting = False
        else:
            self._now += next(self._durations)
            self._starting = True
        return self._now


@pytest.mark.asyncio
async def test_backoff_doubles_on_fast_failure_and_resets_after_a_healthy_run(monkeypatch):
    """The ladder is 5, 10, 20, 40, 60 — and a healthy run puts it back to 5.

    Without the reset, a tunnel that held for hours before dropping would inherit
    the backoff from whatever failed at bring-up and wait a full minute to
    re-dial, in the middle of a demo.

    Neither the clock nor the sleep is real: the assertion is about which delay
    the supervisor chooses, and sleeping them would take two minutes.
    """
    waits = []

    async def record_wait(stop, seconds):
        waits.append(seconds)

    monkeypatch.setattr(tunnel, "_sleep_or_stop", record_wait)
    # Four fast failures, then one session long enough to count as healthy. The
    # sixth sets stop before the clock is read a second time, so it needs no
    # duration of its own.
    monkeypatch.setattr(
        tunnel, "time", ScriptedClock([1.0, 1.0, 1.0, 1.0, tunnel.GOOD_RUN_S])
    )

    fake_session, _ = sessions_that(
        OSError("down"),
        OSError("down"),
        OSError("down"),
        OSError("down"),
        lambda stop: None,  # the healthy session
        lambda stop: stop.set(),
    )
    monkeypatch.setattr(tunnel, "one_session", fake_session)

    await asyncio.wait_for(
        tunnel.run_tunnel(CONFIG, "pw", asyncio.Event()), timeout=DEADLINE_S
    )

    assert waits == [5.0, 10.0, 20.0, 40.0, 5.0]


# --- configuration ---------------------------------------------------------


def write_env(tmp_path, text):
    env = tmp_path / ".env"
    env.write_text(text, encoding="utf-8")
    return env


def test_load_config_precedence_is_override_then_env_then_default(tmp_path):
    env = write_env(
        tmp_path,
        "# a comment\n"
        "JUMP_USER=from_env\n"
        'BOARD_HOST="quoted.example"\n'
        "TUNNEL_PORT=9999\n",
    )

    config = tunnel.load_config(env, jump_user="from_argument")

    assert config.jump_user == "from_argument"  # override beats .env
    assert config.board_host == "quoted.example"  # .env, one layer of quotes stripped
    assert config.port == 9999
    assert config.jump_host == tunnel.DEFAULT_JUMP_HOST  # absent -> built-in default
    assert config.board_user == tunnel.DEFAULT_BOARD_USER


def test_load_config_names_what_is_missing(tmp_path):
    env = write_env(tmp_path, "JUMP_USER=someone\n")

    with pytest.raises(SystemExit, match="BOARD_HOST"):
        tunnel.load_config(env)


def test_describe_keeps_usernames_out_of_the_log():
    """The tunnel log is shown during the demo; the NUSNET id is not for camera."""
    described = CONFIG.describe()

    assert CONFIG.board_host in described
    assert CONFIG.jump_host in described
    assert str(CONFIG.port) in described
    assert CONFIG.jump_user not in described


def test_password_prompt_goes_through_logging(monkeypatch, caplog):
    """The prompt must be a log record, not a direct write.

    setup_logging writes the console from a QueueListener thread, so a getpass
    prompt string races the records queued before it and lands mid-line under
    them -- a blocked read with nothing on screen explaining it (2026-09-17).
    Queueing the prompt restores FIFO order.
    """
    seen = {}
    monkeypatch.setattr(tunnel.getpass, "getpass", lambda prompt: seen.setdefault("prompt", prompt) or "pw")

    with caplog.at_level(logging.INFO, logger="tunnel"):
        # caplog installs a handler on the root logger, which is what the
        # production code checks for to know an entry point configured logging.
        password = tunnel.prompt_for_password("jump.example")

    assert password == "pw"
    assert seen["prompt"] == ""  # nothing written directly, so nothing can race
    assert any("jump.example" in record.getMessage() for record in caplog.records)


def test_password_prompt_falls_back_when_logging_is_unconfigured(monkeypatch):
    """No handlers means a queued prompt would be swallowed at INFO.

    Then the user faces a blocked read with no prompt at all -- worse than the
    bug this replaced, so the direct prompt has to survive for that case.
    """
    seen = {}
    monkeypatch.setattr(tunnel.getpass, "getpass", lambda prompt: seen.setdefault("prompt", prompt) or "pw")
    monkeypatch.setattr(logging.getLogger(), "handlers", [])

    tunnel.prompt_for_password("jump.example")

    assert "jump.example" in seen["prompt"]


def test_load_password_prefers_the_process_environment(monkeypatch, tmp_path, caplog):
    """Process env beats .env, matching load_config's precedence."""
    env_file = tmp_path / ".env"
    env_file.write_text("JUMP_PASSWORD=from-environ-loses\n", encoding="utf-8")
    monkeypatch.setenv(tunnel.PASSWORD_KEY, "from-environ")
    monkeypatch.setattr(tunnel.getpass, "getpass", lambda prompt: pytest.fail("prompted"))

    with caplog.at_level(logging.INFO, logger="tunnel"):
        assert tunnel.load_password("jump.example", env_file) == "from-environ"

    # The value must never reach a record; only the key name may.
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "from-environ" not in logged
    assert tunnel.PASSWORD_KEY in logged


def test_load_password_reads_dotenv_when_the_environment_is_unset(monkeypatch, tmp_path):
    """The .env key is what makes a logon task possible; notes/ultra96_setup.md."""
    env_file = tmp_path / ".env"
    env_file.write_text("JUMP_PASSWORD=from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv(tunnel.PASSWORD_KEY, raising=False)
    monkeypatch.setattr(tunnel.getpass, "getpass", lambda prompt: pytest.fail("prompted"))

    assert tunnel.load_password("jump.example", env_file) == "from-dotenv"


def test_load_password_prompts_when_nothing_is_stored(monkeypatch, tmp_path):
    """An empty key must behave exactly as before the key existed."""
    env_file = tmp_path / ".env"
    env_file.write_text("JUMP_PASSWORD=\n", encoding="utf-8")
    monkeypatch.delenv(tunnel.PASSWORD_KEY, raising=False)
    monkeypatch.setattr(tunnel.getpass, "getpass", lambda prompt: "typed")

    assert tunnel.load_password("jump.example", env_file) == "typed"


def test_the_password_cannot_reach_tunnel_config(tmp_path):
    """TunnelConfig holds no credential, so no repr or traceback can print one.

    A stored password makes this load-bearing rather than tidy: a field added here
    would be printed by every describe() line and every asyncssh traceback.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JUMP_USER=someone\nBOARD_HOST=board.example\nJUMP_PASSWORD=secret-value\n",
        encoding="utf-8",
    )
    config = tunnel.load_config(env_file)

    assert not [name for name in vars(config) if "pass" in name.lower()]
    assert "secret-value" not in repr(config)
    assert "secret-value" not in config.describe()
