"""Build the AI events on the Ultra96.

Events come from the classifier, or are dummies for ECHO and --event-every.
"""

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
    return _event(player, seq, KEY_GESTURES.get(key) or random.choice(GESTURES))


def make_event(player, seq, key=None, result=None):
    """Build one AI event, from the classifier's result if there is one, otherwise a dummy.

    Both have the same fields. `key` only picks the gesture of a dummy event.
    """
    if result is None:
        return random_event(player, seq, key)
    return _event(player, seq, result.label)


def _event(player, seq, gesture):
    return {
        "type": "AI_EVENT",
        "player": player,
        "seq": seq,
        "gesture": gesture,
        # board wall clock, taken after inference and before the A and B emits split,
        # hop 4 of the latency measurement
        "timestamp": time.time(),
    }
