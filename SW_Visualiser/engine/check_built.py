"""Read what the C# side built (csharp_checks) with the board's own parsers. Usage: python check_built.py BUILT.json"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent / "Comms" / "ultra96"))
import engine_wire as wire  # noqa: E402

built = json.load(open(sys.argv[1]))
lanes = [wire.read_lane(json.loads(x)) for x in built["lanes"]]
assert lanes[0] == wire.Lane("e1-1", 2.1, (0.02, -0.1), (0.3, 0.05)), lanes[0]
assert lanes[1] == wire.Lane("", None, None, None), lanes[1]  # no lane, no position, no aim, flags false
assert lanes[2].aim is None and lanes[2].position == (0.02, -0.1)
requests = [wire.read_request(json.loads(x)) for x in built["requests"]]
assert requests[0] == wire.Request("e1-1", 7, "start", "", None)
assert requests[1] == wire.Request("e1-1", 8, "spawn", "Warrior", (0.1, 0.2)), requests[1]
assert requests[2] == wire.Request("e1-1", 9, "spell", "fireball", (0.5, 0.0)), requests[2]
assert requests[3] == wire.Request("e1-1", 10, "rematch", "", None)
print("the board reads every lane and request the phone builds")
