"""Run the game on the Ultra96, with the match's rules from Vis's match.py.

Session decides phases, phones, gloves and actions, with the time passed in and no I/O, so a
fake clock can test it. It takes parsed messages, so no MQTT field name appears in it.
"""

import logging
import random
from collections import Counter, deque
from dataclasses import dataclass, field

TICK_HZ = 10  # the phones' mockTickRateHz, so their smoothing is unchanged
TICK_S = 1 / TICK_HZ
PHONE_FRESH_S = 1.0  # a phone silent this long is gone, and the match pauses
AIM_FRESH_S = 0.5  # an older aim is where the crosshair was, not where it is
RESUME_S = 1.0  # everything good this long before a pause ends, so a flapping link doesn't strobe
COUNTDOWN_S = 3.0  # MatchCountdown.countdownSeconds on the phone
MAX_CATCHUP_TICKS = 5  # a longer stall skips ahead instead of fast-forwarding the game
PAUSE_CAP_S = 180.0  # outlasts the phone's reconnect ladder
DEDUPE_CAP = 4096  # as the phone's SeenEvents
REQ_DEDUPE_CAP = 1024
LANE_MIN_M = 0.3
LANE_MAX_M = 10.0
REASON_MAX = 80
PLAYERS = (1, 2)

WAITING, COUNTDOWN, RUNNING, PAUSED, ENDED = "waiting", "countdown", "running", "paused", "ended"

log = logging.getLogger("game_engine")


class EngineFatal(Exception):
    """A failure that would repeat on every start, so the engine exits with code 3."""


@dataclass(frozen=True)
class Lane:
    """One viz/lane sample. None where the phone has no lane, position or aim."""

    match_id: str
    lane_m: float | None
    position: tuple[float, float] | None
    aim: tuple[float, float] | None


@dataclass(frozen=True)
class Request:
    """One game/request. `name` is the unit or spell, "" for start and rematch."""

    match_id: str
    req_id: int
    action: str
    name: str
    aim: tuple[float, float] | None


@dataclass(frozen=True)
class GameEvent:
    """The result of one action, for game/event."""

    player: int
    input: str
    ok: bool
    reason: str = ""
    action: str = "none"
    what: str = ""
    req_id: int = -1
    gesture_event_id: str = ""
    gesture: str = ""
    unit_id: int = -1


@dataclass(frozen=True)
class MatchInfo:
    """What game/match says, without countdown_ms, which changes every tick."""

    match_id: str
    phase: str
    reason: str
    phones: tuple[bool, bool]
    gloves: tuple[bool, bool]
    starts: tuple[bool, bool]
    winner: str
    rules_version: str


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
        """`core` is match.py's module and `encode(snapshot, match_id)` returns bytes or raises."""
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
        self.last_good = b""
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
            else:
                events = self._tap(player, source, request)
        self.update(now)
        return events

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
        """Advance one tick if one is due and the match runs, and return the game/state bytes.

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
                except (TypeError, ValueError) as exc:
                    self._fail(f"its snapshot can't be sent: {exc}")
        if self.phase == RUNNING and self.match.result is not None:
            self._end(self.match.result, "")
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
        applies = (request.action == "start" and self.phase in (WAITING, COUNTDOWN)) or (
            request.action == "rematch" and self.phase == ENDED
        )
        if request.match_id != self.match_id or (p.bound and source != p.bound) or not applies:
            self.counts["press_dropped"] += 1
            return
        self._seen_requests.add((source, request.req_id))
        p.pressed = source

    def _tap(self, player, source, request):
        self._seen_requests.add((source, request.req_id))
        event = GameEvent(
            player, "tap", ok=False, action=request.action, what=request.name, req_id=request.req_id
        )
        refusal = self._refusal(player, source, request.match_id)
        if refusal:
            return [_refused(event, refusal)]
        action = self.core.Action(request.action, request.name)
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
            rules_version=str(getattr(self.core, "RULES_VERSION", "")),
        )
        if view != self._view:
            self._view = view
            self.version += 1


_FAILED = object()  # what _call_core returns when the core raised


def _refused(event, reason):
    return GameEvent(
        event.player, event.input, ok=False, reason=reason, action=event.action, what=event.what,
        req_id=event.req_id, gesture_event_id=event.gesture_event_id, gesture=event.gesture,
    )
