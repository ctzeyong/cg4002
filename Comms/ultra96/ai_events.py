"""Dummy AI events generated on the Ultra96 (a stand-in for the real classifier)."""

import random
import time

GESTURES = ("circle", "triangle", "square", "zigzag", "arc", "line")

MANA_COST_RANGE = (10, 40)  # dummy game-state value, picked at random per event

# Pressed key -> gesture. The only copy of this table; see notes/tcp_leg.md.
# Any other key (including 1 and 2) gets a random gesture.
KEY_GESTURES = {
    "c": "circle",
    "t": "triangle",
    "s": "square",
    "z": "zigzag",
    "a": "arc",
    "l": "line",
}


def random_event(player, seq, key=None):
    """Build one dummy AI event for `player`.

    `key` (the pressed key) picks the gesture if it is in KEY_GESTURES; otherwise
    random. `seq` comes from the caller, so all events share one counter.
    """
    return {
        "type": "AI_EVENT",
        "player": player,
        "seq": seq,
        "gesture": KEY_GESTURES.get(key) or random.choice(GESTURES),
        "mana_cost": random.randint(*MANA_COST_RANGE),
        # Board wall clock. Hop 4 of the latency measurement: taken before A and B's
        # emits diverge, so it marks the same moment in both. notes/latency.md 3.2.
        "timestamp": time.time(),
    }
