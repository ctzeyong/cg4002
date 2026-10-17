"""Dummy AI events generated on the Ultra96 (a stand-in for the real classifier)."""

import random
import time

# The Vis game's seven gestures: its GestureSymbol enum, lowercased, in order. The enum
# is the only list; test_vis_vocabulary.py fails if this one drifts from it.
GESTURES = ("circle", "caret", "arc", "z", "infinity", "triangle", "star")

# Pressed key -> gesture. The only copy of this table; see notes/tcp_leg.md.
# Any other key (including 1 and 2) gets a random gesture.
KEY_GESTURES = {
    "c": "circle",
    "k": "caret",
    "a": "arc",
    "z": "z",
    "i": "infinity",
    "t": "triangle",
    "s": "star",
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
        # Board wall clock. Hop 4 of the latency measurement: taken before A and B's
        # emits diverge, so it marks the same moment in both. notes/latency.md 3.2.
        "timestamp": time.time(),
    }
