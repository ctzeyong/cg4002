"""Per-hop latency from one run's logs: laptop, board and phone.

    python latency_report.py --config B --board <tcp_client_mqtt_*.log> --phone logs/phone/run1
    python latency_report.py --config A --board <tcp_client_*.log> --laptop <pipeline_*.log> --phone logs/phone/run1

Joins the per-event INFO lines each hop already writes, converts board and phone times onto
the laptop's perf_counter with their clock offsets, and prints median / p95 / max per hop.
Events missing a link are counted, never dropped.

Every time is read from a field in the line, never the line's own timestamp, so
run_board*.py's relayed copy works as the board log too, provided it covers the whole run (a
fresh start, not a re-attach that tails only the last 200 lines). The board's own file, via
--fetch-logs, is the canonical one.

--phone takes the prefix scripts/tools/phone_clock.py wrote: <prefix>_start.json, <prefix>_end.json
and <prefix>_logcat.txt.

    python latency_report.py --config B --echo <echo_*.log> [--board <tcp_client_mqtt_*.log>]

--echo times the crossing between the board and each phone, on any phone, from the board's
capture of gestures and echoes. Adding --board also gives laptop receipt to phone.
"""

import argparse
import ast
import csv
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

NUM = r"-?\d+(?:\.\d+)?"

# Board lines (ultra96/tcp_client.py, tcp_client_mqtt.py, mqtt_publisher.py).
RATE = re.compile(rf"uplink rx: .*offset=(?P<offset>{NUM}) rtt=(?P<rtt>{NUM})ms")
TRIGGER = re.compile(
    rf"trigger node=(?P<node>\d+) seq=(?P<seq>\S+) "
    rf"t_laptop_rx=(?P<t_laptop_rx>{NUM}|-) t_rx=(?P<t_rx>{NUM})"
)
# only a classified event's line has t_infer_ms, a dummy's doesn't
EVENT = re.compile(
    rf"AI event seq=(?P<seq>\d+) player=(?P<player>\S+) .*?t_event=(?P<t_event>{NUM})"
    rf"(?: conf=\S+ t_infer_ms=(?P<t_infer_ms>{NUM}))?"
)
# Both relays' publish line. t= is board wall clock from the board, and laptop perf_counter
# from the laptop-broker relay.
PUBLISHED = re.compile(rf"published (?P<event_id>\S+) to (?P<topic>\S+) t=(?P<t>{NUM})")

# Laptop line in the laptop-broker setup (laptop/pipeline.py forward_downlink). The event is
# a Python dict repr, single-quoted, so it is read with ast.literal_eval, not json.
DOWNLINK = re.compile(rf"downlink seq=(?P<seq>\d+) t=(?P<t>{NUM}) -> (?P<topic>\S+): (?P<msg>\{{.*\}})\s*$")

# Phone lines: `adb logcat -v epoch -v usec`, where AddLine's Debug.Log copy of an event reads
# "<epoch> <pid> <tid> I Unity   : [tunnelled] 01:31:20 player=1 ultra96-relay:ab12cd34:7 {...}".
# The tag is LogTag in tunnelled-visualiser, or in demo-visualiser for the laptop-broker setup.
PHONE_TAGS = ("[demo]", "[tunnelled]")
LOGCAT_TIME = re.compile(r"^\s*(?P<t>\d+\.\d+)\s")
PHONE_EVENT = re.compile(
    r"^\s*(?P<t>\d+\.\d+)\s.*?(?:\[demo\]|\[tunnelled\]) \S+ player=\S+ (?P<event_id>\S+:\d+) "
)

# (column, label). Durations in ms.
HOPS = [
    ("laptop_rx_to_board_rx", "1 -> 3  laptop rx -> board rx"),
    ("board_rx_to_event", "3 -> 4  board rx -> AI event"),
    ("inference", "  in 3 -> 4: inference (t_infer_ms)"),
    ("laptop_rx_to_event", "1 -> 4  laptop rx -> AI event"),
    ("event_to_laptop_downlink", "4 -> A  AI event -> back on laptop"),
    ("event_to_publish_acked", "side   AI event -> publish acked"),
    ("laptop_downlink_to_phone", "A -> 7  back on laptop -> phone"),
    ("event_to_phone", "4 -> 7  AI event -> phone applies"),
    ("laptop_rx_to_phone", "1 -> 7  TOTAL laptop rx -> phone"),
]


@dataclass
class Event:
    event_seq: int
    player: str
    t_event: float  # board wall clock
    t_infer_ms: float = None  # the classifier's own time in ms, None for a dummy event
    offset: float = None  # board clock minus laptop perf_counter, in force at the event
    rtt: float = None
    node: int = None
    ble_seq: str = None
    t_laptop_rx: float = None  # laptop perf_counter
    t_board_rx: float = None  # board wall clock
    relay_seq: int = None  # A: the laptop's envelope seq
    t_downlink: float = None  # A: laptop perf_counter
    t_published: float = None  # B: board wall clock; A: laptop perf_counter
    event_id: str = None  # from the publish line; the phone's line carries the same id
    t_phone: float = None  # phone wall clock, from logcat
    phone_offset: float = None  # phone clock minus laptop perf_counter, at t_phone
    hops: dict = field(default_factory=dict)


@dataclass
class Counts:
    triggers_without_event: int = 0
    events_without_trigger: int = 0
    events_without_offset: int = 0
    published_unmatched: int = 0
    downlinks_unmatched: int = 0
    events_without_downlink: int = 0
    events_without_published: int = 0
    phone_lines_unmatched: int = 0
    phone_duplicates: int = 0
    events_without_phone: int = 0


A_ONLY = ("downlinks_unmatched", "events_without_downlink")
PHONE_ONLY = ("phone_lines_unmatched", "phone_duplicates", "events_without_phone")


class PhoneClock:
    """The phone offset, interpolated linearly between the before and after syncs.

    Same sign as the board's: phone_time_on_laptop_clock = phone_time - offset.
    """

    def __init__(self, start, end=None):
        self.start = start
        self.end = end or start

    @classmethod
    def from_prefix(cls, prefix):
        start = json.loads(Path(f"{prefix}_start.json").read_text(encoding="utf-8"))
        end_path = Path(f"{prefix}_end.json")
        end = json.loads(end_path.read_text(encoding="utf-8")) if end_path.exists() else None
        return cls(start, end)

    def offset_at(self, phone_time):
        span = self.end["phone_time"] - self.start["phone_time"]
        if span <= 0:
            return self.start["offset"]
        frac = (phone_time - self.start["phone_time"]) / span
        return self.start["offset"] + frac * (self.end["offset"] - self.start["offset"])

    def to_laptop(self, phone_time):
        return phone_time - self.offset_at(phone_time)

    @property
    def bound_ms(self):
        return max(self.start["rtt"], self.end["rtt"]) / 2 * 1000

    @property
    def drift_ms(self):
        return (self.end["offset"] - self.start["offset"]) * 1000


def _event_id_seq(event_id):
    """`source:session:seq` -> seq, or None."""
    tail = event_id.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else None


def parse_board(lines, config, counts):
    """Board log -> events in log order, each with its trigger and offset attached."""
    events = []
    by_seq = {}  # latest event per seq; A restarts seq on reconnect, so order matters
    offset = None
    first_offset = None  # for events logged before the first offset line
    pending = None  # trigger waiting for its AI event

    for line in lines:
        if m := RATE.search(line):
            offset = (float(m["offset"]), float(m["rtt"]) / 1000)
            first_offset = first_offset or offset
        elif m := TRIGGER.search(line):
            if pending is not None:
                counts.triggers_without_event += 1
            t_laptop_rx = m["t_laptop_rx"]
            pending = {
                "node": int(m["node"]),
                "ble_seq": m["seq"],
                "t_laptop_rx": None if t_laptop_rx == "-" else float(t_laptop_rx),
                "t_board_rx": float(m["t_rx"]),
            }
        elif m := EVENT.search(line):
            event = Event(int(m["seq"]), m["player"], float(m["t_event"]))
            if m["t_infer_ms"] is not None:
                event.t_infer_ms = float(m["t_infer_ms"])
            if offset is not None:
                event.offset, event.rtt = offset
            # receiver logs the trigger then awaits emit, so its event is the next one.
            if pending is not None and str(pending["node"]) == event.player:
                for key, value in pending.items():
                    setattr(event, key, value)
            else:
                if pending is not None:
                    counts.triggers_without_event += 1
                counts.events_without_trigger += 1
            pending = None
            events.append(event)
            by_seq[event.event_seq] = event
        elif config == "B" and (m := PUBLISHED.search(line)):
            event = by_seq.get(_event_id_seq(m["event_id"]))
            if event is None or event.t_published is not None:
                counts.published_unmatched += 1
            else:
                event.t_published = float(m["t"])
                event.event_id = m["event_id"]

    if pending is not None:
        counts.triggers_without_event += 1

    # Events before the first offset line take the first offset measured after them.
    for event in events:
        if event.offset is None and first_offset is not None:
            event.offset, event.rtt = first_offset
        if event.offset is None:
            counts.events_without_offset += 1
    return events


def _time_key(value):
    return f"{value:.6f}"


def parse_laptop_a(lines, events, counts):
    """A only: attach the laptop's downlink and publish times to the board's events.

    Joined on (player, board seq, event timestamp): exact, and still unique after a
    reconnect restarts the board's seq.
    """
    by_key = {(e.player, e.event_seq, _time_key(e.t_event)): e for e in events}
    by_relay_seq = {}
    for line in lines:
        if m := DOWNLINK.search(line):
            try:
                msg = ast.literal_eval(m["msg"])
                key = (str(msg["player"]), msg["seq"], _time_key(msg["timestamp"]))
                event = by_key.get(key)  # inside the try, since a list in the key fails here
            except (ValueError, SyntaxError, KeyError, TypeError):
                counts.downlinks_unmatched += 1
                continue
            if event is None:
                counts.downlinks_unmatched += 1
                continue
            event.relay_seq = int(m["seq"])
            event.t_downlink = float(m["t"])
            by_relay_seq[event.relay_seq] = event
        elif m := PUBLISHED.search(line):
            event = by_relay_seq.get(_event_id_seq(m["event_id"]))
            if event is None or event.t_published is not None:
                counts.published_unmatched += 1
            else:
                event.t_published = float(m["t"])
                event.event_id = m["event_id"]


def parse_phone(lines, events, clock, counts):
    """Attach the phone's apply time to each event, by event_id.

    The first line per event_id counts; a repeat is counted, not used. The app drops QoS 1
    duplicates itself, but a restarted app forgets what it has seen. Returns the earliest
    time anywhere in the dump (phone clock), for the overwritten-buffer check.
    """
    by_id = {e.event_id: e for e in events if e.event_id}
    earliest = None
    for line in lines:
        if m := LOGCAT_TIME.match(line):
            t = float(m["t"])
            earliest = t if earliest is None else min(earliest, t)
        if not (m := PHONE_EVENT.match(line)):
            continue
        event = by_id.get(m["event_id"])
        if event is None:
            counts.phone_lines_unmatched += 1
        elif event.t_phone is not None:
            counts.phone_duplicates += 1
        else:
            event.t_phone = float(m["t"])
            event.phone_offset = clock.offset_at(event.t_phone)
    return earliest


def compute_hops(events, config, counts, phone=False):
    """Fill each event's hop durations (ms). Board and phone times go onto the laptop clock
    as time - offset; a hop wholly on one clock needs no offset."""
    for e in events:
        to_laptop = (lambda t: t - e.offset) if e.offset is not None else None
        if e.t_board_rx is not None:
            e.hops["board_rx_to_event"] = (e.t_event - e.t_board_rx) * 1000
        if e.t_infer_ms is not None:
            e.hops["inference"] = e.t_infer_ms  # already in ms
        if to_laptop and e.t_laptop_rx is not None and e.t_board_rx is not None:
            e.hops["laptop_rx_to_board_rx"] = (to_laptop(e.t_board_rx) - e.t_laptop_rx) * 1000
            e.hops["laptop_rx_to_event"] = (to_laptop(e.t_event) - e.t_laptop_rx) * 1000
        if config == "A":
            if e.t_downlink is None:
                counts.events_without_downlink += 1
            elif to_laptop:
                e.hops["event_to_laptop_downlink"] = (e.t_downlink - to_laptop(e.t_event)) * 1000
        if e.t_published is None:
            counts.events_without_published += 1
        elif config == "B":
            e.hops["event_to_publish_acked"] = (e.t_published - e.t_event) * 1000
        elif to_laptop:
            e.hops["event_to_publish_acked"] = (e.t_published - to_laptop(e.t_event)) * 1000

        if not phone:
            continue
        if e.t_phone is None:
            counts.events_without_phone += 1
            continue
        phone_on_laptop = e.t_phone - e.phone_offset
        if e.t_downlink is not None:
            e.hops["laptop_downlink_to_phone"] = (phone_on_laptop - e.t_downlink) * 1000
        if to_laptop:
            e.hops["event_to_phone"] = (phone_on_laptop - to_laptop(e.t_event)) * 1000
        if e.t_laptop_rx is not None:
            e.hops["laptop_rx_to_phone"] = (phone_on_laptop - e.t_laptop_rx) * 1000


def summarise(values):
    """(n, median, p95, max) with nearest-rank p95, or None for no samples."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    p95 = ordered[max(0, math.ceil(0.95 * n) - 1)]
    return n, median, p95, ordered[-1]


@dataclass
class Run:
    events: list
    counts: Counts
    phone_clock: PhoneClock = None
    logcat_earliest: float = None  # phone clock


def report(board_lines, config, laptop_lines=None, phone_clock=None, logcat_lines=None):
    """Parse and join one run."""
    counts = Counts()
    events = parse_board(board_lines, config, counts)
    if config == "A" and laptop_lines is not None:
        parse_laptop_a(laptop_lines, events, counts)
    earliest = None
    phone = phone_clock is not None and logcat_lines is not None
    if phone:
        earliest = parse_phone(logcat_lines, events, phone_clock, counts)
    compute_hops(events, config, counts, phone=phone)
    return Run(events, counts, phone_clock if phone else None, earliest)


def buffer_overwritten(run):
    """True if the logcat dump starts after the run's first event: the ring buffer wrapped
    and the earliest phone lines are gone, so the phone hops describe a partial run."""
    if run.phone_clock is None or run.logcat_earliest is None:
        return False
    first = next((e for e in run.events if e.offset is not None), None)
    if first is None:
        return False
    first_event_laptop = first.t_event - first.offset
    return run.phone_clock.to_laptop(run.logcat_earliest) > first_event_laptop


def write_csv(run, path):
    columns = [
        "player", "event_seq", "event_id", "node", "ble_seq", "relay_seq",
        "t_laptop_rx", "t_board_rx", "t_event", "t_downlink", "t_published", "t_phone",
        "offset", "offset_bound_ms", "phone_offset",
    ] + [name for name, _ in HOPS]
    with open(path, "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(columns)
        for e in run.events:
            bound = e.rtt / 2 * 1000 if e.rtt is not None else None
            row = [
                e.player, e.event_seq, e.event_id, e.node, e.ble_seq, e.relay_seq,
                e.t_laptop_rx, e.t_board_rx, e.t_event, e.t_downlink, e.t_published, e.t_phone,
                e.offset, bound, e.phone_offset,
            ] + [e.hops.get(name) for name, _ in HOPS]
            writer.writerow(["" if v is None else v for v in row])


def _causality(events, name, bound, out):
    fastest = [e.hops[name] for e in events if name in e.hops]
    if fastest:
        verdict = "ok" if min(fastest) >= -bound else "NEGATIVE: an offset is wrong"
        print(f"causality: fastest {name} {min(fastest):.3f} ms ({verdict})", file=out)


def print_summary(run, config, out=sys.stdout):
    events, counts, clock = run.events, run.counts, run.phone_clock
    print(f"configuration {config}: {len(events)} AI events", file=out)

    rtts = [e.rtt for e in events if e.rtt is not None]
    offsets = [e.offset for e in events if e.offset is not None]
    bound = max(rtts) / 2 * 1000 if rtts else None
    if offsets:
        drift_ms = (max(offsets) - min(offsets)) * 1000
        print(
            f"board offset: bound <= {bound:.3f} ms (worst rtt/2 used), "
            f"spread over the run {drift_ms:.3f} ms",
            file=out,
        )
        # a wrong offset pushes the two directions apart, so one of them goes negative
        _causality(events, "laptop_rx_to_board_rx", bound, out)
        _causality(events, "event_to_laptop_downlink", bound, out)
    else:
        print("board offset: none found; cross-clock hops cannot be computed", file=out)

    if clock is not None:
        print(
            f"phone offset: bound <= {clock.bound_ms:.3f} ms, "
            f"drift start -> end {clock.drift_ms:+.3f} ms (interpolated)",
            file=out,
        )
        if bound is not None:
            _causality(events, "event_to_phone", bound + clock.bound_ms, out)
        _causality(events, "laptop_downlink_to_phone", clock.bound_ms, out)
        if buffer_overwritten(run):
            print(
                "WARNING: the logcat dump starts after the run's first event; the buffer "
                "wrapped and the phone hops are partial. Enlarge it: phone_clock.py start --buffer 16M",
                file=out,
            )

    print(f"\n{'hop':40} {'n':>5} {'median':>9} {'p95':>9} {'max':>9}  (ms)", file=out)
    for name, label in HOPS:
        stats = summarise([e.hops[name] for e in events if name in e.hops])
        if stats is None:
            continue
        n, median, p95, worst = stats
        print(f"{label:40} {n:5d} {median:9.3f} {p95:9.3f} {worst:9.3f}", file=out)

    print("\nmissing links:", file=out)
    for name, value in vars(counts).items():
        if config == "B" and name in A_ONLY:
            continue
        if clock is None and name in PHONE_ONLY:
            continue
        print(f"  {name}: {value}", file=out)


# --- the crossing between the board and a phone, from an echo capture ---
# each capture line is mosquitto_sub's '%U %t %p', which is board time, topic and envelope

ECHO_WINDOW_S = 30.0  # a phone's clock offset is assumed constant within a window
T_EVENT_MATCH_S = 0.001  # an echoed t_event matches the board's to well within this

# (key, label), durations in ms
ECHO_LEGS = [
    ("phone_to_board", "phone -> board"),
    ("event_to_phone", "4 -> phone  AI event -> phone"),
    ("laptop_rx_to_phone", "1 -> phone  laptop rx -> phone"),
    ("round_trip", "round trip from the capture, no offset"),
]


@dataclass
class Gesture:
    event_id: str
    seq: int
    t_event: float  # board clock, from the payload
    t_capture: float  # board clock


@dataclass
class Echo:
    phone: str  # the envelope's source, new each time the app starts
    player: str  # from the topic
    seq: int
    t_event: float  # copied from the gesture
    t_phone: float  # phone clock, the envelope's ts
    t_echo: float  # board clock


@dataclass
class EchoPair:
    echo: Echo
    gesture: Gesture
    window_status: str = None  # "tested", "untested" (one pair) or "rejected"
    offset: float = None  # phone clock minus board clock
    bound: float = None  # offset error, if the offset held still
    legs: dict = field(default_factory=dict)


@dataclass
class EchoRun:
    pairs: list
    phones: dict  # a Counter per phone
    gestures: int
    echoes: int
    unreadable: int
    unjoined: int = None  # pairs not in the board log, None without --board


def _number(value):
    """Return a JSON number as a float, or raise ValueError if it isn't a finite one."""
    # float() alone takes NaN, inf, True and numbers written as strings
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(value)
    return float(value)


def parse_capture(lines):
    """Split capture lines into gestures, echoes and a count of unreadable lines."""
    gestures, echoes, unreadable = [], [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            t, topic, raw = line.split(" ", 2)
            env = json.loads(raw)
            payload = env["payload"]
            seq = payload["seq"]
            if type(seq) is not int:  # a list can't key the pairing, and True would equal 1
                raise TypeError(seq)
            if topic.startswith("game/gesture/"):
                if type(env["event_id"]) is not str:  # it keys the pairing, so a list would crash
                    raise TypeError(env["event_id"])
                gestures.append(
                    Gesture(env["event_id"], seq, _number(payload["timestamp"]), _number(float(t)))
                )
            elif topic.startswith("viz/echo/"):
                if type(env["source"]) is not str:  # it keys the phones, so a list would crash
                    raise TypeError(env["source"])
                player = topic.rsplit("/", 1)[1]
                echoes.append(
                    Echo(env["source"], player, seq, _number(payload["t_event"]),
                         _number(env["ts"]), _number(float(t)))
                )
            else:
                unreadable += 1
        # an int too big for a float overflows, and very deep JSON recurses too far
        except (ValueError, KeyError, TypeError, OverflowError, RecursionError):
            unreadable += 1
    return gestures, echoes, unreadable


def pair_echoes(gestures, echoes, phones):
    """Pair each echo with its gesture on seq and t_event, and count any that don't pair one to one.

    seq alone restarts with the board, so an old echo could pair with a newer gesture. A gesture
    captured twice (a resend) is ambiguous, since we can't tell which copy the phone answered.
    """
    by_seq = defaultdict(list)
    for g in gestures:
        by_seq[g.seq].append(g)

    answered = defaultdict(set)  # the event_ids each phone echoed
    hits = defaultdict(list)  # the (echo, gesture) matches for each phone and event_id
    for e in echoes:
        found = [g for g in by_seq[e.seq] if abs(g.t_event - e.t_event) <= T_EVENT_MATCH_S]
        answered[e.phone].update(g.event_id for g in found)
        if not found:
            phones[e.phone]["echoes with no gesture"] += 1
        elif len(found) > 1:
            phones[e.phone]["ambiguous"] += 1
        else:
            hits[(e.phone, found[0].event_id)].append((e, found[0]))

    pairs = []
    for (phone, _), matches in hits.items():
        if len(matches) > 1:
            phones[phone]["ambiguous"] += len(matches)
        else:
            pairs.append(EchoPair(*matches[0]))

    # count misses only while the phone was answering, since before that it couldn't echo
    unique = {g.event_id: g for g in gestures}.values()
    for phone, ids in answered.items():
        times = [g.t_capture for g in unique if g.event_id in ids]
        if times:
            first, last = min(times), max(times)
            phones[phone]["gestures with no echo"] = sum(
                1 for g in unique if first <= g.t_capture <= last and g.event_id not in ids
            )
    return pairs


def fit_offsets(pairs, phones):
    """Estimate each phone's clock offset per window, then each pair's legs in ms.

    Each pair puts the offset between t_phone - t_echo and t_phone - t_event. The estimate is the
    middle of where a window's pairs overlap, and a window with no overlap is rejected.
    """
    start = min((p.gesture.t_event for p in pairs), default=0.0)
    windows = defaultdict(list)
    for p in pairs:
        windows[(p.echo.phone, (p.gesture.t_event - start) // ECHO_WINDOW_S)].append(p)

    for (phone, _), members in windows.items():
        low = max(p.echo.t_phone - p.echo.t_echo for p in members)
        high = min(p.echo.t_phone - p.gesture.t_event for p in members)
        if low > high:
            status = "rejected"
        else:
            status = "tested" if len(members) > 1 else "untested"
        phones[phone][f"windows {status}"] += 1

        for p in members:
            p.window_status = status
            p.legs["round_trip"] = (p.echo.t_echo - p.gesture.t_capture) * 1000
            if status == "rejected":
                continue
            p.offset, p.bound = (low + high) / 2, (high - low) / 2
            if status == "untested":
                # one echo alone just assumes both directions took equally long
                continue
            on_board = p.echo.t_phone - p.offset  # on the board's clock
            p.legs["phone_to_board"] = (p.echo.t_echo - on_board) * 1000
            p.legs["event_to_phone"] = (on_board - p.gesture.t_event) * 1000


def echo_report(capture_lines, events=None):
    """Parse and pair one capture, and with the board log's events add laptop receipt to phone."""
    gestures, echoes, unreadable = parse_capture(capture_lines)
    phones = defaultdict(Counter)
    pairs = pair_echoes(gestures, echoes, phones)
    fit_offsets(pairs, phones)
    run = EchoRun(pairs, phones, len(gestures), len(echoes), unreadable)

    if events is not None:
        by_id = {e.event_id: e for e in events if e.event_id}
        run.unjoined = 0
        for p in pairs:
            event = by_id.get(p.gesture.event_id)
            if event is None:
                run.unjoined += 1
            elif "laptop_rx_to_event" in event.hops and "event_to_phone" in p.legs:
                p.legs["laptop_rx_to_phone"] = event.hops["laptop_rx_to_event"] + p.legs["event_to_phone"]
    return run


def print_echo_summary(run, out=sys.stdout):
    joined = "" if run.unjoined is None else f", {run.unjoined} pairs not in the board log"
    print(
        f"echo capture: {run.gestures} gestures, {run.echoes} echoes, "
        f"{run.unreadable} unreadable lines{joined}",
        file=out,
    )
    for phone, counts in sorted(run.phones.items()):
        pairs = [p for p in run.pairs if p.echo.phone == phone]
        players = ", ".join(sorted({p.echo.player for p in pairs})) or "-"
        print(f"\nphone {phone} (player {players}): {len(pairs)} pairs", file=out)
        for name in ("gestures with no echo", "echoes with no gesture", "ambiguous"):
            print(f"  {name}: {counts[name]}", file=out)
        # tested windows only, since no other window gives a one-way leg
        bounds = [p.bound * 1000 for p in pairs if p.window_status == "tested"]
        worst = f"; worst bound {max(bounds):.3f} ms" if bounds else ""
        print(
            f"  windows: {counts['windows tested']} tested, {counts['windows untested']} untested "
            f"(one pair, round trip only), {counts['windows rejected']} rejected (offset moved)"
            f"{worst}",
            file=out,
        )
        print(f"  {'leg':40} {'n':>5} {'median':>9} {'p95':>9} {'max':>9}  (ms)", file=out)
        for name, label in ECHO_LEGS:
            stats = summarise([p.legs[name] for p in pairs if name in p.legs])
            if stats is not None:
                n, median, p95, worst_ms = stats
                print(f"  {label:40} {n:5d} {median:9.3f} {p95:9.3f} {worst_ms:9.3f}", file=out)
    print(
        "\nOne-way legs come only from tested windows, each within its window's bound if the "
        "offset held still. Laptop rx -> phone also carries the board offset's bound.",
        file=out,
    )


def write_echo_csv(run, path):
    columns = [
        "phone", "player", "event_id", "seq", "t_event", "t_capture", "t_phone", "t_echo",
        "window_status", "offset", "bound_ms",
    ] + [name for name, _ in ECHO_LEGS]
    with open(path, "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(columns)
        for p in run.pairs:
            e, g = p.echo, p.gesture
            bound = p.bound * 1000 if p.bound is not None else None
            row = [
                e.phone, e.player, g.event_id, g.seq, g.t_event, g.t_capture, e.t_phone, e.t_echo,
                p.window_status, p.offset, bound,
            ] + [p.legs.get(name) for name, _ in ECHO_LEGS]
            writer.writerow(["" if v is None else v for v in row])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", choices=("A", "B"), required=True)
    parser.add_argument("--board", type=Path, help="the board's log file (required unless --echo)")
    parser.add_argument("--laptop", type=Path, help="pipeline_*.log (A: needed for the return hop)")
    parser.add_argument("--phone", help="prefix written by phone_clock.py, e.g. logs/phone/run1")
    parser.add_argument("--echo", type=Path, help="configuration B only, the board's capture of gestures and echoes")
    parser.add_argument("--csv", type=Path, help="write one row per event here, or per pair with --echo")
    args = parser.parse_args(argv)

    if args.echo:
        if args.config != "B":
            parser.error("--echo needs --config B: the capture is taken on the board's broker")
        events = None
        if args.board:
            events = report(args.board.read_text(encoding="utf-8").splitlines(), "B").events
        run = echo_report(args.echo.read_text(encoding="utf-8").splitlines(), events)
        print_echo_summary(run, sys.stdout)
        if args.csv:
            write_echo_csv(run, args.csv)
            print(f"\nwrote {args.csv}")
        return
    if args.board is None:
        parser.error("--board is required unless --echo")

    board_lines = args.board.read_text(encoding="utf-8").splitlines()
    laptop_lines = args.laptop.read_text(encoding="utf-8").splitlines() if args.laptop else None
    if args.config == "A" and laptop_lines is None:
        print("note: no --laptop log, so A's return hop, publish time and phone join are skipped\n")
    clock = logcat = None
    if args.phone:
        clock = PhoneClock.from_prefix(args.phone)
        logcat = Path(f"{args.phone}_logcat.txt").read_text(encoding="utf-8").splitlines()

    run = report(board_lines, args.config, laptop_lines, clock, logcat)
    print_summary(run, args.config)
    if args.csv:
        write_csv(run, args.csv)
        print(f"\nwrote {args.csv}")


if __name__ == "__main__":
    main()
