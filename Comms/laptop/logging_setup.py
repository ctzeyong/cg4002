"""Console and file logging for every laptop entry point.

`ultra96/board_logging.py` is the board's copy; keep the two in step.
"""

import atexit
import logging
import os
import queue
import sys
import time
from logging.handlers import QueueHandler, QueueListener

# Times are UTC (the Z), so laptop and board logs line up side by side.
LINE_FORMAT = "%(asctime)sZ [%(name)s] %(message)s"

# Library log levels, kept here even under -v. asyncssh matters most: it logs the
# jump username and hostnames at INFO.
THIRD_PARTY_LEVELS = {
    "asyncssh": logging.WARNING,
    "asyncio": logging.INFO,
    "mqtt": logging.INFO,  # paho, via aiomqtt
    "bleak": logging.INFO,  # its scanner logs every advertisement in the room
}

# Mark a once-a-second counter line: `log.info(..., extra=PROGRESS)` gives it the
# counter shade instead of the event shade.
PROGRESS = {"progress": True}

# 256-colour codes: the basic 16 colours did not give enough distinct hues per link.
_RESET = "\033[0m"
_YELLOW = "\033[38;5;184m"
_BRIGHT_RED = "\033[38;5;196m"


def _sgr(index):
    return f"\033[38;5;{index}m"


# One colour per link, keyed by logger name, as (counter, event) 256-colour codes:
# counters and per-packet lines get the first, one-off events the brighter second.
# Warnings and errors are always yellow/red.
LINK_COLOURS = {
    # BLE is the exception: one shade each, because a second shade read as a second
    # board. This green is for lines about both boards; each board's is in NODE_COLOURS.
    "ble_client": (40, 40),  # green -- BLE, lines shared by both boards
    "file_transfer": (40, 40),
    "tcp_server": (33, 45),  # blue -- the Ultra96 leg
    "tcp_client": (33, 45),
    "mqtt_client": (170, 218),  # pink -- the phone leg
    "mqtt_publisher": (170, 218),
    "tunnel": (94, 130),  # orange -- the SSH tunnel
    "pipeline": (245, 255),  # grey -- the relay itself
    "pipeline_board_broker": (245, 255),
    "tcp_client_mqtt": (245, 255),  # the board's relay, same role
    "plumbing": (245, 255),
}

# Each FireBeetle is its own connection, so each board's BLE lines get
# a green of their own. Keyed by the `node` that ble_client.node_log attaches.
NODE_COLOURS = {
    1: (151, 151),  # pale green -- BLE, FireBeetle 1
    2: (35, 35),  # jade green -- BLE, FireBeetle 2
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
        leg = record.name.split(".")[0]
        pair = LINK_COLOURS.get(leg)
        if leg == "ble_client":
            pair = NODE_COLOURS.get(getattr(record, "node", None), pair)
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
    """Whether `stream` takes ANSI colour, enabling Windows VT mode if so."""
    stream = sys.stderr if stream is None else stream
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if sys.platform != "win32":
        return True

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-12)  # STD_ERROR_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False  # a console that refuses VT is not a reason to fail bring-up


def run_log_path(stem, now=None):
    """Turn `logs/pipeline.log` into `logs/<UTC date>/pipeline_<UTC time>Z.log`.

    One file per run of the program; reconnects keep writing to the same file.
    """
    stamp = time.gmtime() if now is None else now
    day = time.strftime("%Y-%m-%d", stamp)
    clock = time.strftime("%H%M%S", stamp)
    path = stem.parent / day / f"{stem.stem}_{clock}Z{stem.suffix}"

    # Two runs in the same second get -2, -3, ... rather than sharing a file.
    suffix = 2
    while path.exists():
        path = path.with_name(f"{stem.stem}_{clock}Z-{suffix}{stem.suffix}")
        suffix += 1
    return path


def setup_logging(log_file, verbose=False, colour=None, debug_libs=False):
    """Log to the console and a new per-run file without blocking the event loop.

    Writing happens on a QueueListener thread, so a slow or paused console cannot
    stall asyncio. `verbose` adds our per-packet lines; `debug_libs` also lets library
    logs through. Does nothing if logging is already set up. Returns the file path.
    """
    root = logging.getLogger()
    if root.handlers:
        return None

    log_file = run_log_path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    if colour is None:
        colour = console_supports_colour()
    formatter = logging.Formatter(LINE_FORMAT)
    formatter.converter = time.gmtime  # UTC, matching the Z in LINE_FORMAT
    console = ConsoleColourFormatter(LINE_FORMAT, enabled=colour)
    console.converter = time.gmtime
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(console)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)  # plain: grep reads this

    records = queue.SimpleQueue()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(QueueHandler(records))

    # Keeps usernames out of the log, so -v alone must not lift it.
    if not debug_libs:
        for name, level in THIRD_PARTY_LEVELS.items():
            logging.getLogger(name).setLevel(level)

    # Behind a QueueListener so a paused console cannot stall the event loop.
    listener = QueueListener(records, stream_handler, file_handler)
    listener.start()
    _log_listeners.append(listener)
    atexit.register(listener.stop)
    return log_file
