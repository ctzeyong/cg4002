"""Hardware's gesture detector in the firmware is an unchanged copy of Hardware's own files."""

import pathlib

import pytest

COMMS = pathlib.Path(__file__).resolve().parents[2]
FIRMWARE = COMMS / "firebeetle"
HARDWARE = COMMS.parent / "Hardware" / "Data Collection" / "esp32_continuous_pipeline_test"
COPIED = ("gesture_core.h", "segments.h", "model_data.h")


def lines(path):
    # the team repo checks out with CRLF, so compare the text, not the line endings
    return path.read_bytes().replace(b"\r\n", b"\n")


@pytest.mark.parametrize("name", COPIED)
def test_the_firmwares_detector_file_is_hardwares_unchanged(name):
    source = HARDWARE / name
    assert source.exists(), f"{source} is missing: merge main, which has Hardware's detector"
    assert lines(FIRMWARE / name) == lines(source), (
        f"firebeetle/{name} differs from Hardware's copy: copy it across again"
    )


def test_the_firmware_has_the_trained_model_not_the_placeholder():
    assert b"constexpr bool MODEL_READY = true;" in lines(FIRMWARE / "model_data.h")
