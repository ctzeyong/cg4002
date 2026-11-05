"""The engine's Session on a stub core and a fake clock, for phases, phones, gloves and actions."""

import itertools
import json
import types
from pathlib import Path

import pytest

import ai_events  # noqa: E402 - ultra96/, on sys.path via conftest
import game_engine as ge  # noqa: E402
from engine_wire import Lane, Request, state_payload
from stub_core import match as stub

T0 = 1000.0
ids = itertools.count()


def encode(snapshot, match_id):
    """As the wire does, but only the fields the tests read, and NaN refused."""
    fields = {"match_id": match_id, "tick": snapshot.tick, "time_left": snapshot.time_left}
    json.dumps(fields, allow_nan=False)
    return fields


class Game:
    """A Session on a fake clock, with both phones streaming unless a test stops one."""

    def __init__(self, rules=None):
        self.now = T0
        self.s = ge.Session(stub, rules if rules is not None else {}, encode, "e1", self.now)
        self.sources = {1: "phone-a", 2: "phone-b"}  # set one to None to make that phone quiet
        self.lane_m = 2.0
        self.position = {1: None, 2: None}
        self.aim = {1: None, 2: None}
        self.next_lane = self.now
        self.states = []

    @property
    def match(self):
        return self.s.match

    def lane(self, player):
        return Lane(self.s.match_id, self.lane_m, self.position[player], self.aim[player])

    def run(self, seconds):
        """Let time pass in 10 ms steps, sending lanes at 10 Hz and ticking when due."""
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.now = round(self.now + 0.01, 6)
            if self.now >= self.next_lane - 1e-9:
                self.next_lane = round(self.next_lane + 0.1, 6)
                for player, source in self.sources.items():
                    if source:
                        self.s.on_lane(player, source, self.lane(player), self.now)
            if self.now >= self.s.next_tick:
                self.states.append(self.s.on_tick(self.now))

    def gloves(self, one=True, two=True):
        self.s.on_gloves({1: one, 2: two}, self.now)

    def press(self, player, action="start", source=None, req_id=None, match_id=None):
        request = Request(match_id or self.s.match_id, next(ids) if req_id is None else req_id,
                          action, "", None)
        return self.s.on_request(player, source or self.sources[player], request,
                                 f"ev-{next(ids)}", self.now)

    def tap(self, player, name="Warrior", source=None, req_id=None, match_id=None,
            aim=(0.25, 0.0), event_id=None):
        request = Request(match_id or self.s.match_id, next(ids) if req_id is None else req_id,
                          "spawn", name, aim)
        return self.s.on_request(player, source or self.sources[player], request,
                                 event_id or f"ev-{next(ids)}", self.now)

    def gesture(self, player, name="warrior", event_id=None):
        return self.s.on_gesture(player, name, event_id or f"g-{next(ids)}", self.now)

    @property
    def info(self):
        return self.s.match_info()


def running_game(rules=None):
    """Both phones streaming, both gloves ready, both STARTs in, countdown done."""
    g = Game(rules)
    g.run(0.2)
    g.gloves()
    g.press(1)
    g.press(2)
    g.run(ge.COUNTDOWN_S + 0.05)
    assert g.s.phase == ge.RUNNING
    g.position = {1: (0.0, 0.0), 2: (1.0, 0.0)}
    g.aim = {1: (0.25, 0.0), 2: (0.75, 0.0)}
    g.run(1.0)
    return g


# --- the gate and the countdown ------------------------------------------------


def test_the_gate_says_what_it_waits_for_in_order():
    g = Game()
    assert g.info.reason == "waiting for player 1's phone"
    g.run(0.2)
    assert g.info.reason == "player 1 glove not ready"
    g.gloves()
    assert g.info.reason == "waiting for player 1 to press START"
    g.press(1)
    assert g.info.reason == "waiting for player 2 to press START"
    assert g.info.starts == (True, False)
    g.press(2)
    assert g.info.phase == ge.COUNTDOWN


def test_the_countdown_lasts_countdown_s_then_starts_with_player_1s_lane():
    g = Game()
    g.lane_m = 2.1
    g.run(0.2)
    g.gloves()
    g.press(1)
    g.press(2)
    started = g.now
    g.run(ge.COUNTDOWN_S - 0.02)
    assert g.s.phase == ge.COUNTDOWN
    assert g.s.countdown_ms(g.now) == 20
    g.run(0.03)
    assert g.s.phase == ge.RUNNING
    assert g.match.lane_m == 2.1
    assert g.now - started < ge.COUNTDOWN_S + 0.02


def test_a_phone_lost_in_the_countdown_goes_back_to_waiting_and_comes_back_by_itself():
    g = Game()
    g.run(0.2)
    g.gloves()
    g.press(1)
    g.press(2)
    g.run(0.5)
    g.sources[2] = None
    g.run(ge.PHONE_FRESH_S + 0.05)
    assert (g.s.phase, g.info.reason) == (ge.WAITING, "waiting for player 2's phone")
    assert all(p.bound is None for p in g.s.players.values())
    g.sources[2] = "phone-b"
    g.run(0.1)
    assert g.s.phase == ge.COUNTDOWN  # its START was kept


@pytest.mark.parametrize("lane_m", [None, 0.2, 10.5])
def test_without_a_usable_lane_it_waits_rather_than_counting_down(lane_m):
    g = Game()
    g.lane_m = lane_m
    g.run(0.2)
    g.gloves()
    g.press(1)
    g.press(2)
    g.run(ge.COUNTDOWN_S + 1.0)
    assert (g.s.phase, g.info.reason) == (ge.WAITING, "player 1 lane not measured")
    g.lane_m = 2.0
    g.run(0.1)
    assert g.s.phase == ge.COUNTDOWN


def test_a_lane_that_goes_bad_in_the_countdown_goes_back_to_waiting():
    g = Game()
    g.run(0.2)
    g.gloves()
    g.press(1)
    g.press(2)
    g.run(1.0)
    g.lane_m = None
    g.run(0.1)
    assert (g.s.phase, g.info.reason) == (ge.WAITING, "player 1 lane not measured")
    assert g.match.lane_m is None


def test_two_phones_on_one_player_are_refused_at_the_gate():
    g = Game()
    g.run(0.2)
    g.gloves()
    g.s.on_lane(2, "phone-c", g.lane(2), g.now)
    g.press(1)
    g.press(2)
    assert (g.s.phase, g.info.reason) == (ge.WAITING, "two phones on player 2")
    assert g.info.phones == (True, False)


# --- START: who counts ---------------------------------------------------------


def test_a_start_from_a_source_that_has_gone_shows_false_and_the_new_phone_opens_the_gate():
    """A relaunched app must see its flag false, or it would never press again."""
    g = Game()
    g.run(0.2)
    g.gloves()
    g.press(1)
    g.press(2)
    g.sources[2] = None  # player 2's app dies before the countdown ends
    g.run(ge.PHONE_FRESH_S + 0.05)
    g.sources[2] = "phone-c"  # and comes back as a new source
    g.run(0.2)
    assert g.s.phase == ge.WAITING
    assert g.info.starts == (True, False)
    g.press(2)
    assert g.s.phase == ge.COUNTDOWN


def test_a_start_from_another_source_during_the_countdown_is_dropped():
    g = Game()
    g.run(0.2)
    g.gloves()
    g.press(1)
    g.press(2)
    assert g.s.phase == ge.COUNTDOWN
    g.press(2, source="phone-c")
    assert g.s.counts["press_dropped"] == 1
    assert g.s.players[2].pressed == "phone-b"


# --- running, pausing, resuming --------------------------------------------------


def test_a_quiet_phone_pauses_the_match_after_phone_fresh_s():
    g = running_game()
    g.sources[2] = None
    g.run(ge.PHONE_FRESH_S - 0.05)
    assert g.s.phase == ge.RUNNING
    g.run(0.1)
    assert (g.s.phase, g.info.reason) == (ge.PAUSED, "player 2 phone not connected")
    assert g.info.phones == (True, False)


def test_a_request_just_after_the_phone_went_quiet_finds_the_match_paused_without_a_tick():
    """The phase is worked out at each message, so no tick has to come first (R90)."""
    g = running_game()
    g.sources[2] = None
    g.run(0.05)  # player 2's last sample is now ~0.05 s old
    g.now += ge.PHONE_FRESH_S + 0.01  # and no tick or lane in between
    assert g.tap(1)[0].reason == "not_running"
    assert g.s.phase == ge.PAUSED


def test_resuming_needs_resume_s_of_stream_and_does_not_fast_forward():
    g = running_game()
    g.sources[2] = None
    g.run(3.0)
    assert g.s.phase == ge.PAUSED
    ticks = g.match.ticks
    g.sources[2] = "phone-b"
    g.run(ge.RESUME_S - 0.05)
    assert g.s.phase == ge.PAUSED
    assert g.info.reason == "resuming"
    g.run(0.1)
    assert g.s.phase == ge.RUNNING
    assert g.match.ticks == ticks  # nothing moved while paused
    g.run(1.0)
    assert ticks + 9 <= g.match.ticks <= ticks + 10  # one per tick, not the ~40 skipped


def test_a_glove_down_pauses_and_resuming_needs_the_glove_ready_for_resume_s():
    g = running_game()
    g.gloves(two=False)
    assert (g.s.phase, g.info.reason) == (ge.PAUSED, "player 2 glove down")
    g.gloves()
    g.run(ge.RESUME_S - 0.05)
    assert g.s.phase == ge.PAUSED
    g.run(0.1)
    assert g.s.phase == ge.RUNNING


def test_a_pause_longer_than_the_cap_ends_the_match_with_no_winner_even_if_the_link_flaps():
    g = running_game()
    g.sources[2] = None
    g.run(2.0)
    paused_at = g.s.last_ran
    for _ in range(int(ge.PAUSE_CAP_S / 0.6)):  # back for one sample every 0.6 s, never resumable
        g.s.on_lane(2, "phone-b", g.lane(2), g.now)
        g.run(0.6)
        if g.s.phase == ge.ENDED:
            break
    assert g.s.phase == ge.ENDED
    assert g.info.winner == "none"
    assert ge.PAUSE_CAP_S <= g.now - paused_at < ge.PAUSE_CAP_S + 0.7
    assert g.info.reason in ("player 2 phone gone", "paused too long")


def test_the_mqtt_session_dropping_forgets_the_gloves_and_the_next_status_sets_them():
    g = running_game()
    g.s.on_mqtt_lost(g.now)
    assert g.info.gloves == (False, False)
    assert g.s.phase == ge.PAUSED
    g.gloves()
    assert g.info.gloves == (True, True)


# --- the tick ----------------------------------------------------------------------


def test_a_stall_catches_up_at_most_max_catchup_ticks():
    g = running_game()
    ticks = g.match.ticks
    g.now += 0.8  # the board stalled for 8 ticks, short of pausing the match
    while g.now >= g.s.next_tick:
        g.s.on_tick(g.now)
    assert g.match.ticks == ticks + 1


def test_a_short_stall_is_caught_up():
    g = running_game()
    ticks = g.match.ticks
    g.now = g.s.next_tick + 0.35  # three ticks late, within MAX_CATCHUP_TICKS
    while g.now >= g.s.next_tick:
        g.s.on_tick(g.now)
    assert g.match.ticks == ticks + 4


def test_a_call_a_little_before_the_deadline_only_repeats_the_state():
    """asyncio can run a timer up to one clock resolution early (15.6 ms on Windows)."""
    g = running_game()
    ticks, deadline = g.match.ticks, g.s.next_tick
    g.s.on_tick(deadline - 0.01)
    assert (g.match.ticks, g.s.next_tick) == (ticks, deadline)
    g.s.on_tick(deadline)
    assert g.match.ticks == ticks + 1


def test_game_state_goes_out_in_every_phase():
    g = Game()
    g.run(1.05)
    assert len(g.states) == 10
    assert {s["tick"] for s in g.states} == {0}


# --- actions -----------------------------------------------------------------------


def test_actions_are_refused_outside_running():
    g = Game()
    g.run(0.2)
    refused = g.tap(1, name="Mage")[0]
    assert (refused.reason, refused.what, refused.action) == ("not_running", "Mage", "spawn")
    assert g.gesture(1)[0].reason == "not_running"
    assert g.match.calls == []


def test_a_tap_goes_to_the_core_with_its_own_aim_and_comes_back_as_an_event():
    g = running_game()
    event = g.tap(2, name="Rogue", aim=(0.8, 0.1))[0]
    assert (event.ok, event.input, event.what, event.unit_id) == (True, "tap", "Rogue", 1)
    method, (player, action, aim) = g.match.calls[-1]
    assert (method, player, aim) == ("request", 2, (0.8, 0.1))
    assert action == stub.Action("spawn", "Rogue")


def test_a_repeated_tap_gets_one_event():
    g = running_game()
    assert len(g.tap(1, req_id=50)) == 1
    assert g.tap(1, req_id=50) == []  # the same tap in a new envelope
    assert len(g.tap(1, req_id=51, event_id="same")) == 1
    assert g.tap(1, req_id=52, event_id="same") == []  # the same envelope again


def test_an_old_match_lane_is_ignored_and_an_old_match_tap_refused():
    g = running_game()
    g.s.on_lane(1, "phone-a", Lane("e1-0", 2.0, (5.0, 5.0), None), g.now)
    assert g.match.positions[1] == (0.0, 0.0)
    assert g.s.counts["old_lane"] == 1
    assert g.tap(1, match_id="e1-0")[0].reason == "old_match"


def test_another_source_is_refused_and_moves_no_tower():
    g = running_game()
    assert g.tap(1, source="phone-c")[0].reason == "wrong_source"
    g.s.on_lane(1, "phone-c", Lane(g.s.match_id, 2.0, (9.0, 9.0), None), g.now)
    assert g.match.positions[1] == (0.0, 0.0)


def test_a_relaunched_app_takes_over_once_the_old_one_is_quiet():
    g = running_game()
    g.sources[2] = None
    g.run(2.0)
    g.sources[2] = "phone-c"
    g.run(ge.RESUME_S + 0.2)
    assert g.s.players[2].bound == "phone-c"
    assert g.s.phase == ge.RUNNING
    assert g.tap(2, source="phone-b")[0].reason == "wrong_source"
    assert g.tap(2, source="phone-c")[0].ok


def test_a_phone_replaced_while_running_pauses_the_match_until_the_new_one_has_streamed():
    """Player 2's app is reopened, and the new copy starts sending before the old one has been
    silent for a full second. The match must still pause, and resume only once the new copy has
    sent for RESUME_S."""
    g = running_game()
    g.sources[2] = None
    g.run(0.85)
    g.sources[2] = "phone-c"
    g.run(0.3)
    assert g.s.phase == ge.PAUSED
    assert g.tap(2, source="phone-c")[0].reason == "not_running"
    g.run(ge.RESUME_S + 0.2)
    assert (g.s.phase, g.s.players[2].bound) == (ge.RUNNING, "phone-c")
    assert g.tap(2, source="phone-c")[0].ok


def test_a_gesture_uses_the_latest_aim_of_the_bound_phone():
    g = running_game()
    event = g.gesture(2, "rogue", event_id="ultra96-relay:ab:3")[0]
    assert (event.ok, event.gesture) == (True, "rogue")
    assert event.gesture_event_id == "ultra96-relay:ab:3"
    assert g.match.calls[-1] == ("gesture", (2, "rogue", (0.75, 0.0)))


def test_a_gesture_without_an_aim_reaches_the_core_with_none():
    g = running_game()
    g.aim[2] = None
    g.run(0.1)
    g.gesture(2)
    assert g.match.calls[-1] == ("gesture", (2, "warrior", None))


def test_after_a_gap_a_gesture_waits_for_resume_s_of_stream():
    g = running_game()
    g.sources[2] = None
    g.run(0.6)  # a gap longer than AIM_FRESH_S, too short to pause
    g.sources[2] = "phone-b"
    g.run(0.1)
    assert g.s.phase == ge.RUNNING
    assert g.gesture(2)[0].reason == "phone_not_connected"
    g.run(ge.RESUME_S)
    assert g.gesture(2)[0].ok


def test_a_repeated_gesture_is_applied_once():
    g = running_game()
    assert len(g.gesture(1, event_id="ultra96-relay:ab:9")) == 1
    assert g.gesture(1, event_id="ultra96-relay:ab:9") == []


def test_positions_come_only_from_the_bound_phone():
    g = running_game()
    assert g.match.positions == {1: (0.0, 0.0), 2: (1.0, 0.0)}


def test_a_bubble_goes_to_the_core():
    g = running_game()
    event = g.s.on_bubble(1, "phone-a", g.s.match_id, 0, "bub-1", g.now)[0]
    assert (event.ok, event.input, event.action) == (True, "bubble", "bubble")
    assert g.match.calls[-1] == ("bubble", (1, 0))


# --- God mode -------------------------------------------------------------------------


def test_god_mode_reaches_the_core_before_any_phone_streams():
    g = Game()  # a new match with no lane yet, as when a phone sends God mode again for it
    assert g.press(1, "god_on") == []
    assert g.press(1, "god_off") == []
    assert g.match.calls == [("set_god_mode", (1, True)), ("set_god_mode", (1, False))]
    assert g.s.counts["god_refused"] == 0


def test_a_repeated_god_request_reaches_the_core_once():
    g = Game()
    g.press(1, "god_on", req_id=7)
    g.press(1, "god_on", req_id=7)  # the same request in a new envelope
    assert g.match.calls == [("set_god_mode", (1, True))]


def test_god_mode_comes_only_from_the_bound_phone_for_the_current_match():
    g = running_game()
    g.press(2, "god_on", source="phone-c")
    g.press(2, "god_on", match_id="e1-0")
    assert g.s.counts["god_refused"] == 2
    assert g.press(2, "god_on") == []
    assert g.match.calls == [("set_god_mode", (2, True))]  # never a tap
    assert g.s.phase == ge.RUNNING


# --- errors ---------------------------------------------------------------------------


def test_a_tick_that_raises_ends_the_match_and_the_last_good_state_repeats():
    g = running_game()
    last = g.states[-1]
    g.match.raise_in.add("tick")
    g.run(0.15)
    asked = g.match.snapshots
    g.run(0.5)
    assert (g.s.phase, g.info.reason, g.info.winner) == (ge.ENDED, "engine error", "none")
    assert all(state == last for state in g.states[-4:])
    assert g.match.snapshots == asked  # never asked again


def test_an_action_that_raises_ends_the_match():
    """The core may have spent the mana before it failed, so nothing can carry on (R108)."""
    g = running_game()
    g.match.raise_in.add("request")
    assert g.tap(1)[0].reason == "engine_error"
    assert (g.s.phase, g.info.reason) == (ge.ENDED, "engine error")
    assert g.tap(1)[0].reason == "not_running"


def test_a_nan_snapshot_ends_the_match_once_and_the_core_is_not_asked_again():
    g = running_game()
    last = g.states[-1]
    g.match.nan = True
    g.run(0.05 + ge.TICK_S)
    assert g.s.phase == ge.ENDED
    asked = g.match.snapshots
    g.run(1.0)
    assert g.match.snapshots == asked
    assert g.states[-1] == last


def test_a_snapshot_missing_a_field_ends_the_match_too():
    g = running_game()

    def encode_missing(snapshot, match_id):
        raise AttributeError("'Snapshot' object has no attribute 'units'")

    g.s.encode = encode_missing
    g.run(ge.TICK_S + 0.01)
    assert (g.s.phase, g.info.reason) == (ge.ENDED, "engine error")


def test_a_nan_opening_state_at_startup_is_fatal():
    with pytest.raises(ge.EngineFatal):
        Game(rules={"nan": True})


# --- the end and the rematch --------------------------------------------------------


def end_by_result(g, winner="player1"):
    g.match.result = winner
    g.run(ge.TICK_S + 0.01)
    assert (g.s.phase, g.info.winner) == (ge.ENDED, winner)


def test_this_matchs_starts_do_not_count_as_rematches():
    g = running_game()
    end_by_result(g)
    g.run(1.0)
    assert g.info.starts == (False, False)
    assert g.s.match_id == "e1-1"


def test_both_rematches_start_the_next_countdown_with_no_further_start():
    g = running_game()
    end_by_result(g, "draw")
    g.press(1, action="rematch")
    assert g.info.starts == (True, False)
    g.press(2, action="rematch")
    g.run(0.15)  # time for each phone to send one sample with the new match_id
    assert g.s.match_id == "e1-2"
    assert g.s.phase == ge.COUNTDOWN
    assert g.info.winner == "none"


def test_a_rematch_waits_for_lane_samples_of_the_new_match():
    """Just after a rematch, the phones' samples from the last match are still recent. They
    mustn't count: the countdown waits for samples with the new match_id."""
    g = running_game()
    end_by_result(g)
    old = g.s.match_id
    g.press(1, action="rematch")
    g.press(2, action="rematch")
    assert (g.s.match_id, g.s.phase) == ("e1-2", ge.WAITING)
    g.s.on_lane(1, "phone-a", Lane(old, 2.0, None, None), g.now)
    g.s.on_lane(2, "phone-b", Lane(old, 2.0, None, None), g.now)
    assert g.s.phase == ge.WAITING
    assert g.info.phones == (False, False)
    g.run(0.15)
    assert g.s.phase == ge.COUNTDOWN


def test_a_start_in_the_ended_phase_is_not_a_rematch():
    g = running_game()
    end_by_result(g)
    g.press(1)
    g.press(2)
    g.run(0.1)
    assert g.s.match_id == "e1-1"


def test_a_nan_opening_state_at_a_rematch_is_fatal_before_the_new_match_is_announced():
    rules = {}
    g = running_game(rules)
    end_by_result(g)
    rules["nan"] = True  # the next Match reads it
    g.press(1, action="rematch")
    version = g.s.version
    with pytest.raises(ge.EngineFatal):
        g.press(2, action="rematch")
    assert g.s.match_id == "e1-1"
    assert g.s.version == version


def test_match_info_changes_only_when_something_in_it_does():
    g = running_game()
    version = g.s.version
    g.run(1.0)
    assert g.s.version == version
    g.gloves(one=False)
    assert g.s.version == version + 1


def test_a_result_that_isnt_a_winner_ends_the_match_as_an_engine_error():
    g = running_game()
    g.match.result = "player3"
    g.run(ge.TICK_S + 0.01)
    assert (g.s.phase, g.info.winner, g.info.reason) == (ge.ENDED, "none", "engine error")


# --- inbound messages, through Engine.handle -------------------------------------------

RULES = {"version": "r-1", "BubbleCaptureManager": {"bubbleCount": 1}}


def message(payload, source="phone-b", event_id=None):
    """One MQTT message's bytes, in an envelope as the phone sends it."""
    env = {"event_id": event_id or f"ev-{next(ids)}", "source": source, "seq": 1, "ts": 0.0,
           "payload": payload}
    return json.dumps(env).encode()


def lane_payload(g, **fields):
    return dict({"match_id": g.s.match_id, "hasLane": True, "lane_length_m": 2.0,
                 "hasPosition": True, "along": 1.0, "lat": 0.2, "locked": True, "hasAim": True,
                 "aimAlong": 0.75, "aimLat": 0.0}, **fields)


def tap_payload(g, **fields):
    return dict({"match_id": g.s.match_id, "req_id": next(ids), "action": "spawn",
                 "unitType": "Rogue", "spell": "", "hasAim": True, "along": 0.75, "lat": 0.0},
                **fields)


def test_each_topic_reaches_its_handler():
    g = running_game(dict(RULES))
    engine = ge.Engine(g.s)
    engine.handle("viz/lane/2", message(lane_payload(g)), g.now)
    assert g.match.positions[2] == (1.0, 0.2)

    [tap] = engine.handle("game/request/2", message(tap_payload(g)), g.now)
    assert (tap.input, tap.what, tap.ok) == ("tap", "Rogue", True)

    relay = message(ai_events._event(1, 0, "warrior"), source="ultra96-relay", event_id="r:1:0")
    [glove] = engine.handle("game/gesture/1", relay, g.now)
    assert (glove.input, glove.gesture_event_id, glove.ok) == ("glove", "r:1:0", True)

    bubble = {"match_id": g.s.match_id, "bubble_id": 0, "event": "captured", "player": 2}
    [captured] = engine.handle("viz/bubble_captured/2", message(bubble), g.now)
    assert (captured.input, captured.ok) == ("bubble", True)

    gloves = {"player1": "ready", "player2": "down"}
    engine.handle("status/gloves", message(gloves, source="ultra96-gloves"), g.now)
    assert (g.s.phase, g.info.reason) == (ge.PAUSED, "player 2 glove down")


def test_a_gesture_with_no_player_is_ignored():
    g = running_game(dict(RULES))
    engine = ge.Engine(g.s)
    assert engine.handle("game/gesture/0", message(ai_events._event(0, 0, "warrior")), g.now) == []
    assert g.s.counts["no_player"] == 1
    assert g.match.calls == []


def test_a_bad_message_is_counted_and_only_the_first_of_each_kind_logged(caplog):
    g = Game(dict(RULES))
    engine = ge.Engine(g.s)
    for _ in range(3):
        engine.handle("viz/lane/1", message(lane_payload(g, hasLane="yes")), g.now)
    engine.handle("viz/lane/1", message(lane_payload(g, along=11.0)), g.now)
    engine.handle("viz/lane/1", b"not json", g.now)
    assert g.s.counts["bad_lane"] == 5
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 3
    assert "viz/lane/1" in warnings[0] and "hasLane" in warnings[0]


def test_a_bad_tap_gets_one_bad_request_event_and_a_bad_start_none():
    g = running_game(dict(RULES))
    engine = ge.Engine(g.s)
    bad_tap = message(tap_payload(g, unitType="Dragon", req_id=77))
    [event] = engine.handle("game/request/2", bad_tap, g.now)
    assert (event.ok, event.reason, event.req_id) == (False, "bad_request", 77)
    assert event.action == "spawn"
    assert engine.handle("game/request/2", bad_tap, g.now) == []  # the broker's redelivery

    w = Game(dict(RULES))
    w.run(0.2)
    w.gloves()
    engine = ge.Engine(w.s)
    bad_start = tap_payload(w, action="start", unitType="", hasAim="yes")
    assert engine.handle("game/request/1", message(bad_start, source="phone-a"), w.now) == []
    assert w.info.starts == (False, False)
    assert w.s.counts["bad_request"] == 1


def test_a_bubble_outside_the_count_is_dropped():
    g = running_game(dict(RULES))
    engine = ge.Engine(g.s)
    bubble = {"match_id": g.s.match_id, "bubble_id": 1, "event": "captured", "player": 2}
    assert engine.handle("viz/bubble_captured/2", message(bubble), g.now) == []
    assert g.s.counts["bad_bubble"] == 1
    assert g.match.calls == []


def test_the_same_retained_glove_status_after_a_reconnect_still_sets_the_gloves():
    """The broker sends the same retained envelope again, so a dedupe here would stall the match."""
    g = running_game(dict(RULES))
    engine = ge.Engine(g.s)
    retained = message({"player1": "ready", "player2": "ready"}, event_id="ultra96-gloves:ab:3")
    engine.handle("status/gloves", retained, g.now)
    g.s.on_mqtt_lost(g.now)
    assert g.info.gloves == (False, False)
    engine.handle("status/gloves", retained, g.now)
    assert g.info.gloves == (True, True)


def test_a_bad_glove_status_leaves_the_gloves_as_they_were():
    g = running_game(dict(RULES))
    engine = ge.Engine(g.s)
    engine.handle("status/gloves", b'{"payload": {"player1": "down"}}', g.now)
    assert g.info.gloves == (True, True)
    assert g.s.counts["bad_gloves"] == 1


def test_offline_names_no_match_even_after_a_rematch():
    """The will is fixed when the engine connects, so it must never name a match."""
    g = running_game(dict(RULES))
    engine = ge.Engine(g.s)
    end_by_result(g)
    g.press(1, action="rematch")
    g.press(2, action="rematch")
    assert g.s.match_id == "e1-2"
    offline = json.loads(engine.offline_message(ge.STOPPED))["payload"]
    assert (offline["match_id"], offline["phase"]) == ("", "offline")


def test_every_message_the_engine_wraps_gets_a_new_event_id():
    engine = ge.Engine(Game(dict(RULES)).s)
    ids_sent = {json.loads(engine.wrap({"x": 1}))["event_id"] for _ in range(3)}
    assert len(ids_sent) == 3
    assert all(i.startswith("ultra96-engine:") for i in ids_sent)


# --- loading the core and rules.json ------------------------------------------------------

STUB_DIR = Path(stub.__file__).parent
VIS_ENGINE = Path(__file__).resolve().parents[3] / "SW_Visualiser" / "engine"


def core_dir(tmp_path, rules=RULES, match_py=None):
    (tmp_path / "match.py").write_text(match_py or (STUB_DIR / "match.py").read_text())
    if rules is not None:
        text = rules if isinstance(rules, str) else json.dumps(rules)
        (tmp_path / "rules.json").write_text(text)
    return tmp_path


def test_a_core_dir_loads_match_py_and_rules_json(tmp_path):
    core, rules = ge.load_core(core_dir(tmp_path))
    assert rules == RULES and hasattr(core, "Match")
    ge.Session(core, rules, lambda *_: {}, "e1", 0.0)


@pytest.mark.parametrize(
    "rules, match_py",
    [
        (None, None),  # no rules.json
        ("{not json", None),
        ("[1, 2]", None),
        (dict(RULES, TowerController={"TowerRadius": None}), None),
        ({"BubbleCaptureManager": {"bubbleCount": 1}}, None),  # no version
        (dict(RULES, version=""), None),
        (dict(RULES, version=3), None),
        (RULES, "raise ImportError('broken core')\n"),
    ],
)
def test_a_core_that_cant_be_used_is_fatal(tmp_path, rules, match_py):
    with pytest.raises(ge.EngineFatal):
        ge.load_core(core_dir(tmp_path, rules, match_py))


def test_a_missing_match_py_is_fatal(tmp_path):
    (tmp_path / "rules.json").write_text(json.dumps(RULES))
    with pytest.raises(ge.EngineFatal):
        ge.load_core(tmp_path)


def test_a_null_inside_a_note_key_is_allowed():
    assert ge._unfilled({"_left_out": {"x": None}, "a": [1, {"b": 2}]}) == ""
    assert ge._unfilled({"a": [1, {"b": None}]}) == "a.1.b"


def test_vis_rules_json_and_match_py_as_committed_start_the_engine():
    core, rules = ge.load_core(VIS_ENGINE)
    session = ge.Session(core, rules, state_payload, "e1", T0)
    assert session.match_info().rules_version == rules["version"]


@pytest.mark.parametrize("rules", [{}, {"BubbleCaptureManager": {"bubbleCount": 0}},
                                   {"BubbleCaptureManager": {"bubbleCount": True}},
                                   {"BubbleCaptureManager": 1}])
def test_an_engine_needs_a_bubble_count(rules):
    with pytest.raises(ge.EngineFatal):
        ge.Engine(Game(rules).s)


def test_the_bubble_respawn_cooldown_is_no_longer_than_its_hold_time():
    """Otherwise the core's cooldown could refuse a real second capture."""
    bubbles = json.loads((VIS_ENGINE / "rules.json").read_text())["BubbleCaptureManager"]
    assert bubbles["respawnCooldownSeconds"] <= bubbles["holdTimeSeconds"]


def test_game_match_reports_the_version_in_rules_json():
    assert Game(dict(RULES, version="r-7")).info.rules_version == "r-7"


# --- a core that misbehaves on an action ---------------------------------------------------


def test_an_action_class_that_raises_ends_the_match():
    g = running_game(dict(RULES))

    def broken_action(kind, name):
        raise TypeError("Action takes 3 arguments")

    g.s.core = types.SimpleNamespace(Match=stub.Match, Action=broken_action)
    [event] = g.tap(2)
    assert (event.ok, event.reason) == (False, "engine_error")
    assert (g.s.phase, g.info.reason) == (ge.ENDED, "engine error")


@pytest.mark.parametrize(
    "result",
    [
        stub.Result(False, "no_mana", "spell", "fireball", None),
        stub.Result(True, "", "spell", "lightning", None),
        stub.Result(True, "", "spawn", "Tank", 9),
        stub.Result(False, "unknown_gesture", "none", "", None),
    ],
)
def test_a_result_the_spec_allows_is_passed_on(result):
    g = running_game(dict(RULES))
    g.match.request = lambda *args: result
    [event] = g.tap(2)
    assert (event.ok, event.reason, event.action, event.what) == (
        result.ok, result.reason, result.action, result.what)
    assert event.unit_id == (-1 if result.unit_id is None else result.unit_id)
    assert g.s.phase == ge.RUNNING


@pytest.mark.parametrize(
    "result",
    [
        None,
        stub.Result(True, "", "spawn", "Warrior", 3_000_000_000),  # too big for the phone's int
        stub.Result(True, "", "spawn", "Warrior", 2.0),
        stub.Result(True, "", "spawn", "Warrior", float("nan")),
        stub.Result(1, "", "spawn", "Warrior", 2),
        stub.Result(True, None, "spawn", "Warrior", 2),
        types.SimpleNamespace(ok=True, reason="", action="spawn", what="Warrior"),  # no unit_id
        stub.Result(True, "", "nuke", "Warrior", 2),
        stub.Result(True, "", "spawn", "Dragon", 2),
        stub.Result(False, "bogus", "spawn", "Warrior", None),
        stub.Result(False, "", "spawn", "Warrior", None),  # refused with no reason
        stub.Result(True, "no_mana", "spawn", "Warrior", 2),  # accepted with a reason
        stub.Result(False, "no_mana", "spawn", "Warrior", 42),  # a unit id for a refusal
        stub.Result(True, "", "spawn", "Warrior", None),  # a spawn with no unit
        stub.Result(True, "", "spell", "fireball", 7),  # a spell makes no unit
        stub.Result(True, "", "spell", "Warrior", None),
        stub.Result(True, "", "spawn", "lightning", 2),
        stub.Result(True, "", "bubble", "Warrior", None),
        stub.Result(True, "", [], "Warrior", 2),  # an action that can't be a dict key
        stub.Result(False, ["no_mana"], "spawn", "Warrior", None),
    ],
)
def test_a_result_the_phones_cant_read_ends_the_match(result):
    g = running_game(dict(RULES))
    g.match.request = lambda *args: result
    [event] = g.tap(2)
    assert (event.ok, event.reason, event.unit_id) == (False, "engine_error", -1)
    assert (g.s.phase, g.info.reason) == (ge.ENDED, "engine error")
    json.dumps(ge.wire.event_payload(event, g.s.match_id), allow_nan=False)
