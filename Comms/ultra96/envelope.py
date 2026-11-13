"""MQTT topic and envelope for the events this board publishes (configuration B).

A copy of the laptop's envelope code: the two machines are deployed separately, so
it cannot be imported. laptop/tests/test_board_mqtt.py checks the two agree.
The contract itself is defined only in notes/mqtt_topics.md; change it there first.
"""

import time
import uuid

# This publisher's name in `source` and `event_id` (the laptop's is "laptop-relay").
CLIENT_ID_ULTRA96 = "ultra96-relay"

# New each time the program starts, so event_ids stay unique even though seq restarts at 0.
SESSION_ID = uuid.uuid4().hex[:8]

# The board's own broker: plain MQTT on loopback only (reached by the phone over SSH).
PLAINTEXT_PORT = 1883
LOOPBACK_HOST = "127.0.0.1"

# The power monitor's latest readings, retained, for the dashboard (power_monitor.py).
# Plain JSON, not an envelope: nothing in the game reads it.
POWER_TOPIC = "status/power"

# One retained topic for both gloves, so one last will covers both.
GLOVES_TOPIC = "status/gloves"
# Its own source, so its event_ids never collide with the AI events'.
GLOVES_SOURCE = "ultra96-gloves"
PLAYERS = (1, 2)
READY = "ready"  # streaming, with a working IMU
DOWN = "down"  # silent, too slow, or no IMU reading
OFFLINE = "offline"  # the board's client is not running


def gesture_topic(player):
    """Topic for one player's events, e.g. game/gesture/1.

    The player is in the topic, so nobody has to open the payload to route it.
    """
    return f"game/gesture/{player}"


def gloves_payload(states):
    """Return the status/gloves payload for {player: state}, e.g. {"player1": "ready", ...}."""
    return {f"player{player}": states[player] for player in PLAYERS}


def build_envelope(payload, seq, source=CLIENT_ID_ULTRA96, session=SESSION_ID):
    """Wrap a game event in the standard envelope: event_id, source, seq, ts, payload.

    event_id is source:session:seq, e.g. "ultra96-relay:3f2a1b9c:17". The phone uses
    it to spot QoS 1 duplicates.
    """
    return {
        "event_id": f"{source}:{session}:{seq}",
        "source": source,
        "seq": seq,
        "ts": time.time(),  # wall clock: this is displayed, not used for durations
        "payload": payload,
    }
