"""The Bridge Rush match core: a port of the C# MatchState rules, for the Ultra96 game engine.

One module, loaded by game_engine.load_core from the folder that holds this file and rules.json.
No clock, no sockets and no randomness beyond random.Random(seed). The interface is spec 3.1:

    Match(rules, seed); start(L); set_position(player, (along, lat)); gesture(player, name, aim);
    request(player, Action(kind, name), aim); bubble(player, id); tick(dt); snapshot(); result

Frame: one lane frame for both players, as the phones' canonical frame. Player 1's tower is at the
origin, Player 2's at (L, 0). Positions and aims arrive and leave in lane units (along / L,
lateral / (laneWidthFraction * L)); inside, everything is metres on the floor. Heights only matter
for drawing, except the tower approach, where the units' height above the tower base is
groundYOffsetMeters (MatchState.Combat.cs 3D distances).

Every public call checks first and changes state after, so an exception never leaves a half-applied
action.
"""

import logging
import math
import random
from dataclasses import dataclass

log = logging.getLogger("match")

REASONS = ("not_running", "no_mana", "bad_aim", "tower_out_of_arena", "unknown_gesture", "cooldown")
UNIT_TYPES = ("Warrior", "Rogue", "Mage", "Tank", "Hero")
SPELLS = ("lightning", "fireball")

LANE_MIN_M = 0.3
LANE_MAX_M = 10.0
DT_MAX = 0.5
ID_MAX = 1_000_000
EPS = 0.0001  # the C# bounds checks' tolerance


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


# --- Unity's maths, so the numbers match the C# ----------------------------------------------


def _clamp01(v):
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def _lerp(a, b, t):
    return a + (b - a) * _clamp01(t)


def _inverse_lerp(a, b, v):
    return _clamp01((v - a) / (b - a)) if a != b else 0.0


def _smooth_step(a, b, t):
    t = _clamp01(t)
    t = -2.0 * t * t * t + 3.0 * t * t
    return b * t + a * (1.0 - t)


def _mag(v):
    return math.hypot(v[0], v[1])


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1])


def _mul(v, s):
    return (v[0] * s, v[1] * s)


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _normalized(v):
    m = _mag(v)
    return (v[0] / m, v[1] / m) if m > 1e-5 else (0.0, 0.0)


def _clamp_magnitude(v, limit):
    m = _mag(v)
    return _mul(v, limit / m) if m > limit and m > 0.0 else v


def _smooth_damp(current, target, velocity, smooth_time, max_speed, dt):
    """Vector3.SmoothDamp in two dimensions. Returns (position, velocity)."""
    smooth_time = max(0.0001, smooth_time)
    omega = 2.0 / smooth_time
    x = omega * dt
    decay = 1.0 / (1.0 + x + 0.48 * x * x + 0.235 * x * x * x)
    change = _clamp_magnitude(_sub(current, target), max_speed * smooth_time)
    clamped_target = _sub(current, change)
    temp = _mul(_add(velocity, _mul(change, omega)), dt)
    velocity = _mul(_sub(velocity, _mul(temp, omega)), decay)
    output = _add(clamped_target, _mul(_add(change, temp), decay))
    to_target = _sub(target, current)
    past = _sub(output, target)
    if to_target[0] * past[0] + to_target[1] * past[1] > 0.0:
        output = target
        velocity = (0.0, 0.0)
    return output, velocity


def _tidy(seconds):
    """Round away float dust, so ten steps of 0.1 s add up to exactly 1 s and a timer reaches 0."""
    return round(seconds, 9)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# --- the match ------------------------------------------------------------------------------


class _Unit:
    def __init__(self, uid, kind, owner, max_hp, dps, pos, attack_range):
        self.id = uid
        self.type = kind
        self.owner = owner
        self.pos = pos
        self.lateral_velocity = (0.0, 0.0)
        self.facing = (1.0, 0.0) if owner == 1 else (-1.0, 0.0)
        self.max_hp = max_hp
        self.hp = max_hp
        self.dps = dps
        self.attacking = False
        self.target = None
        self.past_bridge = False
        self.bridge = -1
        self.chase = None
        self.range = attack_range

    @property
    def ranged(self):
        return self.range > 0.0


class _Strike:
    def __init__(self, sid, caster, center, remaining, fireball=False, start=(0.0, 0.0), duration=0.0, burn=0.0):
        self.id = sid
        self.caster = caster
        self.center = center
        self.remaining = remaining
        self.age = 0.0
        self.applied = False
        self.fireball = fireball
        self.start = start
        self.duration = duration
        self.burn_left = burn


class Match:
    def __init__(self, rules, seed):
        m = rules["MatchState"]
        self.rules = rules
        self.seed = seed
        self._rng = random.Random(seed)  # the rules have no chance in them yet
        self._m = m
        self._spell = rules["SpellSpec"]
        self._stats = rules["UnitStatsTable"]
        self._bubble = rules["BubbleCaptureManager"]
        self._tower_radius_ref = float(rules["TowerController"]["TowerRadius"])
        # the own tower is drawn this many radii in front of the reported position; out of the arena is judged there
        self._tower_offset_radii = float(rules["TowerController"].get("ownTowerForwardOffsetInRadii", 0.0))
        self._gestures = {str(k).lower(): tuple(v) for k, v in rules["gestures"].items()}

        self.result = None
        self.lane_m = None
        self.tick_count = 0
        self._clock = 0.0
        self._time_left = float(m["matchDurationSeconds"])
        self._tower_hp = [float(m["ownTowerMaxHP"]), float(m["opponentTowerMaxHP"])]
        self._tower_max = (float(m["ownTowerMaxHP"]), float(m["opponentTowerMaxHP"]))
        self._mana = [float(m["manaStart"]), float(m["manaStart"])]
        self._god = {1: False, 2: False}
        self._lane_pos = {}  # player -> (along, lat) as reported
        self._outside_for = {1: 0.0, 2: 0.0}  # seconds each player has been continuously outside the arena
        self._units = []
        self._strikes = []
        self._next_unit = 1
        self._next_strike = 0
        self._bridge_toggle = False
        self._last_bubble = {1: None, 2: None}
        self._bridges = [0.0]

    # --- the frame -------------------------------------------------------------------------

    def _setup_frame(self, lane_m):
        m = self._m
        self.lane_m = float(lane_m)
        self.width_m = m["laneWidthFraction"] * self.lane_m
        territory = (1.0 - m["bridgeFraction"]) * 0.5
        self._own_edge = territory * self.lane_m
        self._opp_edge = self.lane_m - territory * self.lane_m
        self._bridge_x = (self._own_edge + self._opp_edge) * 0.5
        if m["twoBridges"]:
            offset = self.width_m * max(0.0, m["bridgeLateralOffsetFraction"])
            self._bridges = [-offset, offset]
        else:
            self._bridges = [0.0]
        ref = m["referenceLaneMeters"]
        if ref < 0.0001 or self.lane_m < 0.0001:
            self.scale = 1.0
        else:
            scale = (self.lane_m / ref) ** _clamp01(m["arenaScaleExponent"])
            low, high = sorted((m["arenaScaleMin"], m["arenaScaleMax"]))
            self.scale = min(max(scale, low), high)
        self.tower_radius = self._tower_radius_ref * self.scale
        self.island_half = self.width_m * 0.5 * max(0.0, m["territoryWidthMultiplier"])
        self.bridge_width = self.width_m * m["laneSpineFraction"]
        self.rear = self.lane_m * max(0.0, m["rearMarginFraction"])

    def _home(self, player):
        return (0.0, 0.0) if player == 1 else (self.lane_m, 0.0)

    def _tower_pos(self, player):
        """The player's live tower, or its home before the first position."""
        reported = self._lane_pos.get(player)
        if reported is None:
            return self._home(player)
        return (reported[0] * self.lane_m, reported[1] * self.width_m)

    def _body_pos(self, player):
        """Where the player's tower is drawn: a little in front of its reported position, towards the other player.

        Being out of the arena is judged here, so a tower has to be more than half out (its centre past the edge) to count,
        however far behind it the player's device is. The phone draws it the same way (TowerController).
        """
        x, y = self._tower_pos(player)
        offset = self.tower_radius * self._tower_offset_radii
        return (x + offset, y) if player == 1 else (x - offset, y)

    def _funnel_blend(self, x):
        m = self._m
        own_len = self._own_edge
        opp_len = self.lane_m - self._opp_edge
        bridge_start = own_len
        bridge_end = self.lane_m - opp_len
        funnel = _clamp01(m["funnelFraction"])
        if x <= bridge_start:
            funnel_start = bridge_start * (1.0 - funnel)
            if x <= funnel_start or bridge_start <= funnel_start:
                return 0.0
            return _smooth_step(0.0, 1.0, _inverse_lerp(funnel_start, bridge_start, x))
        if x >= bridge_end:
            funnel_end = bridge_end + opp_len * funnel
            if x >= funnel_end or funnel_end <= bridge_end:
                return 0.0
            return 1.0 - _smooth_step(0.0, 1.0, _inverse_lerp(bridge_end, funnel_end, x))
        return 1.0

    def _bridge_interval(self, index, blend):
        island = self.island_half
        bridge_half = self.bridge_width * 0.5
        widest = island + abs(self._bridges[index])
        half = _lerp(widest, bridge_half, blend)
        low = max(self._bridges[index] - half, -island)
        high = min(self._bridges[index] + half, island)
        return (low, high) if low <= high else None

    def _clamp_lateral(self, x, lateral, preferred=-1):
        blend = self._funnel_blend(x)
        best, best_distance = lateral, float("inf")
        for i in range(len(self._bridges)):
            if 0 <= preferred < len(self._bridges) and i != preferred:
                continue
            interval = self._bridge_interval(i, blend)
            if interval is None:
                continue
            clamped = min(max(lateral, interval[0]), interval[1])
            distance = abs(clamped - lateral)
            if distance < best_distance:
                best, best_distance = clamped, distance
        return best

    def _inside_arena(self, pos):
        x, y = pos
        if x < -self.rear - EPS or x > self.lane_m + self.rear + EPS:
            return False
        return abs(self._clamp_lateral(x, y) - y) <= EPS

    def _inside_territory(self, player, pos):
        x, y = pos
        along = x if player == 1 else self.lane_m - x
        if along < -self.rear - EPS or along > self._own_edge + EPS:
            return False
        return abs(y) <= self.island_half + EPS

    def _inside_spell_area(self, pos):
        x, y = pos
        if x < -self.rear or x > self.lane_m + self.rear:
            return False
        return abs(y) <= self.island_half

    def _clamp_to_arena(self, pos, look_ahead=0.0, preferred=-1):
        x = min(max(pos[0], -self.rear), self.lane_m + self.rear)
        return (x, self._clamp_lateral(x + look_ahead, pos[1], preferred))

    def _choose_bridge(self, pos):
        if len(self._bridges) <= 1:
            return 0
        lateral = pos[1]
        best, best_distance = 0, abs(lateral - self._bridges[0])
        for i in range(1, len(self._bridges)):
            d = abs(lateral - self._bridges[i])
            if d < best_distance:
                best, best_distance = i, d
        if len(self._bridges) == 2 and abs(abs(lateral - self._bridges[0]) - abs(lateral - self._bridges[1])) < 0.01:
            self._bridge_toggle = not self._bridge_toggle
            return 0 if self._bridge_toggle else 1
        return best

    def _on_bridge_span(self, pos):
        span = self._opp_edge - self._own_edge
        along = pos[0] - self._own_edge
        return 0.0 <= along <= span

    # --- public interface --------------------------------------------------------------------

    def start(self, lane_length_m):
        if self.lane_m is not None:
            raise RuntimeError("the match has already started")
        if not _number(lane_length_m) or not LANE_MIN_M <= lane_length_m <= LANE_MAX_M:
            raise ValueError(f"lane length {lane_length_m!r} is not between {LANE_MIN_M} and {LANE_MAX_M}")
        self._setup_frame(lane_length_m)

    def set_position(self, player, position):
        self._check_player(player)
        if not (isinstance(position, (tuple, list)) and len(position) == 2 and all(_number(v) for v in position)):
            raise ValueError(f"position {position!r} is not two finite numbers")
        self._lane_pos[player] = (float(position[0]), float(position[1]))

    def set_god_mode(self, player, enabled):
        """Debug: the player's mana and tower HP stay full and their actions are free."""
        self._check_player(player)
        self._god[player] = bool(enabled)
        if self._god[player] and self.lane_m is not None:
            self._apply_god()

    def gesture(self, player, name, aim):
        self._check_player(player)
        resolved = self._gestures.get(str(name).lower())
        if resolved is None:
            return self._refuse("not_running" if not self._running() else "unknown_gesture", "none", "")
        return self._act(player, resolved[0], resolved[1], aim)

    def request(self, player, action, aim):
        self._check_player(player)
        kind, name = action.kind, action.name
        valid = (kind == "spawn" and name in UNIT_TYPES) or (kind == "spell" and name in SPELLS)
        if not valid:
            return self._refuse("not_running" if not self._running() else "unknown_gesture", "none", "")
        return self._act(player, kind, name, aim)

    def bubble(self, player, bubble_id):
        self._check_player(player)
        if not self._running():
            return self._refuse("not_running", "bubble", "")
        last = self._last_bubble[player]
        cooldown = float(self._bubble["respawnCooldownSeconds"])
        if last is not None and self._clock - last < cooldown - 1e-9:
            return self._refuse("cooldown", "bubble", "")
        amount = float(self._bubble["reward"]["Amount"])
        self._last_bubble[player] = self._clock
        self._mana[player - 1] = min(float(self._m["manaMax"]), self._mana[player - 1] + amount)
        return Result(True, "", "bubble", "", None)

    def tick(self, dt):
        if self.lane_m is None:
            raise RuntimeError("tick before start")
        if not _number(dt) or not 0.0 < dt <= DT_MAX:
            raise ValueError(f"dt {dt!r} is not in (0, {DT_MAX}]")
        if self.result is not None:
            return self.snapshot()
        m = self._m
        self._apply_god()
        regen = m["manaRegenPerSecond"] * self._regen_multiplier() * dt
        for i in range(2):
            self._mana[i] = min(float(m["manaMax"]), self._mana[i] + regen)
        self._time_left = _tidy(max(0.0, self._time_left - dt))
        self._out_of_bounds(dt)
        self._update_strikes(dt)
        self._simulate(dt)
        self._apply_god()
        self._clock = _tidy(self._clock + dt)
        self.tick_count += 1
        self._check_end()
        return self.snapshot()

    def snapshot(self):
        if self.lane_m is None:
            return Snapshot(0, float(self._m["matchDurationSeconds"]), tuple(self._tower_hp), self._tower_max,
                            tuple(self._mana))
        ids = {u.id for u in self._units}
        units = tuple(
            UnitView(
                id=u.id, type=u.type, owner=u.owner,
                along=u.pos[0] / self.lane_m, lat=u.pos[1] / self.width_m,
                hp=max(0.0, u.hp), max_hp=u.max_hp,
                state="Attacking" if u.attacking else "Walking",
                target=u.target if u.target in ids else None,
                range=u.range, face_along=u.facing[0], face_lat=u.facing[1],
            )
            for u in self._units
        )
        strikes = tuple(self._strike_view(s) for s in self._strikes)
        return Snapshot(
            tick=self.tick_count, time_left=self._time_left,
            tower_hp=(max(0.0, self._tower_hp[0]), max(0.0, self._tower_hp[1])),
            tower_max_hp=self._tower_max, mana=tuple(self._mana), units=units, strikes=strikes,
        )

    # --- actions -----------------------------------------------------------------------------

    @staticmethod
    def _check_player(player):
        if type(player) is not int or player not in (1, 2):
            raise ValueError(f"player {player!r} is not 1 or 2")

    def _running(self):
        return self.lane_m is not None and self.result is None

    @staticmethod
    def _refuse(reason, action, what):
        return Result(False, reason, action, what, None)

    def _aim_point(self, aim):
        if not (isinstance(aim, (tuple, list)) and len(aim) == 2 and all(_number(v) for v in aim)):
            return None
        return (aim[0] * self.lane_m, aim[1] * self.width_m)

    def _act(self, player, kind, name, aim):
        if not self._running():
            return self._refuse("not_running", kind, name)
        point = self._aim_point(aim)
        if kind == "spawn":
            return self._spawn(player, name, point)
        return self._cast(player, name, point)

    def _cost(self, kind, name):
        if kind == "spawn":
            return float(self._stats[name]["ManaCost"])
        return float(self._spell["LightningManaCost" if name == "lightning" else "FireballManaCost"])

    def _can_pay(self, player, cost):
        return self._god[player] or self._mana[player - 1] >= cost

    def _pay(self, player, cost):
        if not self._god[player]:
            self._mana[player - 1] -= cost

    def _spawn(self, player, name, point):
        if point is None or not self._inside_territory(player, point):
            return self._refuse("bad_aim", "spawn", name)
        if not self._inside_arena(self._body_pos(player)):
            return self._refuse("tower_out_of_arena", "spawn", name)
        cost = self._cost("spawn", name)
        if not self._can_pay(player, cost):
            return self._refuse("no_mana", "spawn", name)
        if self._next_unit > ID_MAX:
            raise RuntimeError("unit ids ran out")
        entry = self._stats[name]
        attack_range = max(0.0, float(entry.get("AttackRangeMeters", 0.0))) * self.scale
        unit = _Unit(self._next_unit, name, player, float(entry["MaxHP"]), float(entry["AttackDps"]),
                     point, attack_range)
        self._next_unit += 1
        self._pay(player, cost)
        self._units.append(unit)
        return Result(True, "", "spawn", name, unit.id)

    def _cast(self, player, name, point):
        if point is None or not self._inside_spell_area(point):
            return self._refuse("bad_aim", "spell", name)
        cost = self._cost("spell", name)
        if not self._can_pay(player, cost):
            return self._refuse("no_mana", "spell", name)
        self._pay(player, cost)
        if name == "lightning":
            self._strikes.append(_Strike(self._next_strike, player, point,
                                         float(self._spell["LightningTelegraphSeconds"])))
        else:
            start = self._tower_pos(player)
            speed = max(0.01, float(self._spell["FireballSpeedMetersPerSecond"]) * self.scale)
            flight = max(float(self._spell["FireballMinFlightSeconds"]), _dist(start, point) / speed)
            self._strikes.append(_Strike(self._next_strike, player, point, flight, True, start, flight,
                                         float(self._spell["FireballBurnSeconds"])))
        self._next_strike += 1
        return Result(True, "", "spell", name, None)

    # --- the clock ---------------------------------------------------------------------------

    def _regen_multiplier(self):
        m = self._m
        active = 0.0 < self._time_left <= m["doubleManaLastSeconds"]
        return max(1.0, float(m["doubleManaMultiplier"])) if active else 1.0

    def _apply_god(self):
        for player in (1, 2):
            if self._god[player]:
                self._mana[player - 1] = float(self._m["manaMax"])
                self._tower_hp[player - 1] = self._tower_max[player - 1]

    def _out_of_bounds(self, dt):
        # A player outside the arena loses HP only after outOfBoundsGraceSeconds of continuous time outside, so a
        # sway of the phone or a tracking jump does not cost HP. Coming back inside starts the count again.
        grace = float(self._m.get("outOfBoundsGraceSeconds", 0.0))
        for player in (1, 2):
            pos = self._body_pos(player)
            if self._inside_arena(pos):
                if self._outside_for[player] > grace:
                    log.info("player %d is back inside the arena after %.1f s", player, self._outside_for[player])
                self._outside_for[player] = 0.0
                continue
            before = self._outside_for[player]
            after = _tidy(before + dt)
            self._outside_for[player] = after
            if after > grace:
                if before <= grace:
                    # logged only once the grace runs out, so a player swaying on the edge does not flood the log
                    log.warning(
                        "player %d has been outside the arena for %.1f s, tower HP now draining: device reported %s, "
                        "tower at %.2f m along and %.2f m across, lane %.2f m, rear margin %.2f m",
                        player, after, self._lane_pos.get(player), pos[0], pos[1], self.lane_m, self.rear)
                self._damage_tower(player, self._m["outOfBoundsDamagePerSecond"] * dt)

    def _damage_tower(self, player, amount):
        self._tower_hp[player - 1] = max(0.0, self._tower_hp[player - 1] - amount)

    def _check_end(self):
        own_down = self._tower_hp[0] <= 0.0
        opp_down = self._tower_hp[1] <= 0.0
        time_up = self._time_left <= 0.0
        if not (own_down or opp_down or time_up):
            return
        if own_down and opp_down:
            self.result = "draw"
        elif opp_down:
            self.result = "player1"
        elif own_down:
            self.result = "player2"
        elif self._tower_hp[0] > self._tower_hp[1]:
            self.result = "player1"
        elif self._tower_hp[1] > self._tower_hp[0]:
            self.result = "player2"
        else:
            self.result = "draw"

    # --- spells ------------------------------------------------------------------------------

    def _update_strikes(self, dt):
        sp = self._spell
        for strike in list(self._strikes):
            strike.remaining = _tidy(strike.remaining - dt)
            strike.age = _tidy(strike.age + dt)
            if not strike.applied and strike.remaining <= 0.0:
                strike.applied = True
                if strike.fireball:
                    self._apply_area(strike.caster, strike.center,
                                     float(sp["FireballRadiusMeters"]) * self.scale, float(sp["FireballUnitDamage"]),
                                     float(sp["FireballTowerDamage"]), float(sp["FireballEdgeDamageFraction"]))
                else:
                    self._apply_area(strike.caster, strike.center,
                                     float(sp["LightningRadiusMeters"]) * self.scale, float(sp["LightningUnitDamage"]),
                                     float(sp["LightningTowerDamage"]), float(sp["LightningEdgeDamageFraction"]))
            if strike.fireball and strike.applied and strike.burn_left > 0.0:
                self._apply_burn(strike.caster, strike.center, min(dt, strike.burn_left))
                strike.burn_left = _tidy(strike.burn_left - dt)
            burn = float(sp["FireballBurnSeconds"])
            lifetime = strike.duration + burn if strike.fireball else float(sp["LightningTelegraphSeconds"])
            if strike.applied and strike.age > lifetime + 0.75:
                self._strikes.remove(strike)

    def _apply_area(self, caster, center, scaled_radius, unit_damage, tower_damage, edge_fraction):
        radius = max(0.0001, scaled_radius)
        target = 2 if caster == 1 else 1
        for unit in self._units:
            if unit.owner != target or unit.hp <= 0.0:
                continue
            distance = _dist(unit.pos, center)
            if distance > radius:
                continue
            unit.hp -= unit_damage * _lerp(1.0, _clamp01(edge_fraction), distance / radius)
        if _dist(self._tower_pos(target), center) <= radius + self.tower_radius:
            self._damage_tower(target, tower_damage)

    def _apply_burn(self, caster, center, seconds):
        radius = max(0.0001, float(self._spell["FireballRadiusMeters"]) * self.scale)
        target = 2 if caster == 1 else 1
        for unit in self._units:
            if unit.owner != target or unit.hp <= 0.0:
                continue
            if _dist(unit.pos, center) <= radius:
                unit.hp -= float(self._spell["FireballBurnDps"]) * seconds

    def _strike_view(self, s):
        kind = "fireball" if s.fireball else "lightning"
        from_a = from_l = burn = 0.0
        if s.fireball:
            from_a, from_l = s.start[0] / self.lane_m, s.start[1] / self.width_m
            burn = max(0.0, s.burn_left) if s.applied else float(self._spell["FireballBurnSeconds"])
        return StrikeView(
            id=s.id, owner=s.caster, kind=kind, along=s.center[0] / self.lane_m, lat=s.center[1] / self.width_m,
            delay=max(0.0, s.remaining), from_along=from_a, from_lat=from_l, burn=burn,
        )

    # --- combat ------------------------------------------------------------------------------

    def _hit_reach(self, unit):
        return unit.range if unit.ranged else self._m["hitReachMeters"] * self.scale

    def _notice(self, unit):
        base = self._m["noticeRangeMeters"] * self.scale
        return max(base, unit.range) if unit.ranged else base

    def _standoff(self, unit):
        clearance = self._m["towerClearanceMeters"] * self.scale
        return self.tower_radius + (unit.range if unit.ranged else clearance)

    def _hysteresis(self):
        return self._m["towerDisengageHysteresisMeters"] * self.scale

    def _blocked_by_water(self, unit, other, distance):
        if len(self._bridges) <= 1 or other.bridge == unit.bridge:
            return False
        if not self._on_bridge_span(unit.pos) and not self._on_bridge_span(other.pos):
            return False
        return not (unit.ranged and distance <= unit.range)

    def _find(self, uid):
        for u in self._units:
            if u.id == uid:
                return u
        return None

    def _resolve_enemy(self, unit):
        notice = self._notice(unit)
        if unit.chase is not None:
            current = self._find(unit.chase)
            if current is not None and current.hp > 0.0 and current.owner != unit.owner:
                distance = _dist(unit.pos, current.pos)
                if distance <= notice + self._hysteresis() and not self._blocked_by_water(unit, current, distance):
                    return current
            unit.chase = None
        nearest, nearest_distance = None, notice
        for other in self._units:
            if other.owner == unit.owner or other.hp <= 0.0:
                continue
            distance = _dist(unit.pos, other.pos)
            if distance > nearest_distance or self._blocked_by_water(unit, other, distance):
                continue
            nearest, nearest_distance = other, distance
        return nearest

    def _height(self):
        return float(self._m["groundYOffsetMeters"])

    def _dist_to_tower(self, pos, tower):
        return math.hypot(_dist(pos, tower), self._height())

    def _simulate(self, dt):
        m = self._m
        for unit in self._units:
            if unit.bridge < 0:
                unit.bridge = self._choose_bridge(unit.pos)

        for unit in self._units:
            if unit.attacking:
                continue
            enemy = self._resolve_enemy(unit)
            if enemy is not None:
                if _dist(unit.pos, enemy.pos) <= self._hit_reach(unit):
                    unit.attacking = True
                    unit.target = enemy.id
                    unit.chase = None
                    continue
                unit.chase = enemy.id
            else:
                unit.chase = None

            seconds = float(self._stats.get(unit.type, {}).get("SecondsToCrossLane", 10.0))
            speed = (self.lane_m / seconds) * m["globalSpeedMultiplier"] if seconds > 0.0001 else 0.0
            target_tower = self._tower_pos(2 if unit.owner == 1 else 1)
            forward = (1.0, 0.0) if unit.owner == 1 else (-1.0, 0.0)
            bridge = min(max(unit.bridge, 0), len(self._bridges) - 1)
            chase = self._find(unit.chase) if unit.chase is not None else None
            if chase is not None:
                to_chase = _sub(chase.pos, unit.pos)
                direction = _normalized(to_chase) if _mag(to_chase) ** 2 > 0.0001 else (0.0, 0.0)
                step = min(speed * dt, max(0.0, _mag(to_chase) - self._hit_reach(unit) * 0.9))
                new = self._clamp_to_arena(_add(unit.pos, _mul(direction, step)), 0.0,
                                           -1 if unit.past_bridge else bridge)
            elif not unit.past_bridge:
                look = m["lookAheadMeters"] * self.scale
                look_ahead = look if unit.owner == 1 else -look
                spine = (unit.pos[0], self._bridges[bridge])
                to_bridge = _dist(unit.pos, (self._bridge_x, self._bridges[bridge]))
                influence_radius = max(0.0001, m["centeringInfluenceRadiusFraction"] * self.lane_m)
                influence = _clamp01(1.0 - to_bridge / influence_radius)
                centering_target = (unit.pos[0] + (spine[0] - unit.pos[0]) * influence,
                                    unit.pos[1] + (spine[1] - unit.pos[1]) * influence)
                centered, unit.lateral_velocity = _smooth_damp(
                    unit.pos, centering_target, unit.lateral_velocity, m["laneCenteringSmoothTime"], speed, dt)
                combined = _add(_sub(centered, unit.pos), _mul(forward, speed * dt))
                combined = _clamp_magnitude(combined, speed * dt)
                new = self._clamp_to_arena(_add(unit.pos, combined), look_ahead, bridge)
            else:
                to_target = _sub(target_tower, unit.pos)
                length3 = math.hypot(_mag(to_target), self._height())
                if length3 ** 2 > 0.0001:
                    direction = (to_target[0] / length3, to_target[1] / length3)
                else:
                    direction = (0.0, 0.0)
                new = self._clamp_to_arena(_add(unit.pos, _mul(direction, speed * dt)))

            if not unit.past_bridge:
                exit_edge = self._opp_edge if unit.owner == 1 else self._own_edge
                if (new[0] - exit_edge) * forward[0] >= 0.0:
                    unit.past_bridge = True

            previous = unit.pos
            unit.pos = new
            travel = _sub(unit.pos, previous)
            if _mag(travel) ** 2 > 0.000001:
                unit.facing = _normalized(travel)
            if self._dist_to_tower(unit.pos, target_tower) <= self._standoff(unit):
                unit.attacking = True
                unit.target = None
                unit.chase = None

        for unit in self._units:
            if unit.hp <= 0.0 or not unit.attacking:
                continue
            if unit.target is None:
                target_player = 2 if unit.owner == 1 else 1
                tower = self._tower_pos(target_player)
                to_tower = _sub(tower, unit.pos)
                if _mag(to_tower) ** 2 > 0.0001:
                    length3 = math.hypot(_mag(to_tower), self._height())
                    unit.facing = (to_tower[0] / length3, to_tower[1] / length3)
                if self._dist_to_tower(unit.pos, tower) > self._standoff(unit) + self._hysteresis():
                    unit.attacking = False
                    continue
                self._damage_tower(target_player, unit.dps * dt)
                unit.hp -= m["towerCounterDps"] * dt
                continue

            target = self._find(unit.target)
            if target is None or target.hp <= 0.0:
                unit.attacking = False
                unit.target = None
                continue
            to_target = _sub(target.pos, unit.pos)
            if _mag(to_target) ** 2 > 0.0001:
                unit.facing = _normalized(to_target)
            if _mag(to_target) > self._hit_reach(unit) + self._hysteresis():
                unit.attacking = False
                unit.target = None
                unit.chase = target.id
                continue
            target.hp -= unit.dps * dt

        self._units = [u for u in self._units if u.hp > 0.0]
