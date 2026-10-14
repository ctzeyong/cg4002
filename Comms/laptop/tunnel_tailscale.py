"""The same reverse tunnel as tunnel.py, but straight to the board over Tailscale.

    laptop --ssh over the tailnet--> Ultra96
    Ultra96 localhost:8888  --forwarded back to-->  laptop 127.0.0.1:8888 (tcp_server)

No jump host and no password: the board accepts the laptop's SSH key. Used for A on a
phone-hotspot uplink, where the NUS VPN is off. Reconnects through tunnel.run_tunnel.
Decisions: notes/ultra96_setup.md -> "Fallback D"; notes/tcp_leg.md -> "The tunnel".

    python tunnel_tailscale.py     # run_pipeline_a_hotspot.ps1 starts this beside the relay
"""

import argparse
import asyncio
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import asyncssh

import tunnel
from logging_setup import setup_logging
from mqtt_config import port_number
from tcp_server import DEFAULT_PORT as RELAY_PORT

LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "tunnel_tailscale.log"

# The board's 100.x address. Its own key, so the campus BOARD_HOST is never used by mistake.
HOST_KEY = "TAILSCALE_BOARD_HOST"
INSTALLED_CLI = Path(r"C:\Program Files\Tailscale\tailscale.exe")

log = logging.getLogger("tunnel")


@dataclass(frozen=True)
class DirectConfig:
    """Where the tunnel goes, with no jump host."""

    board_user: str
    board_host: str
    port: int

    def describe(self):
        """Host and port, without the username."""
        return f"{self.board_host} over Tailscale, port {self.port}"


def load_config(env_file=tunnel.ENV_FILE, **overrides):
    """Build a DirectConfig: argument > .env > built-in default.

    Raises:
        SystemExit: if TAILSCALE_BOARD_HOST has no value anywhere.
    """
    env = tunnel.read_dotenv(env_file)

    def pick(name, key, default=""):
        return overrides.get(name) or env.get(key) or default

    config = DirectConfig(
        board_user=pick("board_user", "BOARD_USER", tunnel.DEFAULT_BOARD_USER),
        board_host=pick("board_host", HOST_KEY),
        port=port_number(pick("port", "TUNNEL_PORT", tunnel.DEFAULT_PORT)),
    )
    if not config.board_host:
        raise SystemExit(f"missing {HOST_KEY}: add the board's 100.x address to {env_file}")
    return config


def find_cli():
    """The tailscale command, from PATH or the default install folder; None if neither."""
    found = shutil.which("tailscale")
    if found:
        return found
    return str(INSTALLED_CLI) if INSTALLED_CLI.exists() else None


def describe_path(status, host):
    """One line saying how the tailnet reaches `host`, from `tailscale status --json`."""
    state = status.get("BackendState")
    if state != "Running":
        return f"Tailscale is not running here (state {state})"
    for peer in (status.get("Peer") or {}).values():
        names = {peer.get("HostName"), (peer.get("DNSName") or "").rstrip(".")}
        if host not in names and host not in (peer.get("TailscaleIPs") or []):
            continue
        name = peer.get("HostName") or host
        if not peer.get("Online"):
            return f"{name} is offline in the tailnet"
        if peer.get("CurAddr"):
            return f"{name}: direct via {peer['CurAddr']}"
        return f"{name}: relayed via DERP({peer.get('Relay') or '?'})"
    return f"{host} is not in this tailnet"


def path_report(host, run=subprocess.run):
    """Ask the tailscale CLI how `host` is reached. Never raises: the tunnel retries anyway."""
    cli = find_cli()
    if cli is None:
        return "tailscale command not found"
    try:
        result = run([cli, "status", "--json"], capture_output=True, text=True, timeout=10)
        return describe_path(json.loads(result.stdout), host)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"could not read tailscale status ({exc})"


async def one_session(config, password, stop):
    """Open one SSH hop to the board, forward the port, and hold until it drops or `stop`.

    `password` is unused; the argument is there because tunnel.run_tunnel passes it.
    """
    try:
        # No password or key given: asyncssh offers the laptop's key, as on the campus path.
        async with asyncssh.connect(
            config.board_host,
            username=config.board_user,
            keepalive_interval=tunnel.KEEPALIVE_INTERVAL_S,
            keepalive_count_max=tunnel.KEEPALIVE_COUNT_MAX,
        ) as board:
            log.info("connected to the board over Tailscale")
            # 127.0.0.1, not "localhost", on the laptop end: see tunnel.one_session.
            await board.forward_remote_port("localhost", config.port, "127.0.0.1", config.port)
            log.info(
                "tunnel open: board localhost:%d -> laptop 127.0.0.1:%d", config.port, config.port
            )
            await tunnel._hold_until_closed(board, stop)
    except asyncssh.PermissionDenied:
        log.error("the board refused this laptop's SSH key; check: ssh %s@%s",
                  config.board_user, config.board_host)
        raise
    except asyncssh.HostKeyNotVerifiable:
        log.error("the board's host key is not in known_hosts; run once: ssh %s@%s",
                  config.board_user, config.board_host)
        raise


def main():
    """Entry point: the Tailscale tunnel on its own, until Ctrl-C."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=tunnel.ENV_FILE)
    parser.add_argument("--board-user", help="override BOARD_USER")
    parser.add_argument("--board-host", help=f"override {HOST_KEY}")
    parser.add_argument("--port", type=port_number, help="override TUNNEL_PORT")
    args = parser.parse_args()

    log_file = setup_logging(LOG_FILE)
    config = load_config(
        args.env_file, board_user=args.board_user, board_host=args.board_host, port=args.port
    )
    if config.port != RELAY_PORT:
        # The relay runs on its default port here, so any other port forwards into nothing.
        parser.error(f"tunnel port {config.port} is not the relay's port {RELAY_PORT}")

    log.info("tunnel supervisor start; %s; logging to %s", config.describe(), log_file)
    log.info("tailscale path: %s", path_report(config.board_host))
    try:
        asyncio.run(tunnel.run_tunnel(config, None, asyncio.Event(), open_session=one_session))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
