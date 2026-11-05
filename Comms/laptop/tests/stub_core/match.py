"""A stand-in for Vis's match.py, with the same interface, so the engine can be tested without it.

It accepts every action and counts ticks, unless a test tells it to refuse, raise or send NaN.
"""

import math
from dataclasses import dataclass

@dataclass(frozen=True)
class Action:
    kind: str
    name: str


@dataclass(frozen=True)
class Result:
    ok: bool
    reason: str
    action: str
    what: str
    unit_id: int | None


@dataclass(frozen=True)
class UnitView:
    id: int
    type: str
    owner: int
    along: float
    lat: float
    hp: float
    max_hp: float
    state: str
    target: int | None
    range: float
    face_along: float
    face_lat: float


@dataclass(frozen=True)
class StrikeView:
    id: int
    owner: int
    kind: str
    along: float
    lat: float
    delay: float
    from_along: float
    from_lat: float
    burn: float


@dataclass(frozen=True)
class Snapshot:
    tick: int
    time_left: float
    tower_hp: tuple[float, float]
    tower_max_hp: tuple[float, float]
    mana: tuple[float, float]
    units: tuple = ()
    strikes: tuple = ()


class Match:
    """One match. Set `refuse`, `raise_in` or `nan` on it to script a test."""

    def __init__(self, rules, seed):
        self.rules = rules
        self.seed = seed
        self.result = None
        self.lane_m = None
        self.ticks = 0
        self.positions = {}
        self.calls = []  # (method, args) of every action and God mode change, in order
        self.snapshots = 0  # snapshot() calls, ticks included
        self.refuse = ""  # a core reason code to refuse every action with
        self.raise_in = set()  # method names that raise
        self.nan = rules.get("nan", False)  # the snapshot's time_left is NaN
        self._next_unit = 1

    def _check(self, method):
        if method in self.raise_in:
            raise RuntimeError(f"stub told to raise in {method}")

    def start(self, lane_length_m):
        self._check("start")
        if self.lane_m is not None:
            raise RuntimeError("already started")
        if not math.isfinite(lane_length_m) or not 0.3 <= lane_length_m <= 10:
            raise ValueError(f"lane {lane_length_m}")
        self.lane_m = lane_length_m

    def set_position(self, player, pos):
        self._check("set_position")
        self.positions[player] = pos

    def set_god_mode(self, player, enabled):
        self._check("set_god_mode")
        self.calls.append(("set_god_mode", (player, enabled)))

    def _act(self, method, action, what, *args):
        self._check(method)
        self.calls.append((method, args))
        if self.lane_m is None or self.result is not None:
            return Result(False, "not_running", action, what, None)
        if self.refuse:
            return Result(False, self.refuse, action, what, None)
        if action != "spawn":
            return Result(True, "", action, what, None)  # only a spawn makes a unit
        unit_id, self._next_unit = self._next_unit, self._next_unit + 1
        return Result(True, "", action, what, unit_id)

    def gesture(self, player, name, aim):
        return self._act("gesture", "spawn", name.title(), player, name, aim)

    def request(self, player, action, aim):
        return self._act("request", action.kind, action.name, player, action, aim)

    def bubble(self, player, bubble_id):
        return self._act("bubble", "bubble", "", player, bubble_id)

    def tick(self, dt):
        self._check("tick")
        if self.lane_m is None:
            raise RuntimeError("tick before start")
        if self.result is None:
            self.ticks += 1
        return self.snapshot()

    def snapshot(self):
        self.snapshots += 1
        self._check("snapshot")
        time_left = math.nan if self.nan else 180 - self.ticks / 10
        return Snapshot(self.ticks, time_left, (1000.0, 1000.0), (1000.0, 1000.0), (5.0, 5.0))
