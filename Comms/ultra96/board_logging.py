"""Console and file logging for the Ultra96 scripts.

The board's copy of laptop/logging_setup.py (only ultra96/ is copied to the board).
Keep the two in step.

The log file matters most here: when the SSH session dies, the console goes with it.
"""

import atexit
import logging
import os
import pathlib
import queue
import sys
import time
from logging.handlers import QueueHandler, QueueListener

# Times are UTC (the Z), so laptop and board logs line up side by side.
LINE_FORMAT = "%(asctime)sZ [%(name)s] %(message)s"

# Under ~, never /tmp, which the board wipes on reboot. tcp_client_mqtt.py uses its
# own name so the two clients' logs never mix.
DEFAULT_LOG_FILE = pathlib.Path.home() / "comms" / "logs" / "tcp_client.log"

THIRD_PARTY_LEVELS = {
    "asyncio": logging.INFO,
    "mqtt": logging.INFO,  # paho, via aiomqtt
}

PROGRESS = {"progress": True}

# 256-colour codes, same as the laptop.
_RESET = "\033[0m"
_YELLOW = "\033[38;5;184m"
_BRIGHT_RED = "\033[38;5;196m"


def _sgr(index):
    return f"\033[38;5;{index}m"


# (counter, event) colours per link, the same scheme as the laptop.
LINK_COLOURS = {
    "tcp_client": (33, 45),  # blue -- the laptop leg
    "tcp_client_mqtt": (245, 255),  # grey -- the relay itself
    "mqtt_publisher": (170, 218),  # pink -- the broker
}

_log_listeners = []


class ConsoleColourFormatter(logging.Formatter):
    """Console formatter: one colour per link, a softer shade for frequent lines.

    Never attach it to the file handler: colour codes would break grep.
    """

    def __init__(self, fmt=LINE_FORMAT, enabled=True):
        super().__init__(fmt)
        self.enabled = enabled

    def colour_for(self, record):
        """The SGR prefix for one record, or "" for none."""
        if not self.enabled:
            return ""
        # Level outranks leg: a warning must not hide in its own colour.
        if record.levelno >= logging.ERROR:
            return _BRIGHT_RED
        if record.levelno >= logging.WARNING:
            return _YELLOW
        pair = LINK_COLOURS.get(record.name.split(".")[0])
        if pair is None:
            return ""
        counter, event = pair
        if record.levelno <= logging.DEBUG or getattr(record, "progress", False):
            return _sgr(counter)  # the two high-rate classes
        return _sgr(event)

    def format(self, record):
        line = super().format(record)
        prefix = self.colour_for(record)
        return f"{prefix}{line}{_RESET}" if prefix else line


def console_supports_colour(stream=None):
    """Whether `stream` takes ANSI colour.

    No Windows VT handling needed: the board is Linux, reached over SSH.
    """
    stream = sys.stderr if stream is None else stream
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", None)) and stream.isatty()


def run_log_path(stem, now=None):
    """Turn `logs/tcp_client.log` into `logs/<UTC date>/tcp_client_<UTC time>Z.log`.

    One file per run of the program; reconnects keep writing to the same file.
    """
    stem = pathlib.Path(stem)
    stamp = time.gmtime() if now is None else now
    day = time.strftime("%Y-%m-%d", stamp)
    clock = time.strftime("%H%M%S", stamp)
    path = stem.parent / day / f"{stem.stem}_{clock}Z{stem.suffix}"

    suffix = 2
    while path.exists():
        path = path.with_name(f"{stem.stem}_{clock}Z-{suffix}{stem.suffix}")
        suffix += 1
    return path


def setup_logging(verbose=False, log_file=DEFAULT_LOG_FILE, colour=None, debug_libs=False):
    """Log to the console and a new per-run file without blocking the event loop.

    Writing happens on a QueueListener thread. `log_file=None` means console only.
    Does nothing if logging is already set up. Returns the file path (or None).
    """
    root = logging.getLogger()
    if root.handlers:
        return None

    if colour is None:
        colour = console_supports_colour()
    formatter = logging.Formatter(LINE_FORMAT)
    formatter.converter = time.gmtime  # UTC, matching the Z in LINE_FORMAT
    console = ConsoleColourFormatter(LINE_FORMAT, enabled=colour)
    console.converter = time.gmtime
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(console)
    handlers = [stream_handler]

    if log_file is not None:
        log_file = run_log_path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)  # plain: grep reads this
        handlers.append(file_handler)

    records = queue.SimpleQueue()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(QueueHandler(records))

    if not debug_libs:
        for name, level in THIRD_PARTY_LEVELS.items():
            logging.getLogger(name).setLevel(level)

    # Behind a QueueListener so a paused console cannot stall the event loop.
    listener = QueueListener(records, *handlers)
    listener.start()
    _log_listeners.append(listener)
    atexit.register(listener.stop)
    return log_file
