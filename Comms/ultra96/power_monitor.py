"""Board power during a session, split into playing (a match is running) and idle.

Runs on the Ultra96 beside the board client and the engine; run_board.py --power starts it:

    sudo -n env HOME="$HOME" XILINX_XRT=/usr ~/comms-venv/bin/python ~/comms/power_monitor.py

It reads the board's power rails (pynq.get_rails) every 0.25 s and never touches the FPGA, so the
client keeps its overlay. Each reading is labelled from what the board's broker carries:

    playing   a match is running on the board's engine (game/match phase "running"), or a glove
              gesture was classified in the last 30 s (game/gesture/+). The second covers VS CPU,
              which the phone runs by itself, so the engine never leaves "waiting"
    idle      anything else

Outputs, all in the day's log folder ~/comms/logs/<UTC date>/ (run_board copies them on Ctrl-C):

    power_<time>Z.csv      every reading: time, state, match phase, gestures so far, each rail, TOTAL
    power_summary.csv      one row per session: seconds and mean power for idle and playing
    power_monitor_<time>Z.log   a summary every 30 s, and the totals when it stops

Every 5 s it also publishes the latest readings, retained, on status/power on the board's broker,
which the dashboard shows: the mean of the last 5 s, and the session's idle and playing so far,
with the board's CPU clock, cores and PL clock as they are, and as they were before any tuning.

--cpu-mhz and --cores tune the board for the session (board_settings.py): set before the first
reading, put back when it stops, and by a guardian if it is killed. The board client sets the PL
clock itself (--pl-mhz), since loading the overlay resets it.

The rails report in 1/32 W steps a few times a second, so a single inference (under 1 ms on the
FPGA) does not show; playing vs idle shows what a running match costs as a whole.
"""

import argparse
import csv
import json
import logging
import pathlib
import signal
import threading
import time

import board_settings
import envelope
from board_logging import setup_logging

LOG_DIR = pathlib.Path.home() / "comms" / "logs"
LOG_FILE = LOG_DIR / "power_monitor.log"
# The engine's topic, retained, so the phase is known as soon as we subscribe.
# laptop/tests/test_power_monitor.py checks it equals game_engine.MATCH_TOPIC.
MATCH_TOPIC = "game/match"
PLAYING_PHASE = "running"
# The client's AI events, one per classified gesture (envelope.gesture_topic).
GESTURE_TOPICS = "game/gesture/+"
# A gesture this recent means someone is playing, even with no match on the board's engine.
GESTURE_WINDOW_S = 30.0
STATES = ("idle", "playing")
KEY_RAILS = ("PSINT_FP", "INT", "PSDDR", "3V3")  # PS cores, PL (the CNN), DDR, 3.3 V
SUMMARY_EVERY_S = 30.0
PUBLISH_EVERY_S = 5.0  # how often the dashboard's Power row updates
POWER_TOPIC = envelope.POWER_TOPIC

log = logging.getLogger("power")


def board_rails():
    """{rail name: function returning watts} for each rail the board reports power on."""
    import pynq  # the board only

    readers = {name: (lambda sensor=rail.power: float(sensor.value))
               for name, rail in pynq.get_rails().items() if rail.power is not None}
    missing = [r for r in KEY_RAILS if r not in readers]
    if missing:
        raise SystemExit(f"no power reading for {missing}; this board has {sorted(readers)}")
    return readers


def phase_of(raw):
    """The phase in a game/match message, or None if it has none."""
    try:
        payload = json.loads(raw).get("payload", {})
    except (ValueError, AttributeError):
        return None
    phase = payload.get("phase") if isinstance(payload, dict) else None
    return phase if isinstance(phase, str) else None


def state_of(phase, since_gesture=None):
    """'playing' if a board match is running or a gesture came `since_gesture` s ago, within the window."""
    recent = since_gesture is not None and since_gesture <= GESTURE_WINDOW_S
    return "playing" if phase == PLAYING_PHASE or recent else "idle"


class MatchWatcher:
    """Keeps the latest match phase and gesture count from the board's broker, on paho's thread."""

    def __init__(self, host=envelope.LOOPBACK_HOST, port=envelope.PLAINTEXT_PORT,
                 clock=time.monotonic):
        self.host, self.port = host, port
        self.clock = clock
        self.phase = None  # None until the first message: counted as idle
        self.gestures = 0
        self.last_gesture = None  # clock() of the latest one
        self._client = None

    def on_message(self, raw):
        phase = phase_of(raw)
        if phase is not None and phase != self.phase:
            log.info("match phase %s -> %s", self.phase, phase)
            self.phase = phase

    def on_gesture(self, topic, retained=False):
        if retained:  # an old one replayed on subscribe, not a gesture now
            return
        if self.gestures == 0:
            log.info("first gesture (%s): counting the next %.0f s as playing", topic, GESTURE_WINDOW_S)
        self.gestures += 1
        self.last_gesture = self.clock()

    def snapshot(self):
        """(phase, gestures so far, seconds since the latest gesture or None)."""
        last = self.last_gesture
        return self.phase, self.gestures, None if last is None else self.clock() - last

    def _route(self, msg):
        if msg.topic == MATCH_TOPIC:
            self.on_message(msg.payload)
        else:
            self.on_gesture(msg.topic, bool(msg.retain))

    def start(self):
        from paho.mqtt import client as mqtt  # comes with aiomqtt in comms-venv

        def subscribe(c, userdata, flags, reason, props):
            c.subscribe([(MATCH_TOPIC, 1), (GESTURE_TOPICS, 1)])

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ultra96-power")
        client.on_connect = subscribe
        client.on_message = lambda c, userdata, msg: self._route(msg)
        # If the monitor dies, the broker tells the dashboard instead of leaving a stale reading.
        client.will_set(POWER_TOPIC, json.dumps({"state": "offline"}), qos=1, retain=True)
        client.reconnect_delay_set(1, 10)
        client.connect_async(self.host, self.port)  # keeps retrying if the broker is not up yet
        client.loop_start()
        self._client = client
        return self

    def publish(self, payload):
        """Send the latest readings to the dashboard, retained. Dropped while the broker is away."""
        if self._client is not None:
            self._client.publish(POWER_TOPIC, json.dumps(payload), qos=0, retain=True)

    def stop(self, final=None):
        """Disconnect, first publishing `final` (the session's totals) so the dashboard keeps them."""
        if self._client is None:
            return
        if final is not None:
            sent = self._client.publish(POWER_TOPIC, json.dumps(final), qos=1, retain=True)
            try:
                sent.wait_for_publish(timeout=2)
            except (RuntimeError, ValueError):  # never connected: nothing to tell
                pass
        self._client.disconnect()
        self._client.loop_stop()


class Means:
    """Running mean of each rail and TOTAL, with the time it covers."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.sums, self.n, self.seconds = {}, 0, 0.0

    def add(self, watts, seconds):
        """One reading, standing for the `seconds` since the reading before it."""
        for name, w in watts.items():
            self.sums[name] = self.sums.get(name, 0.0) + w
        self.sums["TOTAL"] = self.sums.get("TOTAL", 0.0) + sum(watts.values())
        self.n += 1
        self.seconds += seconds

    def mean(self):
        return {name: s / self.n for name, s in self.sums.items()} if self.n else {}


def describe(mean):
    """'2.324 W (PSINT_FP 0.702, INT 0.219, PSDDR 0.062, 3V3 0.408, other 0.934)'."""
    if not mean:
        return "no readings"
    other = mean["TOTAL"] - sum(mean.get(r, 0.0) for r in KEY_RAILS)
    rails = ", ".join(f"{r} {mean.get(r, 0.0):.3f}" for r in KEY_RAILS)
    return f"{mean['TOTAL']:.3f} W ({rails}, other {other:.3f})"


def utc(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t % 1 * 1000):03d}Z"


TUNING_FIELDS = ("cpu_mhz", "cores", "pl_mhz")


def summary_row(start, end, csv_name, whole, gestures=0, tuning=None):
    """One power_summary.csv row: the board's settings, per state its time and mean power, and
    playing minus idle. `tuning` is board_settings.tuning's dict, or None if unknown."""
    row = {"start_utc": utc(start), "end_utc": utc(end), "readings_file": csv_name,
           "gestures": gestures}
    now = (tuning or {}).get("now") or {}
    for name in TUNING_FIELDS:
        row[name] = "" if now.get(name) is None else now[name]
    for state in STATES:
        mean = whole[state].mean()
        row[f"{state}_s"] = round(whole[state].seconds, 1)
        for r in (*KEY_RAILS, "TOTAL"):
            row[f"{state}_{r}"] = round(mean[r], 5) if mean else ""
    idle, playing = whole["idle"].mean(), whole["playing"].mean()
    row["playing_minus_idle_W"] = (round(playing["TOTAL"] - idle["TOTAL"], 5)
                                   if idle and playing else "")
    return row


def live_payload(state, phase, gestures, recent, whole, t=None, tuning=None):
    """What status/power carries: the last few seconds' mean, the session's so far, and the
    board's settings now and before tuning (board_settings.tuning), or None if unknown.

    Watts are rounded to the mW; the rails only step in 31 mW anyway.
    """
    def watts(mean):
        return round(mean["TOTAL"], 3) if mean else None

    now = recent.mean()
    payload = {
        "ts": round(time.time() if t is None else t, 3),
        "state": state,
        "phase": phase,
        "gestures": gestures,
        "now_W": watts(now),
        "now_rails": {r: round(now[r], 3) for r in KEY_RAILS if r in now},
        "now_s": round(recent.seconds, 1),
        "tuning": tuning,
    }
    for s in STATES:
        payload[f"{s}_W"] = watts(whole[s].mean())
        payload[f"{s}_s"] = round(whole[s].seconds, 1)
    return payload


def append_summary(path, row):
    """Add `row` to the summary. A file with other columns (an older version's) is rewritten
    with this version's, so every row lines up with the header; missing values stay empty.

    A row an earlier version of this function appended under an old header is recognised by
    its length and read with the columns it was written with.
    """
    path = pathlib.Path(path)
    fields = list(row)
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            lines = list(csv.reader(f))
        header = lines[0] if lines else []
        if header != fields:
            old = []
            for values in lines[1:]:
                names = fields if len(values) == len(fields) else header
                old.append(dict(zip(names, values)))
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(old)
    first = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if first:
            writer.writeheader()
        writer.writerow(row)


def record(rails, status, stop, interval=0.25, out_dir=None, summary_every=SUMMARY_EVERY_S,
           publish=None, publish_every=PUBLISH_EVERY_S, tuning=None):
    """Read the rails every `interval` s until `stop` is set, labelling each reading.

    `status` returns (match phase, gestures so far, seconds since the latest gesture or None),
    as MatchWatcher.snapshot does. `publish`, if given, gets live_payload's dict every
    `publish_every` s. `tuning`, if given, returns board_settings.tuning's dict; it is read
    again at each publish, so a PL clock the client sets later still shows.
    Returns (the summary row, the final payload with state "stopped").
    """
    def read_tuning():
        if tuning is None:
            return None
        try:
            return tuning()
        except Exception as exc:  # the settings are extra: never stop recording over them
            log.warning("could not read the board's settings: %s", exc)
            return None

    settings = read_tuning()
    if settings:
        log.info("board settings: %s (before tuning: %s)", settings["now"], settings["was"])
    out_dir = pathlib.Path(out_dir or LOG_DIR / time.strftime("%Y-%m-%d", time.gmtime()))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"power_{time.strftime('%H%M%S', time.gmtime())}Z.csv"
    names = list(rails)
    whole = {s: Means() for s in STATES}
    window = {s: Means() for s in STATES}
    recent = Means()  # since the last publish, whatever the state
    start = time.time()
    errors = 0
    gestures = 0
    current = None
    previous = None
    log.info("recording every %.2f s to %s", interval, path)
    with open(path, "w", newline="", encoding="utf-8") as f:
        out = csv.writer(f)
        out.writerow(["utc", "state", "match_phase", "gestures", *names, "TOTAL"])
        due = last = time.monotonic()
        next_summary = due + summary_every
        next_publish = due + publish_every
        while not stop.is_set():
            try:
                watts = {name: read() for name, read in rails.items()}
            except Exception as exc:  # one bad read must not end the recording
                errors += 1
                if errors <= 3:
                    log.warning("power read failed: %s", exc)
            else:
                now = time.monotonic()
                gap, last = (now - last if whole["idle"].n + whole["playing"].n else interval), now
                current, gestures, since = status()
                state = state_of(current, since)
                if state != previous:
                    why = ("match running" if current == PLAYING_PHASE else
                           "gesture in the last 30 s" if state == "playing" else
                           f"phase {current or 'none'}, no recent gesture")
                    log.info("now %s (%s)", state, why)
                    previous = state
                out.writerow([utc(time.time()), state, current or "none", gestures,
                              *(f"{watts[n]:.5f}" for n in names), f"{sum(watts.values()):.5f}"])
                f.flush()
                whole[state].add(watts, gap)
                window[state].add(watts, gap)
                recent.add(watts, gap)
            if publish is not None and time.monotonic() >= next_publish:
                next_publish += publish_every
                if recent.n:
                    settings = read_tuning() or settings
                    publish(live_payload(previous, current, gestures, recent, whole,
                                         tuning=settings))
                    recent.reset()
            if time.monotonic() >= next_summary:
                next_summary += summary_every
                for s in STATES:
                    if window[s].n:
                        log.info("power %s: %s over %.0f s", s, describe(window[s].mean()),
                                 window[s].seconds)
                        window[s].reset()
            due += interval
            stop.wait(max(0.0, due - time.monotonic()))
    row = summary_row(start, time.time(), path.name, whole, gestures, settings)
    append_summary(out_dir / "power_summary.csv", row)
    for s in STATES:
        log.info("total %s: %s over %.0f s", s, describe(whole[s].mean()), row[f"{s}_s"])
    if row["playing_minus_idle_W"] != "":
        log.info("playing uses %+.3f W compared with idle", row["playing_minus_idle_W"])
    log.info("%d gesture(s) classified during the session", gestures)
    log.info("readings in %s, summary row added to %s", path, out_dir / "power_summary.csv")
    return row, live_payload("stopped", current, gestures, recent, whole, tuning=settings)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--interval", type=float, default=0.25, help="seconds between readings")
    parser.add_argument("--cpu-mhz", type=int, help="CPU clock for the session, e.g. 300")
    parser.add_argument("--cores", type=int, help="CPU cores left on for the session, 1 to 4")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    log_file = setup_logging(args.verbose, LOG_FILE)
    log.info("power monitor, logging to %s", log_file)

    rails = board_rails()
    log.info("rails with power readings: %s", ", ".join(sorted(rails)))
    settings = board_settings.Settings()
    try:
        wanted = {}
        if args.cpu_mhz is not None:
            wanted["khz"] = settings.khz_for(args.cpu_mhz)
        if args.cores is not None:
            wanted["cores"] = args.cores
        settings.apply(wanted)  # puts back first whatever a killed earlier run left
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(f"board not tuned: {exc}")
    stop = threading.Event()

    def handle(signum, frame):  # pkill (SIGTERM), Ctrl-C, a dropped session
        log.info("signal %d; writing the summary", signum)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, handle)
    final = None
    try:
        watcher = MatchWatcher().start()
        try:
            _, final = record(rails, watcher.snapshot, stop, args.interval,
                              publish=watcher.publish,
                              tuning=lambda: board_settings.tuning(settings))
        finally:
            watcher.stop(final)
    finally:
        settings.restore()


if __name__ == "__main__":
    main()
