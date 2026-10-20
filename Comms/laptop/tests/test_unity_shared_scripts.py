"""Guard the C# files that the two Unity projects hold identical copies of.

`phone/demo-visualiser` is the LAN client (broker on the laptop) and
`phone/tunnelled-visualiser` is the same visualiser reached through the two-hop
SSH nest (broker on the Ultra96). They are separate Unity projects on purpose —
the LAN one is a proven APK and must not grow a 4.9 MB BouncyCastle dependency
it never uses — but that buys the classic duplicated-source drift.

Three files encode the wire contract with `mqtt_client.py`, and a change applied
to one copy and not the other is invisible: both projects still compile, both
still connect, and the one that was missed silently drops every event the relay
sends because the relay validates by type. So drift is made a test failure here,
the same way `conftest.py` leans on the session tests to catch drift between the
two copies of `framing.py`.

This is a file-comparison test and touches no Python under test. If it fails,
copy the changed file across rather than editing this list.
"""

import pathlib
import re
from xml.etree import ElementTree

import pytest

_COMMS = pathlib.Path(__file__).resolve().parents[2]
_LAN = _COMMS / "phone" / "demo-visualiser" / "Assets" / "Scripts"
_TUNNELLED = _COMMS / "phone" / "tunnelled-visualiser" / "Assets" / "Scripts"

# Only the contract files. MqttVisualiser.cs and VisualiserUI.cs are deliberately
# NOT here: the tunnelled project replaces them with TunnelledVisualiser.cs and
# TunnelledUI.cs, which supervise a second leg the LAN client does not have.
SHARED = ["Envelope.cs", "SeenEvents.cs", "MqttConfig.cs"]


@pytest.mark.parametrize("name", SHARED)
def test_shared_script_is_identical_in_both_projects(name):
    lan = _LAN / name
    tunnelled = _TUNNELLED / name

    assert lan.exists(), f"{lan} is missing"
    assert tunnelled.exists(), f"{tunnelled} is missing"

    # Bytes, not text: a copy that went through a tool which rewrote LF to CRLF
    # is still drift worth knowing about, and it is the same Windows newline trap
    # CLAUDE.md records for write_text in the mutation-testing rule.
    assert lan.read_bytes() == tunnelled.read_bytes(), (
        f"{name} has drifted between the two Unity projects. "
        f"Copy the intended version across; do not edit one in place."
    )


def test_tunnelled_project_has_no_stray_copy_of_the_lan_client():
    """The LAN client's own scripts must not be in the tunnelled project.

    Both declare a `[RuntimeInitializeOnLoadMethod]` bootstrap in the same
    namespace, so a stray copy does not fail to compile — it builds two
    visualisers at startup, and they fight over the broker client id.
    """
    for name in ["MqttVisualiser.cs", "VisualiserUI.cs"]:
        assert not (_TUNNELLED / name).exists(), (
            f"{name} must not exist in phone/tunnelled-visualiser: it would "
            f"bootstrap a second visualiser alongside TunnelledVisualiser."
        )


# --- phone/comms-visualiser: the folder the Vis app imports (vis_integration.md 4.2) ---------
#
# Copies of our scripts that the Vis teammate drops into his Unity project. Drift here is
# the same silent failure as above, and the folder's shape is part of the contract: the
# Transport/ half is deleted outright for fallback A, so Hook/ must never need it.

_DROPIN = _COMMS / "phone" / "comms-visualiser"

# drop-in path -> the tunnelled-visualiser script it must equal byte for byte.
DROPIN_COPIES = {
    "Shared/MqttConfig.cs": "MqttConfig.cs",
    "Transport/SshMqttTunnel.cs": "SshMqttTunnel.cs",
    "Transport/TunnelConfig.cs": "TunnelConfig.cs",
    "Hook/Envelope.cs": "Envelope.cs",
    "Hook/SeenEvents.cs": "SeenEvents.cs",
}

SSH_NET_DLLS = [
    "BouncyCastle.Cryptography.dll",
    "Microsoft.Extensions.DependencyInjection.Abstractions.dll",
    "Microsoft.Extensions.Logging.Abstractions.dll",
    "Renci.SshNet.dll",
    "System.Formats.Asn1.dll",
]

DROPIN_FILES = sorted(
    ["README.md", "Hook/AiGestureHook.cs", "Hook/GestureEventParser.cs",
     "Transport/CommsTunnelBridge.cs", "Transport/link.xml"]
    + list(DROPIN_COPIES)
    + [f"Transport/Plugins/SshNet/{name}" for name in SSH_NET_DLLS]
)

# Types each half declares. Neither half may name the other's.
TRANSPORT_TYPES = ["CommsTunnelBridge", "SshMqttTunnel", "SshTunnelSettings", "SshHopSettings", "TunnelConfig"]
HOOK_TYPES = ["AiGestureHook", "GestureEventParser", "Envelope", "SeenEvents"]

# Gesture names, current and old. None may be spelled out in the drop-in (4.6).
GESTURE_WORDS = ["circle", "caret", "arc", "z", "infinity", "triangle", "star", "square", "zigzag", "line"]

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_COMMENT_LINE = re.compile(r"//[^\n]*")
_STRING = re.compile(r'"([^"\\]*)"')


def _strip_comments(text):
    """C# with comments removed, so prose about the other half is not a reference."""
    return _COMMENT_LINE.sub("", _COMMENT_BLOCK.sub("", text))


def _code(path):
    return _strip_comments(path.read_text(encoding="utf-8"))


def _names(code, types):
    return [t for t in types if re.search(rf"\b{t}\b", code)]


def _gesture_literals(code):
    return sorted({s for s in _STRING.findall(code) if s.lower() in GESTURE_WORDS})


@pytest.mark.parametrize("dropin_path", sorted(DROPIN_COPIES))
def test_dropin_copy_is_identical_to_the_tunnelled_project(dropin_path):
    copy = _DROPIN / dropin_path
    source = _TUNNELLED / DROPIN_COPIES[dropin_path]

    assert copy.exists(), f"{copy} is missing"
    assert copy.read_bytes() == source.read_bytes(), (
        f"comms-visualiser/{dropin_path} has drifted from tunnelled-visualiser. Fix the source "
        "in tunnelled-visualiser, then copy it across."
    )


def test_dropin_dlls_are_the_tunnelled_projects_ssh_net_dlls():
    plugins = _DROPIN / "Transport" / "Plugins" / "SshNet"
    tunnelled = _COMMS / "phone" / "tunnelled-visualiser" / "Assets" / "Plugins" / "SshNet"

    for name in SSH_NET_DLLS:
        assert (plugins / name).read_bytes() == (tunnelled / name).read_bytes(), name


def test_dropin_holds_exactly_the_planned_files():
    """Catches a stray TunnelledVisualiser.cs (it bootstraps a second tunnel), an .asmdef,
    System.Memory or System.Buffers (they clash with Unity's own), or anything unplanned."""
    found = sorted(
        p.relative_to(_DROPIN).as_posix()
        for p in _DROPIN.rglob("*")
        if p.is_file() and p.suffix != ".meta"
    )

    assert found == DROPIN_FILES


@pytest.mark.parametrize(
    "half, forbidden",
    [("Transport", HOOK_TYPES), ("Hook", TRANSPORT_TYPES)],
    ids=["transport-names-no-hook-type", "hook-names-no-transport-type"],
)
def test_dropin_halves_depend_only_on_shared(half, forbidden):
    """Fallback A deletes Transport/; the hook must still compile without it."""
    files = sorted((_DROPIN / half).glob("*.cs"))
    assert files, f"no C# found in comms-visualiser/{half}"

    for path in files:
        named = _names(_code(path), forbidden)
        assert not named, f"{half}/{path.name} names {named} from the other half"


def test_no_dropin_script_starts_itself():
    """The Vis scene wires these in; a self-starting one would fight the bridge for 1883."""
    for path in sorted(_DROPIN.rglob("*.cs")):
        assert "RuntimeInitializeOnLoadMethod" not in _code(path), path.name


def test_dropin_keeps_no_list_of_gesture_names():
    """The Vis enum is the only list. A literal like "circle" here is a second copy."""
    for path in sorted(_DROPIN.rglob("*.cs")):
        listed = _gesture_literals(_code(path))
        assert not listed, f"{path.name} spells out gesture names {listed}"


def test_dropin_link_xml_parses_and_preserves_ssh_net():
    """IL2CPP strips SSH.NET's algorithms without it. UnityLinker aborts on "--" in a
    comment, and so does this parser."""
    root = ElementTree.parse(_DROPIN / "Transport" / "link.xml").getroot()
    preserved = {a.get("fullname") for a in root.iter("assembly") if a.get("preserve") == "all"}

    assert preserved >= {name[: -len(".dll")] for name in SSH_NET_DLLS} | {"Newtonsoft.Json"}


def test_the_source_detectors_match_what_they_claim_to():
    """The dependency and name checks are regexes, so prove they can fire."""
    code = _strip_comments('var e = new Envelope(); // TunnelConfig in prose\nvar g = "Circle";')

    assert _names(code, HOOK_TYPES) == ["Envelope"]
    assert _names(code, TRANSPORT_TYPES) == []
    assert _gesture_literals(code) == ["Circle"]
