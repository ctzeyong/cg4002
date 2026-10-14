"""Start configuration A's board client over Tailscale: one SSH hop, no jump host, no password.

Pane 2 of two; pane 1 is run_pipeline_a_hotspot.ps1, and run_board_a_hotspot.ps1 wraps
this file. Same board command as run_board_a.py; only the connection differs.
Decisions: notes/ultra96_setup.md -> "Fallback D".

    python run_board_a_hotspot.py                  # start it, or re-attach if running
    python run_board_a_hotspot.py --sync           # re-copy ultra96/*.py, then restart it
    python run_board_a_hotspot.py --stop           # kill it and exit
    python run_board_a_hotspot.py --fetch-logs     # copy today's (UTC) board logs here, then exit

Ctrl-C stops the board client.
"""

import asyncio

import asyncssh

import run_board_a  # first: it puts laptop/ on sys.path for the imports below
from run_board import REPO, fetch_logs, log, sync_sources, tunnel
from logging_setup import setup_logging
import tunnel_tailscale

LOG_FILE = REPO / "logs" / "run_board_a_hotspot.log"


async def run(config, args):
    """Open the one hop, do the work, and stream until interrupted."""
    async with asyncssh.connect(
        config.board_host,
        username=config.board_user,
        keepalive_interval=tunnel.KEEPALIVE_INTERVAL_S,
        keepalive_count_max=tunnel.KEEPALIVE_COUNT_MAX,
    ) as board:
        log.info("connected to the board over Tailscale")
        if args.stop:
            await run_board_a.stop_client(board)
            return
        if args.fetch_logs:
            await fetch_logs(board, args.fetch_logs)
            return
        if args.sync:
            await sync_sources(board)
        command = run_board_a.remote_command(args.port, args.client_args, restart=args.sync)
        await run_board_a.attach(board, command)


def main():
    """Entry point: load .env settings, then run until Ctrl-C."""
    args = run_board_a.parse_args()

    log_file = setup_logging(LOG_FILE)
    config = tunnel_tailscale.load_config()
    if args.port is None:
        args.port = config.port

    log.info(
        "configuration A board client, %s over Tailscale, port %d; logging to %s",
        config.board_host,
        args.port,
        log_file,
    )
    log.info("tailscale path: %s", tunnel_tailscale.path_report(config.board_host))
    try:
        asyncio.run(run(config, args))
    except asyncssh.PermissionDenied as exc:
        raise SystemExit(f"the board refused this laptop's SSH key: {exc.reason or exc}")
    except (OSError, asyncssh.Error) as exc:
        raise SystemExit(f"could not reach the board over Tailscale: {exc}")
    except KeyboardInterrupt:
        log.info("interrupted; the board client may still be running (--stop ends it)")


if __name__ == "__main__":
    main()
