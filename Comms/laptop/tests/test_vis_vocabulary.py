"""Pin the dummy gesture names to both ends of the vocabulary: AI's classifier and the Vis game.

AI's classifier names each gesture (`NEW_LABEL_MAP` in `AI/board/keras_stream.py`), and the
Vis game takes a name only if its `GestureSymbol` enum in
`SW_Visualiser/script/Input/GestureBindings.cs` has a member of that name. The phone's hook
keeps no list of its own; it parses `payload.gesture` straight into that enum. So
`ai_events.GESTURES` is the only copy on the Comms side, and a name in it that the enum lacks
reaches the phone as "unknown gesture" and spawns nothing.

That drift raises no error on the board or the laptop, so it is caught here. This reads each
side's published names, not its internals: the enum's members, and AI's label map without the
labels its `wk10_integration.REJECT_LABELS` turns into no result.

These are source-parsing tests, so they need no numpy. If one fails because AI or Vis renamed
or added a gesture, change `GESTURES` and `KEY_GESTURES` to match; don't edit their files.
"""

import ast
import pathlib
import re

import ai_events

_TEAM = pathlib.Path(__file__).resolve().parents[3]
_BINDINGS = _TEAM / "SW_Visualiser" / "script" / "Input" / "GestureBindings.cs"
_KERAS_STREAM = _TEAM / "AI" / "board" / "keras_stream.py"
_WK10 = _TEAM / "AI" / "board" / "wk10_integration.py"

# What each side holds today. Only the guards against a parser that silently finds nothing
# use these; the real checks compare against whatever the files say.
EXPECTED_MEMBERS = ["Warrior", "Rogue", "Mage", "Tank", "Hero", "Lightning", "Fireball", "Shield"]
EXPECTED_LABELS = ["warrior", "mage", "fireball", "rogue", "lightning", "hero", "shield"]

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


def module_constant(path, name):
    """The literal value assigned to `name` at the top level of a Python file, or None."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    return None


def classifier_names():
    """AI's gesture names in class order, without the labels that give no result."""
    label_map = module_constant(_KERAS_STREAM, "NEW_LABEL_MAP") or {}
    rejected = module_constant(_WK10, "REJECT_LABELS") or ()
    return [label_map[i] for i in sorted(label_map) if label_map[i] not in rejected]


def test_the_source_files_exist():
    for path in (_BINDINGS, _KERAS_STREAM, _WK10):
        assert path.exists(), f"{path} is missing"


def test_the_parser_finds_the_members_vis_has_today():
    """Without this, a regex that stopped matching would compare against an empty list."""
    members = enum_members(_BINDINGS.read_text(encoding="utf-8"))

    assert members == EXPECTED_MEMBERS, (
        f"parsed {members} from {_BINDINGS.name}. If Vis changed the enum, update "
        "EXPECTED_MEMBERS and check ai_events.GESTURES; if the list is empty, the parser broke."
    )


def test_the_parser_finds_the_labels_ai_has_today():
    """The same guard for AI's side: an empty map would make the order check meaningless."""
    assert classifier_names() == EXPECTED_LABELS, (
        f"parsed {classifier_names()} from {_KERAS_STREAM.name}. If AI changed the labels, "
        "update EXPECTED_LABELS and ai_events.GESTURES; if the list is empty, the parser broke."
    )


def test_dummy_names_are_the_classifiers_names_in_class_order():
    assert list(ai_events.GESTURES) == classifier_names()


def test_every_dummy_name_is_a_member_of_the_vis_enum():
    """The hook parses ignoring case, so a lowercase name matches its capitalised member."""
    members = {m.lower() for m in enum_members(_BINDINGS.read_text(encoding="utf-8"))}

    missing = [g for g in ai_events.GESTURES if g not in members]
    assert not missing, f"the Vis game would show {missing} as unknown gestures"


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
