"""Pin which secret `TunnelConfig` is allowed to persist, and which it is not.

`phone/tunnelled-visualiser` writes `tunnel_config.json` into the phone's
`Application.persistentDataPath`, so every public field on `TunnelConfig` ends up
on the device's filesystem — readable over `adb` on a development build. The two
SSH hops' passwords are treated differently on purpose (2026-09-17):

  - the **board** password is stored. The Ultra96 is a shared lab box whose
    `xilinx` password other teams already hold, so persisting it leaks nothing
    that is not already shared and it halves what is typed at bring-up.
  - the **jump-host** password is not, and must not be. It is the operator's NUS
    account credential, and writing it to disk is the trade `ultra96_setup.md`'s
    Fallback B was rejected for.

That asymmetry is a decision, not an accident, and nothing in the C# enforces it —
adding one more `public string` to a `[Serializable]` class is a one-line change
that compiles, runs, and quietly puts an institutional credential on a phone. So
it is enforced here instead.

This is a source-parsing test and touches no Python under test. If it fails,
the fix is to take the field back out, not to widen `ALLOWED_SECRET`.
"""

import pathlib
import re

import pytest

_COMMS = pathlib.Path(__file__).resolve().parents[2]

# Both copies: the tunnelled visualiser's and the one the Vis app imports
# (phone/comms-visualiser). test_unity_shared_scripts.py keeps them byte-identical; this
# checks each anyway, so the rule holds while a copy is mid-change.
CONFIGS = [
    _COMMS / "phone" / "tunnelled-visualiser" / "Assets" / "Scripts" / "TunnelConfig.cs",
    _COMMS / "phone" / "comms-visualiser" / "Transport" / "TunnelConfig.cs",
]
_IDS = ["tunnelled-visualiser", "comms-visualiser"]

ALLOWED_SECRET = "BoardPassword"

# `public <type> <Name> =` or `public <type> <Name>;`. The `(?!>)` keeps expression-bodied
# properties (`public static string KeyDirectory => ...`) out, and a trailing `(` means it
# is a method, which this pattern already excludes.
_FIELD = re.compile(r"^\s*public\s+(?:const\s+|static\s+|readonly\s+)*[\w<>\[\]?.]+\s+(\w+)\s*(?:=(?!>)|;)")


def _public_fields(config):
    """Public field names declared on TunnelConfig, comments stripped.

    Comments have to go first: the class doc-comment discusses the jump-host
    password at length to explain why it is absent, and a test that searched the
    file's text would fail on the very prose that documents the rule.
    """
    names = []
    for line in config.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(("//", "/*", "*", "///")):
            continue
        match = _FIELD.match(line)
        if match:
            names.append(match.group(1))
    return names


@pytest.mark.parametrize("config", CONFIGS, ids=_IDS)
def test_config_file_exists(config):
    assert config.exists(), f"{config} is missing"


@pytest.mark.parametrize("config", CONFIGS, ids=_IDS)
def test_board_password_is_persisted(config):
    """The half of the decision that is easy to lose in a refactor."""
    assert ALLOWED_SECRET in _public_fields(config), (
        f"{ALLOWED_SECRET} is gone from TunnelConfig. It is deliberately persisted so "
        "the board hop needs no typing at bring-up; see the class doc comment."
    )


@pytest.mark.parametrize("config", CONFIGS, ids=_IDS)
def test_no_other_secret_is_persisted(config):
    """Anything password- or passphrase-shaped besides the board password is a leak."""
    offenders = [
        name
        for name in _public_fields(config)
        if name != ALLOWED_SECRET
        and ("password" in name.lower() or "passphrase" in name.lower())
    ]

    assert not offenders, (
        f"TunnelConfig would persist {offenders} to tunnel_config.json on the phone. "
        "Only the board password may be stored - the jump-host password is an NUS "
        "account credential and is held in memory for the process lifetime instead. "
        "See notes/tunnelled_visualiser.md -> Credentials."
    )


@pytest.mark.parametrize(
    "line",
    [
        "        public string JumpPassword = \"\";",
        "        public string JumpPassphrase = \"\";",
        "        public string BoardPassphrase = \"\";",
    ],
)
def test_detector_catches_a_smuggled_secret(line):
    """The regex is the whole test, so prove it matches what it claims to.

    Without this, a pattern that silently stopped matching anything would leave
    `test_no_other_secret_is_persisted` passing forever on an empty field list —
    the shape of green-while-broken that CLAUDE.md warns the suite has hit twice.
    """
    match = _FIELD.match(line)
    assert match is not None, f"the field detector missed {line!r}"
    assert "password" in match.group(1).lower() or "passphrase" in match.group(1).lower()


def test_detector_ignores_properties_and_methods():
    assert _FIELD.match("        public static string KeyDirectory => Foo;") is None
    assert _FIELD.match("        public void Save()") is None
