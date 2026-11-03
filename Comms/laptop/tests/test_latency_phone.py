"""Latency measurement, stage 2: the phone's clock offset over adb, and the phone hop.

notes/latency.md 3.3 and 3.7. Nothing here runs adb or Unity: the clock maths is fed fake
exchanges, and the C# is checked as source against the report's parser, because the APK
cannot be built from here.
"""

import re
import sys
from pathlib import Path

import pytest

COMMS = Path(__file__).resolve().parents[2]
TOOLS = COMMS / "scripts" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import latency_report  # noqa: E402
import phone_clock  # noqa: E402

PREFIX = "2026-10-02 12:00:00,000Z [x] "


def lines(*messages):
    return [PREFIX + m for m in messages]


def logcat(t, tag, event_id, player=1):
    """One line as `adb logcat -v epoch -v usec` prints AddLine's Debug.Log copy."""
    return (
        f"  {t:.6f}  12345  12367 I Unity   : {tag} 01:31:20 player={player} {event_id} "
        '{"type":"AI_EVENT","player":1,"seq":0,"gesture":"circle"}'
    )


def sample(phone_time, laptop_mid, rtt):
    return {"phone_time": phone_time, "laptop_time": laptop_mid,
            "offset": phone_time - laptop_mid, "rtt": rtt}


# --- phone_clock -------------------------------------------------------------


def test_measure_keeps_the_lowest_rtt_exchange_and_its_sign():
    """Phone clock = laptop + 500 s. A slow exchange's midpoint is off; the fast one is not."""
    exchanges = iter([
        (0.0, 500.0, 0.0),        # warm-up, discarded
        (1.000, 501.030, 1.100),  # rtt 100 ms, midpoint 1.05: offset 499.98
        (2.000, 502.001, 2.002),  # rtt 2 ms, midpoint 2.001: offset 500.000
        (3.000, 503.050, 3.080),  # rtt 80 ms
    ])
    result = phone_clock.measure(lambda: next(exchanges), samples=3)

    assert result["rtt"] == pytest.approx(0.002)
    assert result["offset"] == pytest.approx(500.0)
    # phone_time - offset is the laptop time: the sign latency_report relies on.
    assert result["phone_time"] - result["offset"] == pytest.approx(2.001)


@pytest.mark.parametrize("text", ["", "\n", "$EPOCHREALTIME\n", "not a number"])
def test_a_shell_without_epochrealtime_fails_clearly(text):
    with pytest.raises(phone_clock.ClockError, match="EPOCHREALTIME"):
        phone_clock.parse_phone_time(text)


def test_epochrealtime_output_parses_with_its_line_ending():
    assert phone_clock.parse_phone_time("1790875880.676178\r\n") == 1790875880.676178


# --- PhoneClock: interpolation -------------------------------------------------


def test_the_offset_is_interpolated_between_the_two_syncs():
    clock = latency_report.PhoneClock(sample(1000.0, 500.0, 0.002), sample(1100.0, 599.99, 0.004))
    # offset 500.000 at the start, 500.010 at the end, so 500.005 halfway.
    assert clock.offset_at(1050.0) == pytest.approx(500.005)
    assert clock.bound_ms == pytest.approx(2.0)  # the worse sync's rtt / 2
    assert clock.drift_ms == pytest.approx(10.0)


def test_a_start_sync_alone_gives_a_constant_offset():
    clock = latency_report.PhoneClock(sample(1000.0, 500.0, 0.002))
    assert clock.offset_at(5000.0) == pytest.approx(500.0)


# --- the phone join ---------------------------------------------------------------

# Board = laptop + 1000 s, phone = laptop + 500 s.
B_RUN = lines(
    "uplink rx: total=5 rate=5.0 msg/s, 0 AI events sent, offset=1000.000000 rtt=2.000ms",
    "trigger node=1 seq=10 t_laptop_rx=50.000000 t_rx=1050.004000",
    "AI event seq=0 player=1 key='c' gesture=circle mana=10 t_event=1050.005000 -> game/gesture/1",
    "published ultra96-relay:abc:0 to game/gesture/1 t=1050.060000",
)
PHONE_CLOCK = latency_report.PhoneClock(sample(540.0, 40.0, 0.003))


def test_phone_offset_is_applied_with_the_right_sign():
    """Applied at laptop time 50.040: 35 ms after the event, 40 ms after the laptop got the
    packet. A flipped sign would move both by twice the offset, 1000 s."""
    phone = [logcat(550.040, "[tunnelled]", "ultra96-relay:abc:0")]
    run = latency_report.report(B_RUN, "B", phone_clock=PHONE_CLOCK, logcat_lines=phone)

    hops = run.events[0].hops
    assert hops["event_to_phone"] == pytest.approx(35.0)
    assert hops["laptop_rx_to_phone"] == pytest.approx(40.0)


def test_a_run_joins_the_phone_through_the_laptops_publish_line():
    board = lines(
        "uplink rx: total=1 rate=1.0 msg/s, 0 AI events sent, offset=1000.000000 rtt=2.000ms",
        "trigger node=1 seq=1 t_laptop_rx=10.000000 t_rx=1010.003000",
        "AI event seq=0 player=1 key='c' gesture=circle mana=10 t_event=1010.004000",
    )
    laptop = lines(
        "downlink seq=0 t=10.010000 -> game/gesture/1: {'type': 'AI_EVENT', 'player': 1, "
        "'seq': 0, 'gesture': 'circle', 'mana_cost': 10, 'timestamp': 1010.004}",
        "published laptop-relay:s:0 to game/gesture/1 t=10.012000",
    )
    phone = [logcat(510.020, "[demo]", "laptop-relay:s:0")]
    run = latency_report.report(board, "A", laptop, PHONE_CLOCK, phone)

    hops = run.events[0].hops
    assert hops["laptop_downlink_to_phone"] == pytest.approx(10.0)
    assert hops["event_to_phone"] == pytest.approx(16.0)
    assert hops["laptop_rx_to_phone"] == pytest.approx(20.0)


def test_a_repeated_event_id_is_counted_once():
    """QoS 1 can deliver twice; a restarted app forgets what it has seen."""
    phone = [
        logcat(550.040, "[tunnelled]", "ultra96-relay:abc:0"),
        logcat(550.900, "[tunnelled]", "ultra96-relay:abc:0"),
    ]
    run = latency_report.report(B_RUN, "B", phone_clock=PHONE_CLOCK, logcat_lines=phone)

    assert run.counts.phone_duplicates == 1
    assert run.events[0].hops["event_to_phone"] == pytest.approx(35.0)  # the first one


def test_missing_and_unknown_phone_lines_are_counted():
    phone = [logcat(550.040, "[tunnelled]", "ultra96-relay:other:7")]
    run = latency_report.report(B_RUN, "B", phone_clock=PHONE_CLOCK, logcat_lines=phone)

    assert run.counts.phone_lines_unmatched == 1
    assert run.counts.events_without_phone == 1


def test_a_wrapped_logcat_buffer_is_reported():
    """The dump starting after the first event means its earliest lines were overwritten."""
    kept = [logcat(549.0, "[tunnelled]", "x:y:1"), logcat(550.040, "[tunnelled]", "ultra96-relay:abc:0")]
    wrapped = [logcat(550.040, "[tunnelled]", "ultra96-relay:abc:0")]  # starts after 50.005

    for phone, expected in ((kept, False), (wrapped, True)):
        run = latency_report.report(B_RUN, "B", phone_clock=PHONE_CLOCK, logcat_lines=phone)
        assert latency_report.buffer_overwritten(run) is expected


def test_a_wrong_phone_offset_is_flagged_by_causality():
    import io

    early = [logcat(550.000, "[tunnelled]", "ultra96-relay:abc:0")]  # before the event exists
    run = latency_report.report(B_RUN, "B", phone_clock=PHONE_CLOCK, logcat_lines=early)
    out = io.StringIO()
    latency_report.print_summary(run, "B", out=out)
    assert "NEGATIVE" in out.getvalue()


# --- the C# side of the contract ------------------------------------------------

APPS = {
    "[demo]": COMMS / "phone/demo-visualiser/Assets/Scripts/MqttVisualiser.cs",
    "[tunnelled]": COMMS / "phone/tunnelled-visualiser/Assets/Scripts/TunnelledVisualiser.cs",
}


@pytest.mark.parametrize("tag, source", APPS.items())
def test_each_visualiser_copies_its_lines_to_logcat_under_a_tag_the_report_reads(tag, source):
    """Both apps must Debug.Log every AddLine with their LogTag, and print the event line
    the report parses. The APK cannot be built here, so the source is the contract."""
    code = source.read_text(encoding="utf-8")

    assert f'private const string LogTag = "{tag}";' in code
    assert tag in latency_report.PHONE_TAGS
    assert 'Debug.Log($"{LogTag} {stamped}");' in code
    assert re.search(r'AddLine\(\$"player=\{player\?\.ToString\(\) \?\? "\?"\} \{eventId\} ', code)

    line = logcat(550.040, tag, "src:session:3")
    match = latency_report.PHONE_EVENT.match(line)
    assert match and match["event_id"] == "src:session:3"


def test_run_adb_survives_bytes_the_windows_code_page_cannot_decode():
    """2 Oct: a 0x81 byte 10 MB into a logcat dump killed `finish` under cp1252, and the
    dump came back as None. The fake adb here is Python printing that byte."""
    script = "import sys; sys.stdout.buffer.write(b'[demo] ok ' + bytes([0x81]) + b' end')"
    out = phone_clock.run_adb(sys.executable, "-c", script)
    assert isinstance(out, str)
    assert out.startswith("[demo] ok ") and out.endswith(" end")
