"""The real core inside the engine shell's Session (Comms/ultra96/game_engine.py), on a fake clock.

Checks that load_core accepts match.py and rules.json, that every snapshot the core makes passes the
wire checks, and that a whole match runs through the START gate, the countdown and the end.
"""

import json
import pathlib
import shutil
import sys

import pytest

HERE = pathlib.Path(__file__).parent
ULTRA96 = HERE.parent.parent / "Comms" / "ultra96"
if not ULTRA96.exists():
    pytest.skip("needs Comms/ultra96", allow_module_level=True)
sys.path.insert(0, str(ULTRA96))
pytest.importorskip("aiomqtt")
import game_engine as ge  # noqa: E402
from engine_wire import Lane, Request, state_payload  # noqa: E402

T0 = 1000.0


@pytest.fixture
def session(tmp_path):
    shutil.copy(HERE / "match.py", tmp_path / "match.py")
    rules = json.loads((HERE / "rules.json").read_text(encoding="utf-8"))
    rules["TowerController"]["TowerRadius"] = 0.15
    (tmp_path / "rules.json").write_text(json.dumps(rules), encoding="utf-8")
    core, loaded = ge.load_core(tmp_path)
    return Game(core, loaded)


class Game:
    def __init__(self, core, rules):
        self.now = T0
        self.s = ge.Session(core, rules, state_payload, "e1", self.now)
        self.next_lane = self.now
        self.states = []
        self.req = 0
        self.aim = {1: (0.1, 0.0), 2: (0.9, 0.0)}
        self.position = {1: None, 2: None}
        self.live = False

    def lane(self, player):
        return Lane(self.s.match_id, 2.0, self.position[player], self.aim[player])

    def run(self, seconds):
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.now = round(self.now + 0.01, 6)
            if self.now >= self.next_lane - 1e-9:
                self.next_lane = round(self.next_lane + 0.1, 6)
                for player in (1, 2):
                    self.s.on_lane(player, f"phone-{player}", self.lane(player), self.now)
            if self.now >= self.s.next_tick:
                self.states.append(self.s.on_tick(self.now))

    def start(self, player):
        self.req += 1
        request = Request(self.s.match_id, self.req, "start", "", None)
        self.s.on_request(player, f"phone-{player}", request, f"ev{self.req}", self.now)

    def glove(self, player, name):
        self.req += 1
        return self.s.on_gesture(player, name, f"g{self.req}", self.now)

    def tap(self, player, kind, name):
        self.req += 1
        request = Request(self.s.match_id, self.req, kind, name, self.aim[player])
        return self.s.on_request(player, f"phone-{player}", request, f"t{self.req}", self.now)


def test_whole_match_through_the_shell(session):
    g = session
    g.s.on_gloves({1: True, 2: True}, g.now)
    g.run(0.5)
    assert g.s.phase == ge.WAITING
    g.start(1)
    g.start(2)
    g.run(0.5)
    assert g.s.phase == ge.COUNTDOWN
    g.run(3.2)
    assert g.s.phase == ge.RUNNING
    for p in (1, 2):
        g.position[p] = (0.0 if p == 1 else 1.0, 0.0)
    g.run(1.2)  # the aim counts again once the stream has run a second

    events = g.glove(1, "warrior")
    assert events[0].ok and events[0].what == "Warrior" and events[0].unit_id == 1
    events = g.glove(2, "lightning")
    assert events[0].ok and events[0].action == "spell"
    assert g.glove(1, "circle")[0].reason == "unknown_gesture"
    assert g.tap(1, "spawn", "Hero")[0].reason == "no_mana"

    g.run(5)
    assert g.states[-1]["units"]
    g.run(15)  # the Warrior reaches the tower, hits it, and dies to its counter-damage
    assert g.states[-1]["towerHP2"] < 1000
    assert g.s.phase == ge.RUNNING
    g.run(170)
    assert g.s.phase == ge.ENDED
    assert g.s.winner in ge.RESULTS
    assert not g.s.failed


def test_a_glove_without_a_phone_is_refused_without_calling_the_core(session):
    g = session
    g.s.on_gloves({1: True, 2: True}, g.now)
    events = g.glove(1, "warrior")
    assert events[0].reason in ("not_running", "phone_not_connected")


def test_engine_refuses_a_rules_file_without_a_tower_radius(tmp_path):
    shutil.copy(HERE / "match.py", tmp_path / "match.py")
    rules = json.loads((HERE / "rules.json").read_text(encoding="utf-8"))
    rules["TowerController"]["TowerRadius"] = None
    (tmp_path / "rules.json").write_text(json.dumps(rules), encoding="utf-8")
    with pytest.raises(ge.EngineFatal):
        ge.load_core(tmp_path)


def _running(g):
    g.s.on_gloves({1: True, 2: True}, g.now)
    g.run(0.5)
    g.start(1)
    g.start(2)
    g.run(3.8)
    assert g.s.phase == ge.RUNNING
    for p in (1, 2):
        g.position[p] = (0.0 if p == 1 else 1.0, 0.0)
    g.run(1.2)


def _god(g, player, action, match_id=None, source=None):
    g.req += 1
    request = Request(match_id or g.s.match_id, g.req, action, "", None)
    return g.s.on_request(player, source or f"phone-{player}", request, f"god{g.req}", g.now)


def test_god_mode_works_for_either_player_and_gives_no_event(session):
    g = session
    _running(g)
    assert g.tap(1, "spawn", "Hero")[0].reason == "no_mana"  # 8 mana, the player has 5 plus a little
    assert _god(g, 1, "god_on") == []
    assert g.tap(1, "spawn", "Hero")[0].ok
    assert g.tap(2, "spawn", "Hero")[0].reason == "no_mana"  # only the player who asked
    assert _god(g, 2, "god_on") == []
    assert g.tap(2, "spawn", "Hero")[0].ok
    assert _god(g, 1, "god_off") == []
    assert g.tap(1, "spawn", "Hero")[0].ok  # the mana God mode pinned at 10 pays 8 now
    g.run(0.1)
    assert g.states[-1]["mana1"] < 5
    assert g.tap(1, "spawn", "Hero")[0].reason == "no_mana"
    assert g.tap(2, "spawn", "Hero")[0].ok  # player 2 is still free
    assert not g.s.failed


def test_god_mode_from_another_match_or_source_is_dropped(session):
    g = session
    _running(g)
    _god(g, 1, "god_on", match_id="old")
    _god(g, 2, "god_on", source="phone-intruder")
    assert g.tap(1, "spawn", "Hero")[0].reason == "no_mana"
    assert g.tap(2, "spawn", "Hero")[0].reason == "no_mana"
    assert g.s.counts["god_refused"] == 2
