"""Write the JSON the engine sends (game/state, game/match, game/event) for a real match, for the C# checks.

Usage: python dump_wire_cases.py OUT.json   (needs Comms/ultra96 beside this repo's SW_Visualiser)
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "Comms" / "ultra96"))
import engine_wire as wire  # noqa: E402
import match  # noqa: E402

rules = json.loads((HERE / "rules.json").read_text(encoding="utf-8"))
rules["TowerController"]["TowerRadius"] = 0.15
m = match.Match(rules, 1)
m.start(2.0)
m.set_god_mode(1, True)  # spells need mana the summons would spend
states, events = [], []
for tick in range(900):
    if tick % 15 == 0:
        kind = ("warrior", "rogue", "mage", "tank", "hero")[tick // 15 % 5]
        m.gesture(1, kind, (0.1, 0.1 * (tick // 15 % 5 - 2)))
        m.gesture(2, kind, (0.9, 0.1 * (tick // 15 % 5 - 2)))
    if tick % 60 == 0:
        m.gesture(1, "fireball" if tick % 120 else "lightning", (0.5, 0.0))
    states.append(wire.state_payload(m.tick(0.1), "e1-1"))
fresh = match.Match(rules, 2)  # the long match above has ended
fresh.start(2.0)
for result, gesture in ((fresh.gesture(1, "warrior", (0.1, 0.0)), "warrior"), (fresh.gesture(1, "circle", None), "circle"),
                        (fresh.bubble(1, 0), "")):
    event = wire.GameEvent(1, "glove", ok=result.ok, reason=result.reason, action=result.action, what=result.what,
                           gesture_event_id="ev-1", gesture=gesture, unit_id=-1 if result.unit_id is None else result.unit_id)
    events.append(wire.event_payload(event, "e1-1"))
infos = []
for phase in ("waiting", "countdown", "running", "paused", "ended"):
    info = wire.MatchInfo("e1-1", phase, "player 2 glove down", (True, False), (True, True), (False, True), "none", "v1")
    infos.append(wire.match_payload(info, 2500 if phase == "countdown" else 0, 10))
infos.append(wire.offline_payload("engine error", 10, "v1"))
json.dump({"states": states, "events": events, "matches": infos}, open(sys.argv[1], "w"), allow_nan=False)
print(f"{len(states)} states, {len(events)} events, {len(infos)} match messages")
