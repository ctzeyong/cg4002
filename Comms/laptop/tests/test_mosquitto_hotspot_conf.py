"""laptop/mosquitto_hotspot.conf against mosquitto.conf: only the listeners and include_dir differ."""

from pathlib import Path

LAPTOP = Path(__file__).resolve().parents[1]
SCRIPT = LAPTOP.parent / "scripts" / "run_pipeline_a_hotspot.ps1"


def directives(name):
    """The non-comment, non-blank lines of a config, in order."""
    lines = (LAPTOP / name).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def split(lines):
    listeners = [line for line in lines if line.startswith("listener ")]
    rest = [line for line in lines if not line.startswith(("listener ", "include_dir "))]
    return listeners, rest


def test_listens_on_loopback_and_the_lease_only():
    listeners, _ = split(directives("mosquitto_hotspot.conf"))
    assert listeners == ["listener 1883 127.0.0.1", "listener 1883 HOTSPOT_LEASE"]


def test_no_include_dir_so_the_tls_listener_on_the_laptop_ap_never_loads():
    assert not any(line.startswith("include_dir") for line in directives("mosquitto_hotspot.conf"))


def test_every_other_setting_matches_the_campus_config():
    _, hotspot_rest = split(directives("mosquitto_hotspot.conf"))
    _, campus_rest = split(directives("mosquitto.conf"))
    assert hotspot_rest == campus_rest


def test_the_script_replaces_exactly_the_lines_the_template_has():
    script = SCRIPT.read_text(encoding="utf-8")
    template = directives("mosquitto_hotspot.conf")
    for line in ("listener 1883 HOTSPOT_LEASE", "log_dest stdout"):
        assert template.count(line) == 1
        assert f'"{line}"' in script
