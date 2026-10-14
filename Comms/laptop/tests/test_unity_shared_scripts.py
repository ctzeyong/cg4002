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
