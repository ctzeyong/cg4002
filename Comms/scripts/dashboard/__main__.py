"""Start the dashboard. The package's docstring in __init__.py lists the flags."""

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

# run as a folder, so put scripts/ on the path for this package and run_board
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard  # noqa: E402  first, since it puts the laptop and board modules on the path
import tunnel  # noqa: E402
from dashboard import board_link, drawing, relay_log, stop_all  # noqa: E402
from logging_setup import LINE_FORMAT, THIRD_PARTY_LEVELS, run_log_path  # noqa: E402
from run_board import install_interrupt_handler  # noqa: E402

LOG_FILE = dashboard.REPO / "logs" / "dashboard.log"
RELAY_LOGS = dashboard.REPO / "logs"
# how long Ctrl-C waits for the tasks to finish before cancelling, e.g. a hung SSH connect
SHUTDOWN_S = 2.0


class Dashboard:
    """Holds everything shown on the screen. Only tasks on this one event loop touch it."""

    def __init__(self, watch_board=True):
        self.relay = relay_log.Relay()
        self.board = board_link.Board(watch_board)


async def run(state, screen, config, password, logs_dir=RELAY_LOGS):
    """Run the readers and the redraw until Ctrl-C."""
    stop = asyncio.Event()
    restore = install_interrupt_handler(stop)
    tasks = [
        asyncio.create_task(drawing.draw_loop(state, screen, stop)),
        asyncio.create_task(relay_log.follow_relay_log(state, logs_dir, stop)),
    ]
    if config is not None:
        tasks.append(
            asyncio.create_task(board_link.watch_board(config, password, state.board, stop))
        )
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()  # re-raise a crash
        # watch_board returns early only after a rejected login, so keep showing that
        await stop.wait()
    finally:
        stop.set()  # before any cancel, so a wait_for loop cannot swallow it
        restore()
        _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_S)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def setup_file_logging():
    """Log to a per-run file only, since the console belongs to the screen."""
    log_file = run_log_path(LOG_FILE)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file, encoding="utf-8")
    formatter = logging.Formatter(LINE_FORMAT)
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name, level in THIRD_PARTY_LEVELS.items():
        logging.getLogger(name).setLevel(level)  # asyncssh logs usernames at INFO
    return log_file


def main():
    """Load the SSH settings, then show the screen until Ctrl-C."""
    parser = argparse.ArgumentParser(description=dashboard.__doc__.splitlines()[0])
    parser.add_argument("--no-board", action="store_true", help="show the laptop rows only, no SSH")
    parser.add_argument(
        "--stop-pipeline", metavar="RUN_ID",
        help="on Ctrl-C, also press Ctrl-C in the relay and board client windows that "
        "run_all.ps1 opened with this id",
    )
    args = parser.parse_args()

    config = password = None
    if not args.no_board:
        config = tunnel.load_config()
        # before logging is set up, so a password prompt shows on the console
        password = tunnel.load_password(config.jump_host)
    setup_file_logging()

    screen = drawing.Screen(sys.stdout)
    try:
        asyncio.run(run(Dashboard(watch_board=not args.no_board), screen, config, password))
    except KeyboardInterrupt:
        pass  # a second Ctrl-C
    finally:
        screen.close()
    if args.stop_pipeline:
        stop_all.stop_pipeline(args.stop_pipeline)


if __name__ == "__main__":
    main()
