"""Follow the relay's newest log file and track what it says about the laptop side."""

import calendar
import collections
import logging
import re
import time

from dashboard import REPO, clean, redact, sleep_or_stop

RELAY_LOG_GLOB = "*/pipeline_board_broker_*.log"
LOG_POLL_S = 0.25
FIND_LOG_S = 2.0
MAX_READ_BYTES = 1024 * 1024  # per poll, so a long -v log is read in steps
# the relay's counters print once a second, so three missed means the data stopped
STALE_S = 3.0
WARNINGS_SHOWN = 5

LINE_PATTERN = re.compile(r"(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d),(\d{3})Z \[([^\]]+)\] (.*)")
NODE_PATTERN = re.compile(r"node (\d+): (.*)")
COUNTER_PATTERN = re.compile(r"n=\d+ seq=\d+ rate=([\d.]+) Hz .* missed=(\d+) dup=(\d+) .*"
                             r"sent=(\d+) echoed=(\d+)")
UPLINK_PATTERN = re.compile(
    r"uplink: forwarded=\d+ unsent=\d+ ([\d.]+) Hz [\d.]+ kbps queued=(\d+)"
)

# first words of the relay's BLE lines that change a glove's state
GLOVE_STATES = (
    ("scanning", "scanning"),
    ("nothing advertising", "not found"),
    ("found name", "found"),
    ("connected", "connected"),
    ("subscribed", "subscribed"),
    ("peripheral disconnected", "disconnected"),
    ("session ended", "disconnected"),
    ("session failed", "disconnected"),
    ("retrying", "retrying"),
    ("supervisor stopped", "stopped"),
)
TUNNEL_STATES = (
    ("attempt", "connecting"),
    ("tunnel open", "up"),
    ("session failed", "down"),
    ("session ended", "down"),
    ("remote port", "down"),
    ("authentication rejected", "login rejected"),
    ("reconnecting", "down, retrying"),
    ("tunnel supervisor stopped", "stopped"),
)
# the per-packet lines that -v adds, which never change a state
PACKET_PREFIXES = ("uplink: node=", "drained:", "recv from")
# words that mark a relay line as a warning or an error, since the log has no level field
WARNING_WORDS = (
    "fail", "error", "gap", "duplicate", "timed out", "unavailable", "rejected", "drop",
    "full", "discard", "unexpected", "unknown", "not acknowledged", "no message in",
    "peripheral disconnected", "nothing advertising", "session ended", "supersedes", "slow",
    "should publish", "no usable", "not on the main thread", "no console keyboard",
    "do not record", "fix the password", "the board is shared", "reader ended",
    "packet too short", "invalid", "imu missing", "imu readings restarting", "does not match",
)
# the relay's own loggers are named after its modules, and any other name is a library
OWN_LOGGERS = frozenset(path.stem for path in (REPO / "laptop").glob("*.py"))
# counters such as dropped=0 or cmd_failed=0, which are not warnings
KEY_VALUE_PATTERN = re.compile(r"[\w\[\]:]+=\S+")

log = logging.getLogger(__name__)


class Glove:
    """Holds one glove's BLE state, as the relay log reports it."""

    def __init__(self):
        self.state = None
        self.rate = None
        self.missed = self.dup = self.sent = self.echoed = 0
        self.counted_at = None


class Relay:
    """Tracks what the relay's log says about the laptop side."""

    def __init__(self, path=None):
        self.path = path
        self.last_line_at = None
        self.stopped = False
        self.tunnel = None
        self.tcp_clients = 0
        self.tcp_seen = False
        self.uplink = None
        self.uplink_at = None
        self.gloves = collections.defaultdict(Glove)
        self.warnings = collections.deque(maxlen=WARNINGS_SHOWN)
        self.warning_count = 0

    def apply(self, line):
        """Update from one log line. Lines that are not log records are ignored."""
        match = LINE_PATTERN.fullmatch(line)
        if not match:
            return
        year, month, day, hour, minute, second, millis, name, message = match.groups()
        stamp = calendar.timegm(
            (int(year), int(month), int(day), int(hour), int(minute), int(second), 0, 0, 0)
        ) + int(millis) / 1000
        self.last_line_at = stamp
        if message.startswith(PACKET_PREFIXES):
            return
        self._update(stamp, name, message)
        if is_warning(name, message):
            self.warning_count += 1
            text = redact(f"[{name}] {message}", None)
            self.warnings.append((f"{hour}:{minute}:{second}", clean(text)))

    def _update(self, stamp, name, message):
        """Apply a line's effect on the connection states."""
        if name == "ble_client":
            node = NODE_PATTERN.fullmatch(message)
            if node:
                self._update_glove(stamp, int(node[1]), node[2])
        elif name == "tunnel":
            self.tunnel = first_match(TUNNEL_STATES, message, self.tunnel)
        elif name == "tcp_server":
            if message.startswith("listening on"):
                self.tcp_clients, self.tcp_seen = 0, True
            elif message.startswith("client connected"):
                self.tcp_clients += 1
            elif message.startswith("client disconnected"):
                self.tcp_clients = max(0, self.tcp_clients - 1)
        elif name == "pipeline":
            uplink = UPLINK_PATTERN.fullmatch(message)
            if uplink:
                self.uplink = (float(uplink[1]), int(uplink[2]))
                self.uplink_at = stamp
            elif message.startswith(("interrupt received", "quit key pressed")):
                self.stopped = True
        elif name == "pipeline_board_broker" and message == "interrupted":
            self.stopped = True

    def _update_glove(self, stamp, node, message):
        glove = self.gloves[node]
        counter = COUNTER_PATTERN.fullmatch(message)
        if counter:
            glove.rate = float(counter[1])
            counts = (int(value) for value in counter.groups()[1:])
            glove.missed, glove.dup, glove.sent, glove.echoed = counts
            glove.counted_at = stamp
        else:
            glove.state = first_match(GLOVE_STATES, message, glove.state)


def first_match(states, message, current):
    """Return the state of the first prefix that `message` starts with, or `current` if none."""
    for prefix, state in states:
        if message.startswith(prefix):
            return state
    return current


def is_warning(name, message):
    """Return True if a relay log line is a warning or an error.

    The log has no level field, so this goes by the wording of the relay's warnings.
    Every line from a library counts, since none of them logs at INFO on the relay's path.
    """
    if name.split(".")[0] not in OWN_LOGGERS:
        return True
    text = KEY_VALUE_PATTERN.sub("", message).lower()
    return any(word in text for word in WARNING_WORDS)


def newest_relay_log(logs_dir):
    """Return the relay log file written to most recently, or None."""
    newest, newest_time = None, None
    for path in logs_dir.glob(RELAY_LOG_GLOB):
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue  # deleted between the glob and the stat
        if newest_time is None or modified > newest_time:
            newest, newest_time = path, modified
    return newest


def read_new_lines(path, offset, partial, apply):
    """Read what was added to `path` since `offset` and apply each whole line.

    Returns the new offset and any unfinished last line. The file is opened and closed
    each time, so this never holds it open while the relay writes.
    """
    with open(path, "rb") as file:
        file.seek(0, 2)
        if file.tell() < offset:
            offset, partial = 0, b""  # the file was replaced, so start again
        file.seek(offset)
        data = file.read(MAX_READ_BYTES)
    offset += len(data)
    lines = (partial + data).split(b"\n")
    partial = lines.pop()
    for line in lines:
        apply(line.decode("utf-8", "replace").rstrip("\r"))
    return offset, partial


async def follow_relay_log(state, logs_dir, stop):
    """Follow the newest relay log into `state.relay`, starting again when a newer one appears."""
    path, offset, partial, next_find = None, 0, b"", 0.0
    while not stop.is_set():
        if time.monotonic() >= next_find:
            next_find = time.monotonic() + FIND_LOG_S
            newest = newest_relay_log(logs_dir)
            if newest is not None and newest != path:
                path, offset, partial = newest, 0, b""
                state.relay = Relay(newest)
                log.info("following %s", newest)
        if path is not None:
            try:
                offset, partial = read_new_lines(path, offset, partial, state.relay.apply)
            except OSError as exc:
                log.warning("cannot read %s: %s", path, exc)
                path, next_find = None, 0.0
        await sleep_or_stop(stop, LOG_POLL_S)
