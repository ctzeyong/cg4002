"""SSH reverse tunnel to the Ultra96, supervised on the relay's own event loop.

    laptop --ssh--> jump host --ssh--> Ultra96
    Ultra96 localhost:8888  --forwarded back to-->  laptop 127.0.0.1:8888 (tcp_server)

asyncssh, not ssh.exe: the jump host needs a password, and a tunnel that reconnects
by itself must be able to give it without a terminal. Reconnects with backoff like
ble_client.run_node.

    python tunnel.py                            # the tunnel on its own
    python pipeline_board_broker.py --tunnel    # inside the relay (one loop, one log)
"""

import argparse
import asyncio
import getpass
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import asyncssh

from logging_setup import setup_logging
from mqtt_config import port_number

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "tunnel.log"

# Read by load_password, never stored on TunnelConfig.
PASSWORD_KEY = "JUMP_PASSWORD"

DEFAULT_JUMP_HOST = "stujump.comp.nus.edu.sg"
DEFAULT_BOARD_USER = "xilinx"
DEFAULT_PORT = 8888

# SSH keepalive: the link counts as dead after about 15 s x 3 = 45 s of silence.
KEEPALIVE_INTERVAL_S = 15
KEEPALIVE_COUNT_MAX = 3

# Reconnect wait 5, 10, 20, 40, 60 s. Slower than BLE's because every attempt logs
# in to shared NUS servers.
MIN_BACKOFF_S = 5.0
MAX_BACKOFF_S = 60.0
GOOD_RUN_S = 60.0  # a session lasting this long resets the backoff

log = logging.getLogger("tunnel")


@dataclass(frozen=True)
class TunnelConfig:
    """Where the tunnel goes. No password here on purpose, so printing it can never leak one."""

    jump_user: str
    jump_host: str
    board_user: str
    board_host: str
    port: int

    def describe(self):
        """Hosts and port without usernames, so the NUSNET id never appears on screen."""
        return f"{self.board_host} via {self.jump_host}, port {self.port}"


def read_dotenv(path):
    """Read simple KEY=VALUE lines from a .env file; a missing file gives {}."""
    values = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_config(env_file=ENV_FILE, **overrides):
    """Build a TunnelConfig: argument > .env > built-in default.

    .env is gitignored, so usernames and the board name never reach a commit.

    Raises:
        SystemExit: naming the settings that have no value anywhere.
    """
    env = read_dotenv(env_file)

    def pick(name, key, default=""):
        return overrides.get(name) or env.get(key) or default

    config = TunnelConfig(
        jump_user=pick("jump_user", "JUMP_USER"),
        jump_host=pick("jump_host", "JUMP_HOST", DEFAULT_JUMP_HOST),
        board_user=pick("board_user", "BOARD_USER", DEFAULT_BOARD_USER),
        board_host=pick("board_host", "BOARD_HOST"),
        port=port_number(pick("port", "TUNNEL_PORT", DEFAULT_PORT)),
    )

    missing = [
        key
        for key, value in (("JUMP_USER", config.jump_user), ("BOARD_HOST", config.board_host))
        if not value
    ]
    if missing:
        raise SystemExit(
            f"missing {', '.join(missing)}: copy .env.example to {env_file} and fill it in"
        )
    return config


def load_password(jump_host, env_file=ENV_FILE):
    """Jump-host password from JUMP_PASSWORD (environment, then .env), else ask once.

    Storing it lets the relay start unattended.
    """
    stored = os.environ.get(PASSWORD_KEY) or read_dotenv(env_file).get(PASSWORD_KEY)
    if stored:
        # Never the value, and never at DEBUG either.
        log.info("jump-host password taken from %s, not prompted", PASSWORD_KEY)
        return stored
    return prompt_for_password(jump_host)


def prompt_for_password(jump_host):
    """Ask for the jump-host password once; it is reused on every reconnect.

    The prompt text is logged, not passed to getpass, so it shows in order after
    the other log lines instead of getting lost among them.

    Raises:
        SystemExit: empty password, which the jump host always rejects.
    """
    # If logging is not set up, a logged prompt would never show; use getpass's own.
    if logging.getLogger().handlers:
        log.info("jump-host password for %s — type it and press Enter:", jump_host)
        prompt = ""
    else:
        prompt = f"NUS password for {jump_host}: "

    password = getpass.getpass(prompt)
    if not password:
        raise SystemExit("no password entered; the jump host always demands one")
    return password


async def _sleep_or_stop(stop, seconds):
    """Wait `seconds`, but return early if `stop` is set, so Ctrl-C never waits out a backoff."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _hold_until_closed(conn, stop):
    """Wait until the SSH connection drops or `stop` is set, whichever comes first."""
    waiters = [
        asyncio.create_task(conn.wait_closed()),
        asyncio.create_task(stop.wait()),
    ]
    try:
        done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for waiter in pending:
            waiter.cancel()
        for waiter in done:
            waiter.result()  # surface anything a waiter raised
    finally:
        await asyncio.gather(*waiters, return_exceptions=True)


async def one_session(config, password, stop):
    """Open both SSH hops and the reverse forward, and hold them until the link drops or `stop`.

    Both hops send keepalives, so a silent break on either one is noticed.
    """
    session_options = {
        "keepalive_interval": KEEPALIVE_INTERVAL_S,
        "keepalive_count_max": KEEPALIVE_COUNT_MAX,
    }

    async with asyncssh.connect(
        config.jump_host,
        username=config.jump_user,
        password=password,
        **session_options,
    ) as jump:
        log.info("authenticated to the jump host")

        # The board hop uses the laptop's SSH key (asyncssh finds it); no password.
        async with asyncssh.connect(
            config.board_host,
            username=config.board_user,
            tunnel=jump,
            **session_options,
        ) as board:
            # Board localhost:8888 -> laptop 127.0.0.1:8888. Use 127.0.0.1, not "localhost":
            # Windows tries IPv6 first and tcp_server listens on IPv4 only.
            await board.forward_remote_port(
                "localhost", config.port, "127.0.0.1", config.port
            )
            log.info(
                "tunnel open: board localhost:%d -> laptop 127.0.0.1:%d",
                config.port,
                config.port,
            )
            await _hold_until_closed(board, stop)


async def run_tunnel(config, password, stop, open_session=None):
    """Keep the tunnel up: connect, hold, wait, retry.

    Returns when `stop` is set, or gives up for good if the password is rejected
    (retrying a bad password could lock the account). `open_session` replaces
    `one_session`; tunnel_tailscale.py uses it to skip the jump host.
    """
    # Looked up at call time, not as a default argument, so tests can still patch one_session.
    open_session = open_session or one_session
    backoff = MIN_BACKOFF_S
    attempts = 0

    # In a finally so the summary still prints when this task is cancelled.
    try:
        while not stop.is_set():
            attempts += 1
            log.info("attempt %d: opening tunnel", attempts)
            started = time.monotonic()  # monotonic: a duration, not a wall time

            try:
                await open_session(config, password, stop)
            except asyncssh.PermissionDenied as exc:
                log.error("authentication rejected: %s", exc.reason or exc)
                log.error("fix the password or the skeys key registration, then restart")
                return
            except asyncssh.ChannelListenError as exc:
                log.warning("remote port %d unavailable: %s", config.port, exc)
                log.warning("the board is shared; check there: ss -ltnp | grep %d", config.port)
            except (OSError, asyncssh.Error) as exc:
                log.warning("session failed: %s", exc)
            except Exception:
                log.exception("unexpected session error")

            if stop.is_set():
                break

            elapsed = time.monotonic() - started
            if elapsed >= GOOD_RUN_S:
                # A long healthy session resets the wait to the minimum.
                log.info("session lasted %.0fs -> healthy run, backoff reset", elapsed)
                backoff = MIN_BACKOFF_S
            else:
                log.warning("session ended after %.0fs (VPN down? port taken?)", elapsed)

            log.info("reconnecting in %.0fs", backoff)
            await _sleep_or_stop(stop, backoff)

            if elapsed < GOOD_RUN_S:
                backoff = min(backoff * 2, MAX_BACKOFF_S)
    finally:
        log.info("tunnel supervisor stopped after %d attempt(s)", attempts)


async def run_standalone(config, password):
    """Run the tunnel on its own, with nothing else on the loop."""
    await run_tunnel(config, password, asyncio.Event())


def main():
    """Entry point for the tunnel alone."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=ENV_FILE)
    parser.add_argument("--jump-user", help="override JUMP_USER")
    parser.add_argument("--jump-host", help="override JUMP_HOST")
    parser.add_argument("--board-user", help="override BOARD_USER")
    parser.add_argument("--board-host", help="override BOARD_HOST")
    parser.add_argument("--port", type=port_number, help="override TUNNEL_PORT")
    parser.add_argument("-v", "--verbose", action="store_true", help="log the SSH exchange")
    args = parser.parse_args()

    log_file = setup_logging(LOG_FILE, args.verbose)
    config = load_config(
        args.env_file,
        jump_user=args.jump_user,
        jump_host=args.jump_host,
        board_user=args.board_user,
        board_host=args.board_host,
        port=args.port,
    )
    log.info("tunnel supervisor start; %s; logging to %s", config.describe(), log_file)
    if args.verbose:
        # asyncssh's own logs name the jump username, so -v here is not safe to record.
        log.warning("verbose logging includes the jump username; do not record this")
        logging.getLogger("asyncssh").setLevel(logging.DEBUG)
        asyncssh.set_debug_level(2)

    password = load_password(config.jump_host, args.env_file)
    try:
        asyncio.run(run_standalone(config, password))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
