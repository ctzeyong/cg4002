"""Pin the dummy gesture names to the Vis game's own list of gestures.

The game defines the gestures, so the vocabulary is Vis-owned: the `GestureSymbol`
enum in `SW_Visualiser/script/Input/GestureRecognizer.cs`. The phone's hook keeps
no list of its own; it parses `payload.gesture` straight into that enum. So
`ai_events.GESTURES` is the only copy on the Comms side, and a name in it that the
enum lacks reaches the phone as "unknown gesture" and spawns nothing.

That drift raises no error on the board or the laptop, so it is caught here. This
reads the enum's *members*, the vocabulary Vis publishes, not its internals.
notes/integration/vis_integration.md section 4.6.

This is a source-parsing test. If it fails because Vis renamed or added a
gesture, change `GESTURES` and `KEY_GESTURES` to match; don't edit the enum.
"""

import pathlib
import re

import ai_events

_TEAM = pathlib.Path(__file__).resolve().parents[3]
_RECOGNIZER = _TEAM / "SW_Visualiser" / "script" / "Input" / "GestureRecognizer.cs"

# What the enum holds today. Only the guard against a parser that silently finds
# nothing uses it; the real check compares against whatever the file says.
EXPECTED_MEMBERS = ["Circle", "Caret", "Arc", "Z", "Infinity", "Triangle", "Star"]

_ENUM = re.compile(r"\benum\s+GestureSymbol\s*\{(?P<body>[^}]*)\}")


def enum_members(source):
    """Member names of `enum GestureSymbol` in C# source, in declaration order.

    Comments are stripped first, and any `= value` is dropped, so only the names
    are left.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"//[^\n]*", "", source)
    match = _ENUM.search(source)
    if match is None:
        return []
    members = []
    for item in match.group("body").split(","):
        name = item.split("=")[0].strip()
        if name:
            members.append(name)
    return members


def test_the_enum_file_exists():
    assert _RECOGNIZER.exists(), f"{_RECOGNIZER} is missing"


def test_the_parser_finds_the_seven_members_vis_has_today():
    """Without this, a regex that stopped matching would compare two empty lists."""
    members = enum_members(_RECOGNIZER.read_text(encoding="utf-8"))

    assert members == EXPECTED_MEMBERS, (
        f"parsed {members} from {_RECOGNIZER.name}. If Vis changed the enum, update "
        "EXPECTED_MEMBERS and ai_events.GESTURES together; if the list is empty, the "
        "parser broke."
    )


def test_dummy_names_are_the_enum_lowercased_in_order():
    members = enum_members(_RECOGNIZER.read_text(encoding="utf-8"))

    assert list(ai_events.GESTURES) == [m.lower() for m in members]


def test_every_gesture_has_a_key_and_the_node_keys_stay_free():
    """A key per gesture, so every name can be fired by hand. 1 and 2 pick a node."""
    assert sorted(ai_events.KEY_GESTURES.values()) == sorted(ai_events.GESTURES)
    assert "1" not in ai_events.KEY_GESTURES
    assert "2" not in ai_events.KEY_GESTURES


def test_the_parser_reads_comments_and_explicit_values_correctly():
    source = """
    public enum GestureSymbol
    {
        Circle = 0, // the first
        /* Old, */ Caret,
        Arc
    }
    """
    assert enum_members(source) == ["Circle", "Caret", "Arc"]


def test_the_parser_returns_nothing_when_the_enum_is_absent():
    assert enum_members("public enum Other { A, B }") == []
