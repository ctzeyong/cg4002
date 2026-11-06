"""Run the game on the Ultra96, with the match's rules from Vis's match.py.

Session decides phases, phones, gloves and actions, with the time passed in and no I/O, so a
fake clock can test it. It takes parsed messages, so no MQTT field name appears in it.
The rest of this file runs it over MQTT, and engine_wire.py reads and writes the messages.

    ~/comms-venv/bin/python game_engine.py --core-dir ~/comms/engine

run_engine.sh runs it like this and restarts it after a crash.
"""

import argparse
import asyncio
import importlib.util
import json
import logging
import pathlib
import random
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field

import aiomqtt

import engine_wire as wire
import envelope
from board_logging import DEFAULT_LOG_FILE, setup_logging
from engine_wire import GameEvent, Lane, MatchInfo
from mqtt_publisher import GOOD_RUN_S, MAX_BACKOFF_S, MIN_BACKOFF_S
from tcp_client_mqtt import stop_on_interrupt

TICK_HZ = 10  # the phones' mockTickRateHz, so their smoothing is unchanged
TICK_S = 1 / TICK_HZ
PHONE_FRESH_S = 1.0  # a phone silent this long is gone, and the match pauses
AIM_FRESH_S = 0.5  # an older aim is where the crosshair was, not where it is
RESUME_S = 1.0  # steady this long before a pause ends or an aim counts again after a gap
COUNTDOWN_S = 3.0  # MatchCountdown.countdownSeconds on the phone
MAX_CATCHUP_TICKS = 5  # a longer stall skips ahead instead of fast-forwarding the game
PAUSE_CAP_S = 180.0  # outlasts the phone's reconnect ladder
DEDUPE_CAP = 4096  # as the phone's SeenEvents
REQ_DEDUPE_CAP = 1024
LANE_MIN_M = 0.3
LANE_MAX_M = 10.0
REASON_MAX = 80
PLAYERS = (1, 2)
RESULTS = ("player1", "player2", "draw")

WAITING, COUNTDOWN, RUNNING, PAUSED, ENDED = "waiting", "countdown", "running", "paused", "ended"

log = logging.getLogger("game_engine")


class EngineFatal(Exception):
    """A failure that would repeat on every start, so the engine exits with code 3."""


class Seen:
    """Ids seen recently, forgetting the oldest beyond `cap`."""

    def __init__(self, cap):
        self.cap = cap
        self._order = deque()
        self._ids = set()

    def add(self, key):
        """Remember `key`. Return False if it was already there."""
        if key in self._ids:
            return False
        self._ids.add(key)
        self._order.append(key)
        if len(self._order) > self.cap:
            self._ids.discard(self._order.popleft())
        return True

    def __contains__(self, key):
        return key in self._ids


@dataclass
class Stream:
    """One phone source on one player's viz/lane."""

    last: float
    since: float  # first sample after the last gap longer than AIM_FRESH_S
    sample: Lane


@dataclass
class Player:
    streams: dict = field(default_factory=dict)  # a Stream per source
    bound: str | None = None
    pressed: str | None = None  # the source whose START (or rematch, when ended) is counted
    glove_ready: bool = False
    glove_since: float = 0.0


class Session:
    """The engine's decisions for one process, one match after another."""

    def __init__(self, core, rules, encode, engine_id, now):
        """`core` is match.py's module. `encode` returns the game/state payload or raises."""
        self.core = core
        self.rules = rules
        self.encode = encode
        self.engine_id = engine_id
        self.counts = Counter()  # messages dropped, by why
        self.players = {p: Player() for p in PLAYERS}
        self._seen_events = Seen(DEDUPE_CAP)
        self._seen_requests = Seen(REQ_DEDUPE_CAP)
        self._match_count = 0
        self.match_id = ""
        self.match = None
        self.phase = WAITING
        self.reason = ""
        self.winner = "none"
        self.failed = False  # the core raised, so it is never asked again this match
        self.last_good = None  # the last game/state payload that could be sent
        self.countdown_end = 0.0
        self.last_ran = 0.0
        self.next_tick = now + TICK_S
        self._view = None
        self.version = 0  # goes up whenever match_info changes
        self._new_match(carry_presses=False)
        self.update(now)

    # --- inputs ---------------------------------------------------------------

    def on_lane(self, player, source, lane, now):
        """Take one viz/lane sample from `source`."""
        self.update(now)
        if lane.match_id != self.match_id:
            self.counts["old_lane"] += 1
            return
        p = self.players[player]
        stream = p.streams.get(source)
        if stream is None or now - stream.last > AIM_FRESH_S:
            p.streams[source] = Stream(last=now, since=now, sample=lane)
        else:
            stream.last, stream.sample = now, lane
        if source == p.bound and lane.position is not None:
            self._call_core("set_position", player, lane.position)
        self.update(now)

    def on_gloves(self, ready, now):
        """Take status/gloves as {player: True if "ready"}."""
        self.update(now)
        for player, is_ready in ready.items():
            p = self.players[player]
            if is_ready and not p.glove_ready:
                p.glove_since = now
            p.glove_ready = is_ready
        self.update(now)

    def on_mqtt_lost(self, now):
        """Forget the gloves, since only the next MQTT session's status/gloves can be trusted."""
        self.on_gloves({p: False for p in PLAYERS}, now)

    def on_request(self, player, source, request, event_id, now):
        """Take one game/request. Return the game/events it causes, if any."""
        self.update(now)
        events = []
        if self._seen_events.add(event_id) and (source, request.req_id) not in self._seen_requests:
            if request.action in ("start", "rematch"):
                self._press(player, source, request)
            elif request.action in ("god_on", "god_off"):
                self._god(player, source, request)
            else:
                events = self._tap(player, source, request)
        self.update(now)
        return events

    def on_bad_request(self, player, source, req_id, action, event_id, now):
        """Refuse a card tap that failed its checks. Return its game/event, none for a repeat."""
        self.update(now)
        if not (self._seen_events.add(event_id) and self._seen_requests.add((source, req_id))):
            return []
        return [GameEvent(player, "tap", False, "bad_request", action=action, req_id=req_id)]

    def on_gesture(self, player, name, event_id, now):
        """Take one glove gesture from game/gesture. Return its game/event."""
        self.update(now)
        events = []
        if self._seen_events.add(event_id):
            event = GameEvent(player, "glove", ok=False, gesture_event_id=event_id, gesture=name)
            stream = self._aim_stream(player, now)
            if self.phase != RUNNING:
                events = [_refused(event, "not_running")]
            elif stream is None:
                events = [_refused(event, "phone_not_connected")]
            else:
                events = [self._apply(event, "gesture", player, name, stream.sample.aim)]
        self.update(now)
        return events

    def on_bubble(self, player, source, match_id, bubble_id, event_id, now):
        """Take one viz/bubble_captured. Return its game/event."""
        self.update(now)
        events = []
        if self._seen_events.add(event_id):
            event = GameEvent(player, "bubble", ok=False, action="bubble")
            refusal = self._refusal(player, source, match_id)
            if refusal:
                events = [_refused(event, refusal)]
            else:
                events = [self._apply(event, "bubble", player, bubble_id)]
        self.update(now)
        return events

    def on_tick(self, now):
        """Advance one tick if one is due and the match runs, and return the game/state payload.

        Call it at `next_tick`, in every phase, so the deadline never falls behind in a pause.
        """
        self.update(now)
        if now - self.next_tick > MAX_CATCHUP_TICKS * TICK_S:
            self.next_tick = now  # stalled, so skip ahead rather than fast-forward
        due = now >= self.next_tick
        if due:
            self.next_tick += TICK_S
        if not self.failed:
            # asyncio can wake a little early, and a call that isn't due only repeats the state
            stepping = due and self.phase == RUNNING
            snapshot = self._call_core("tick", TICK_S) if stepping else self._call_core("snapshot")
            if snapshot is not _FAILED:
                try:
                    self.last_good = self.encode(snapshot, self.match_id)
                except Exception as exc:
                    self._fail(f"its snapshot can't be sent: {exc!r}")
        if self.phase == RUNNING and self.match.result is not None:
            if self.match.result in RESULTS:
                self._end(self.match.result, "")
            else:
                self._fail(f"its result {self.match.result!r} isn't a player or a draw")
        self._refresh(now)
        return self.last_good

    # --- what the phones are told ------------------------------------------------

    def match_info(self):
        return self._view

    def countdown_ms(self, now):
        if self.phase != COUNTDOWN:
            return 0
        return max(0, round((self.countdown_end - now) * 1000))

    # --- phases ---------------------------------------------------------------

    def update(self, now):
        """Apply every change of phase that is due at `now`, before any message is used."""
        for p in self.players.values():
            self._move_binding(p, now)

        # first, so a new match is gated in this same call
        if self.phase == ENDED and all(self._counts_press(p, now) for p in self.players.values()):
            self._new_match(carry_presses=True)

        if self.phase == WAITING:
            failure = self._gate(now)
            if failure is None:
                for p in self.players.values():
                    p.bound = self._current_source(p, now)
                self.phase, self.reason = COUNTDOWN, ""
                self.countdown_end = now + COUNTDOWN_S
                log.info("match %s: countdown", self.match_id)
            else:
                self.reason = failure
        elif self.phase == COUNTDOWN:
            failure = self._gate(now)
            if failure is None and now >= self.countdown_end:
                self._start(now)
            elif failure is not None:
                self._back_to_waiting(failure)
        elif self.phase == RUNNING:
            missing = self._missing(now)
            if missing:
                self.phase, self.reason, self.last_ran = PAUSED, missing, now
                log.info("match %s: paused, %s", self.match_id, missing)
        elif self.phase == PAUSED:
            if now - self.last_ran >= PAUSE_CAP_S:
                missing = self._missing(now).replace("not connected", "gone")
                self._end("none", missing or "paused too long")
            elif self._resumable(now):
                self.phase, self.reason = RUNNING, ""
                log.info("match %s: resumed", self.match_id)
            else:
                self.reason = self._missing(now) or "resuming"
        self._refresh(now)

    def _gate(self, now):
        """Return why the match can't count down now, or None if it can."""
        for player, p in self.players.items():
            fresh = [s for s in p.streams if self._fresh(p.streams[s], now)]
            if p.bound is None and len(fresh) > 1:
                return f"two phones on player {player}"
            if self._current_source(p, now) is None or (p.bound and p.bound not in fresh):
                return f"waiting for player {player}'s phone"
            if not p.glove_ready:
                return f"player {player} glove not ready"
            if not self._counts_press(p, now):
                return f"waiting for player {player} to press START"
        if self._lane_m(now) is None:
            return "player 1 lane not measured"  # here too, or a bad lane would loop the countdown
        return None

    def _lane_m(self, now):
        """Player 1's current lane length if it's usable, else None. It sets the arena's scale."""
        p = self.players[1]
        stream = p.streams.get(self._current_source(p, now))
        lane_m = stream.sample.lane_m if stream else None
        return lane_m if lane_m is not None and LANE_MIN_M <= lane_m <= LANE_MAX_M else None

    def _start(self, now):
        lane_m = self._lane_m(now)  # the gate has just checked it
        if self._call_core("start", lane_m) is _FAILED:
            return
        self.phase, self.reason = RUNNING, ""
        log.info("match %s: running, lane %.2f m", self.match_id, lane_m)

    def _back_to_waiting(self, reason):
        for p in self.players.values():
            p.bound = None  # bound again at the next countdown
        self.phase, self.reason = WAITING, reason
        log.info("match %s: waiting, %s", self.match_id, reason)

    def _end(self, winner, reason):
        for p in self.players.values():
            p.pressed = None  # this match's STARTs aren't rematches
        self.phase, self.winner, self.reason = ENDED, winner, reason
        log.info("match %s: ended, winner %s %s", self.match_id, winner, reason)

    def _fail(self, why):
        log.error("match %s: engine error, %s", self.match_id, why)
        self.failed = True
        self._end("none", "engine error")

    def _new_match(self, carry_presses):
        """Start a new match in waiting. Raise EngineFatal if its opening state can't be sent."""
        number = self._match_count + 1
        match_id = f"{self.engine_id}-{number}"
        try:
            match = self.core.Match(self.rules, random.randrange(2**31))
            opening = self.encode(match.snapshot(), match_id)
        except Exception as exc:
            raise EngineFatal(f"match {match_id} can't open: {exc}") from exc
        self._match_count, self.match_id, self.match = number, match_id, match
        self.last_good, self.failed, self.winner = opening, False, "none"
        self.phase, self.reason = WAITING, ""
        for p in self.players.values():
            p.bound = None
            # Forget the last match's samples: a phone counts once it sends one for this match.
            p.streams = {}
            if not carry_presses:
                p.pressed = None
        log.info("match %s: waiting", match_id)

    # --- players -----------------------------------------------------------------

    @staticmethod
    def _fresh(stream, now):
        return now - stream.last <= PHONE_FRESH_S

    def _current_source(self, p, now):
        """The bound source, or before binding the one fresh source, or None."""
        if p.bound is not None:
            return p.bound
        fresh = [s for s, stream in p.streams.items() if self._fresh(stream, now)]
        return fresh[0] if len(fresh) == 1 else None

    def _there(self, p, now):
        """The bound source is fresh, or before binding there is exactly one fresh source."""
        if p.bound is not None:
            return self._fresh(p.streams[p.bound], now)
        return self._current_source(p, now) is not None

    def _counts_press(self, p, now):
        return p.pressed is not None and p.pressed == self._current_source(p, now)

    def _move_binding(self, p, now):
        """Rebind to another phone when the currently bound phone goes quiet.

        Only switch when exactly one other phone is actively sending on the player's
        topic. This handles an app being closed and reopened on another phone.

        Never switch during a running match. The old phone must first pause the match,
        and the new phone must send steadily for RESUME_S before resuming it.
        """
        if p.bound is None or self.phase == RUNNING or self._fresh(p.streams[p.bound], now):
            return
        others = [s for s, stream in p.streams.items() if s != p.bound and self._fresh(stream, now)]
        if len(others) == 1:
            log.info("match %s: %s replaces %s", self.match_id, others[0], p.bound)
            p.bound = others[0]

    def _missing(self, now):
        """Why the match can't run now, or "" if it can."""
        for player, p in self.players.items():
            if p.bound is None or not self._fresh(p.streams[p.bound], now):
                return f"player {player} phone not connected"
            if not p.glove_ready:
                return f"player {player} glove down"
        return ""

    def _resumable(self, now):
        for p in self.players.values():
            stream = p.streams.get(p.bound)
            if stream is None or now - stream.last > AIM_FRESH_S or now - stream.since < RESUME_S:
                return False
            if not p.glove_ready or now - p.glove_since < RESUME_S:
                return False
        return True

    def _aim_stream(self, player, now):
        """The bound stream if its aim can be used for a gesture now, else None."""
        p = self.players[player]
        stream = p.streams.get(p.bound)
        if stream is None or now - stream.last > AIM_FRESH_S or now - stream.since < RESUME_S:
            return None
        return stream

    # --- actions -----------------------------------------------------------------

    def _press(self, player, source, request):
        """Count a START or rematch, or drop it if it doesn't apply now."""
        p = self.players[player]
        # a relaunched app has no PLAY AGAIN button, so its START counts as a rematch
        applies = (request.action == "start" and self.phase in (WAITING, COUNTDOWN, ENDED)) or (
            request.action == "rematch" and self.phase == ENDED
        )
        if request.match_id != self.match_id or (p.bound and source != p.bound) or not applies:
            self.counts["press_dropped"] += 1
            return
        self._seen_requests.add((source, request.req_id))
        p.pressed = source

    def _god(self, player, source, request):
        """Debug: the demo's God mode for the player who asked (full mana and tower HP, free actions).

        It has no game/event, so a phone never waits on one. A request for another match, or from a source that isn't
        the one bound to this player, is dropped. A new match starts without it, so a phone sends it again.
        """
        self._seen_requests.add((source, request.req_id))
        bound = self.players[player].bound
        if request.match_id != self.match_id or (bound and source != bound):
            self.counts["god_refused"] += 1
            return
        on = request.action == "god_on"
        if self._call_core("set_god_mode", player, on) is not _FAILED:
            log.info("match %s: God mode %s for player %d", self.match_id, "on" if on else "off", player)

    def _tap(self, player, source, request):
        self._seen_requests.add((source, request.req_id))
        event = GameEvent(
            player, "tap", ok=False, action=request.action, what=request.name, req_id=request.req_id
        )
        refusal = self._refusal(player, source, request.match_id)
        if refusal:
            return [_refused(event, refusal)]
        try:
            action = self.core.Action(request.action, request.name)
        except Exception as exc:
            self._fail(f"Action raised {exc!r}")
            return [_refused(event, "engine_error")]
        return [self._apply(event, "request", player, action, request.aim)]

    def _refusal(self, player, source, match_id):
        """The shell's reason to refuse a tap or bubble without asking the core, or ""."""
        bound = self.players[player].bound
        if match_id != self.match_id:
            return "old_match"
        if bound and source != bound:
            return "wrong_source"
        if self.phase != RUNNING:
            return "not_running"
        return ""

    def _apply(self, event, method, *args):
        """Ask the core, and turn its Result into the event."""
        result = self._call_core(method, *args)
        if result is not _FAILED and not _good_result(result):
            self._fail(f"{method} returned {result!r}")
            result = _FAILED
        if result is _FAILED:
            return _refused(event, "engine_error")
        return GameEvent(
            event.player, event.input, ok=result.ok, reason=result.reason, action=result.action,
            what=result.what, req_id=event.req_id, gesture_event_id=event.gesture_event_id,
            gesture=event.gesture, unit_id=-1 if result.unit_id is None else result.unit_id,
        )

    def _call_core(self, method, *args):
        """Call the core. Any exception ends the match, since it may have changed half its state."""
        if self.failed:
            return _FAILED
        try:
            return getattr(self.match, method)(*args)
        except Exception as exc:
            self._fail(f"{method} raised {exc!r}")
            return _FAILED

    def _refresh(self, now):
        view = MatchInfo(
            match_id=self.match_id,
            phase=self.phase,
            reason=self.reason[:REASON_MAX],
            phones=tuple(self._there(p, now) for p in self.players.values()),
            gloves=tuple(p.glove_ready for p in self.players.values()),
            starts=tuple(self._counts_press(p, now) for p in self.players.values()),
            winner=self.winner,
            rules_version=str(self.rules.get("version", "")),
        )
        if view != self._view:
            self._view = view
            self.version += 1


_FAILED = object()  # what _call_core returns when the core raised


def _good_result(result):
    """Whether the core's Result holds only values game/event allows."""
    try:
        ok, reason, action, what = result.ok, result.reason, result.action, result.what
        unit_id = result.unit_id
    except AttributeError:
        return False
    if ok is True and action == "spawn":  # only a spawn makes a unit
        good_id = type(unit_id) is int and 0 <= unit_id <= wire.ID_MAX
    else:
        good_id = unit_id is None
    good_reason = reason == "" if ok is True else reason in wire.CORE_REASONS
    # a string first, since a list can't be looked up in a dict
    names = wire.EVENT_NAMES.get(action, ()) if type(action) is str else ()
    return type(ok) is bool and good_id and good_reason and what in names


def _refused(event, reason):
    return GameEvent(
        event.player, event.input, ok=False, reason=reason, action=event.action, what=event.what,
        req_id=event.req_id, gesture_event_id=event.gesture_event_id, gesture=event.gesture,
    )


# --- loading the core ------------------------------------------------------------


def load_core(core_dir):
    """Return Vis's match.py module and the rules from rules.json, both in `core_dir`.

    Raises EngineFatal if either can't be used, since that would fail on every start.
    """
    core_dir = pathlib.Path(core_dir)
    rules_path = core_dir / "rules.json"
    try:
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EngineFatal(f"can't read {rules_path}: {exc}") from exc
    if not isinstance(rules, dict):
        raise EngineFatal(f"{rules_path} is not a JSON object")
    version = rules.get("version")
    if type(version) is not str or not version:
        raise EngineFatal(f"{rules_path} has no version, which game/match reports")
    unfilled = _unfilled(rules)
    if unfilled:
        raise EngineFatal(f"{rules_path} has no value yet for {unfilled}")
    try:
        spec = importlib.util.spec_from_file_location("match", core_dir / "match.py")
        core = importlib.util.module_from_spec(spec)
        sys.modules["match"] = core  # dataclasses look their module up here
        spec.loader.exec_module(core)
    except Exception as exc:
        raise EngineFatal(f"can't load {core_dir / 'match.py'}: {exc!r}") from exc
    return core, rules


def _unfilled(value, path=""):
    """Return where the first null in the rules is, or "". Keys starting with _ are notes."""
    if value is None:
        return path
    if isinstance(value, dict):
        items = [(k, v) for k, v in value.items() if not k.startswith("_")]
    elif isinstance(value, list):
        items = list(enumerate(value))
    else:
        items = []
    for key, item in items:
        found = _unfilled(item, f"{path}.{key}" if path else str(key))
        if found:
            return found
    return ""


def _bubble_count(rules):
    section = rules.get("BubbleCaptureManager")
    count = section.get("bubbleCount") if isinstance(section, dict) else None
    if type(count) is not int or count < 1:
        raise EngineFatal(f"rules.json's BubbleCaptureManager.bubbleCount is {count!r}")
    return count


# --- MQTT --------------------------------------------------------------------------

CLIENT_ID = "ultra96-engine"  # also the envelopes' source, so its event_ids never collide
KEEPALIVE_S = 15
QOS = 1  # every topic but game/state, which is QoS 0
EVENTS_MAXSIZE = 256
EVENT_TTL_S = 2.0  # the phone stops waiting for a tap's result after this
OFFLINE_TIMEOUT_S = 2.0  # how long a stop waits to mark the engine offline
EXIT_FATAL = 3  # tells run_engine.sh not to restart
STOPPED = "game engine stopped"
ENGINE_ERROR = "engine error"

MATCH_TOPIC = "game/match"
STATE_TOPIC = "game/state"
EVENT_TOPIC = "game/event"
INPUTS = {  # topic without its player, and its name in the counts
    "viz/lane": "lane",
    "game/request": "request",
    "viz/bubble_captured": "bubble",
    "game/gesture": "gesture",
}
SUBSCRIPTIONS = [f"{topic}/+" for topic in INPUTS] + [envelope.GLOVES_TOPIC]
PLAYER_OF = {"1": 1, "2": 2}

CLOCK_STEP_S = time.get_clock_info("monotonic").resolution
LOG_FILE = DEFAULT_LOG_FILE.with_name("game_engine.log")


class Engine:
    """The Session plus what its MQTT messages need. It lasts across reconnects."""

    def __init__(self, session):
        self.session = session
        self.bubble_count = _bubble_count(session.rules)
        self.changed = asyncio.Event()  # set after anything that may change game/match
        # game/events wait here across reconnects, each in the envelope it is retried in
        self.events = asyncio.Queue(maxsize=EVENTS_MAXSIZE)
        self.pending = []  # the event being published, until the broker acknowledges it
        self._seq = 0
        self._warned = set()

    def wrap(self, payload):
        """Return `payload` as JSON in a new envelope, so the phones never drop a repeat."""
        env = envelope.build_envelope(payload, self._seq, source=CLIENT_ID)
        self._seq += 1
        return json.dumps(env, allow_nan=False)

    def match_message(self, now):
        info = self.session.match_info()
        return self.wrap(wire.match_payload(info, self.session.countdown_ms(now), TICK_HZ))

    def queue_event(self, event, now):
        """Queue a game/event to publish. A full queue throws away the oldest."""
        message = self.wrap(wire.event_payload(event, self.session.match_id))
        if self.events.full():
            self.events.get_nowait()
            self.session.counts["event_dropped"] += 1
        self.events.put_nowait((message, event, now))

    def offline_message(self, reason):
        rules_version = self.session.match_info().rules_version
        return self.wrap(wire.offline_payload(reason, TICK_HZ, rules_version))

    def handle(self, topic, raw, now):
        """Apply one inbound message and return the game/events it causes.

        A message that fails a check is dropped and counted. Only the first of each kind is logged.
        """
        session = self.session
        kind, _, player_text = topic.rpartition("/")
        if topic == envelope.GLOVES_TOPIC:
            name, player = "gloves", None
        else:
            name, player = INPUTS.get(kind), PLAYER_OF.get(player_text)
            if name is None or player is None:
                session.counts["no_player"] += 1  # game/gesture/0, which is nobody's
                return []
        try:
            event_id, source, payload = wire.read_envelope(raw)
            if name == "gloves":
                session.on_gloves(wire.read_gloves(payload), now)
            elif name == "lane":
                session.on_lane(player, source, wire.read_lane(payload), now)
            elif name == "request":
                return self._request(topic, player, source, payload, event_id, now)
            elif name == "bubble":
                match_id, bubble_id = wire.read_bubble(payload, self.bubble_count)
                return session.on_bubble(player, source, match_id, bubble_id, event_id, now)
            else:
                name = wire.read_gesture(payload, player)
                return session.on_gesture(player, name, event_id, now)
        except wire.BadMessage as exc:
            self._drop(topic, name, exc)
        return []

    def _request(self, topic, player, source, payload, event_id, now):
        try:
            request = wire.read_request(payload)
        except wire.BadMessage as exc:
            self._drop(topic, "request", exc)
            tap = wire.refusable_tap(payload)
            if tap is None:
                return []
            return self.session.on_bad_request(player, source, *tap, event_id, now)
        return self.session.on_request(player, source, request, event_id, now)

    def _drop(self, topic, name, exc):
        self.session.counts[f"bad_{name}"] += 1
        if (name, exc.field) not in self._warned:
            self._warned.add((name, exc.field))
            log.warning("dropping a bad message on %s: %s (more are only counted)", topic, exc)


def describe(event):
    """Describe a game/event in one log line."""
    outcome = f"ok, unit {event.unit_id}" if event.ok else f"refused {event.reason}"
    gesture = f" for {event.gesture_event_id}" if event.gesture_event_id else ""
    return f"player {event.player} {event.input} {event.action} {event.what!r}: {outcome}{gesture}"


async def _receive(client, engine):
    """Apply each inbound message and queue its game/events. Ends when the connection drops."""
    async for message in client.messages:
        now = time.monotonic()
        for event in engine.handle(message.topic.value, message.payload, now):
            engine.queue_event(event, now)
        engine.changed.set()


async def _tick(client, engine, ended):
    """Publish one game/state per tick, in every phase."""
    session = engine.session
    while not ended.is_set():
        now = time.monotonic()
        if now < session.next_tick:
            # asyncio can wake up to one clock step early, so never wait less than a step
            await asyncio.sleep(max(session.next_tick - now, CLOCK_STEP_S))
            continue
        state = session.on_tick(now)
        engine.changed.set()
        await client.publish(STATE_TOPIC, engine.wrap(state), qos=0)


async def _publish_match(client, engine, ended, sent):
    """Publish game/match, retained, whenever it changes. Only the latest is sent."""
    session = engine.session
    while not ended.is_set():
        engine.changed.clear()
        if session.version == sent:
            try:
                await asyncio.wait_for(engine.changed.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            continue
        sent = session.version
        message = engine.match_message(time.monotonic())
        await client.publish(MATCH_TOPIC, message, qos=QOS, retain=True)


async def _publish_events(client, engine, ended):
    """Publish each game/event in order, on its own so game/state never waits for an ack.

    An event not yet acknowledged when the link drops is sent again after the reconnect, in
    the same envelope, unless it is older than EVENT_TTL_S.
    """
    pending = engine.pending
    while not ended.is_set():
        if not pending:
            try:
                pending.append(await asyncio.wait_for(engine.events.get(), timeout=1.0))
            except asyncio.TimeoutError:
                pass
            continue  # check ended again, since wait_for can swallow a cancel
        message, event, queued_at = pending[0]
        if time.monotonic() - queued_at > EVENT_TTL_S:
            engine.session.counts["event_stale"] += 1
            log.warning("discarding a stale event %s", describe(event))
            pending.clear()
            continue
        await client.publish(EVENT_TOPIC, message, qos=QOS)  # on a lost link it stays pending
        pending.clear()
        log.info("event %s", describe(event))


async def _publish_offline(client, engine, reason):
    """Mark the engine offline before a clean disconnect, which would suppress the will."""
    try:
        message = engine.offline_message(reason)
        await client.publish(MATCH_TOPIC, message, qos=QOS, retain=True, timeout=OFFLINE_TIMEOUT_S)
        log.info("game/match: offline, %s", reason)
    except aiomqtt.MqttError as exc:
        log.warning("could not mark the engine offline: %s", exc)


async def _serve(client, engine, stop):
    """Run the engine on one connection until `stop` is set or a task fails."""
    session = engine.session
    session.on_mqtt_lost(time.monotonic())  # only this session's status/gloves counts
    sent = session.version
    # first, since the will or a broker restart may have replaced it
    await client.publish(MATCH_TOPIC, engine.match_message(time.monotonic()), qos=QOS, retain=True)
    for topic in SUBSCRIPTIONS:
        await client.subscribe(topic, qos=QOS)

    ended = asyncio.Event()  # set before cancelling, so a task that swallows the cancel still stops
    loops = [
        asyncio.create_task(_tick(client, engine, ended)),
        asyncio.create_task(_publish_match(client, engine, ended, sent)),
        asyncio.create_task(_publish_events(client, engine, ended)),
    ]
    watchers = [
        asyncio.create_task(stop.wait()),
        asyncio.create_task(_receive(client, engine)),
    ]
    try:
        await asyncio.wait([*loops, *watchers], return_when=asyncio.FIRST_COMPLETED)
    finally:
        ended.set()  # before cancelling, as everywhere
        engine.changed.set()  # wake the match loop now
        # publishers too, since a dropped link leaves a publish waiting 10 s for its ack
        for task in [*loops, *watchers]:
            task.cancel()
        await asyncio.gather(*loops, *watchers, return_exceptions=True)
    for task in [*loops, *watchers]:
        if not task.cancelled():
            task.result()  # re-raise a failure


async def one_session(host, port, engine, stop):
    """Connect to the broker once and run the engine until `stop` is set or the link fails."""
    will = aiomqtt.Will(MATCH_TOPIC, engine.offline_message(STOPPED), qos=QOS, retain=True)
    async with aiomqtt.Client(
        hostname=host, port=port, identifier=CLIENT_ID, keepalive=KEEPALIVE_S, will=will
    ) as client:
        log.info("connected to mqtt://%s:%d as %s", host, port, CLIENT_ID)
        try:
            await _serve(client, engine, stop)
        except aiomqtt.MqttError:
            raise  # the link is gone, so the will says offline
        except Exception:
            await _publish_offline(client, engine, ENGINE_ERROR)
            raise
        await _publish_offline(client, engine, STOPPED)


async def run(host, port, engine, stop):
    """Keep the engine connected, and reconnect with backoff until `stop` is set.

    Raises EngineFatal for a failure that would repeat on every start.
    """
    backoff = MIN_BACKOFF_S
    while not stop.is_set():
        started = time.monotonic()
        try:
            await one_session(host, port, engine, stop)
        except aiomqtt.MqttError as exc:
            log.warning("mqtt session failed: %s", exc)
        if stop.is_set():
            break

        elapsed = time.monotonic() - started
        if elapsed >= GOOD_RUN_S:
            backoff = MIN_BACKOFF_S
        log.info("reconnecting to the broker in %.0fs", backoff)
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
        if elapsed < GOOD_RUN_S:
            backoff = min(backoff * 2, MAX_BACKOFF_S)


async def serve(host, port, core_dir):
    """Load the core and run the engine until Ctrl-C. Nothing connects if the core can't load."""
    core, rules = load_core(core_dir)
    session = Session(core, rules, wire.state_payload, envelope.SESSION_ID, time.monotonic())
    engine = Engine(session)
    log.info("engine %s, rules %s", envelope.SESSION_ID, rules.get("version"))
    stop = asyncio.Event()
    restore_interrupt = stop_on_interrupt(stop)
    try:
        await run(host, port, engine, stop)
    finally:
        restore_interrupt()
        log.info("engine stopped, dropped: %s", dict(session.counts) or "none")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=envelope.LOOPBACK_HOST, help="this board's broker")
    parser.add_argument("--port", type=int, default=envelope.PLAINTEXT_PORT)
    parser.add_argument(
        "--core-dir",
        default=pathlib.Path(__file__).resolve().parent / "engine",
        help="folder with match.py and rules.json (default %(default)s)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    log_file = setup_logging(args.verbose, LOG_FILE)
    log.info("logging to %s", log_file)
    if sys.platform == "win32":
        # aiomqtt needs the selector loop, which Windows doesn't use by default
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(serve(args.host, args.port, args.core_dir))
    except EngineFatal as exc:
        log.error("not restarting, since this fails on every start: %s", exc)
        sys.exit(EXIT_FATAL)
    except KeyboardInterrupt:
        log.info("interrupted")
    except Exception:
        log.exception("engine crashed")
        sys.exit(1)


if __name__ == "__main__":
    main()
