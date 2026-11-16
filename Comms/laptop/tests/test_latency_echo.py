"""Tests for timing the crossing between the board and a phone from an echo capture.

The capture lines are made up in the shape of the board's `mosquitto_sub -F '%U %t %p'`. Phone
clocks run 100 s ahead of the board unless a test says otherwise.
"""

import csv
import json
import math
import re
import sys
from pathlib import Path

import pytest

COMMS = Path(__file__).resolve().parents[2]
TOOLS = COMMS / "scripts" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import latency_report  # noqa: E402

GESTURE_ECHO_CS = COMMS / "phone/comms-visualiser/Hook/GestureEcho.cs"


def gesture(t, seq, t_event, player=1, session="s1"):
    """The capture's line for one gesture the board published."""
    env = {
        "event_id": f"ultra96-relay:{session}:{seq}", "source": "ultra96-relay", "seq": seq,
        "ts": t_event,
        "payload": {"type": "AI_EVENT", "player": player, "seq": seq, "gesture": "mage",
                    "timestamp": t_event},
    }
    return f"{t:.9f} game/gesture/{player} {json.dumps(env)}"


def echo(t, seq, t_event, t_phone, phone="phone-visualiser-a", player=1):
    """The capture's line for one phone's echo of a gesture, with t_phone in the envelope's ts."""
    env = {
        "event_id": f"{phone}:x:{seq}", "source": phone, "seq": seq, "ts": t_phone,
        "payload": {"seq": seq, "t_event": t_event},
    }
    return f"{t:.9f} viz/echo/{player} {json.dumps(env)}"


def run_of(*lines, events=None):
    return latency_report.echo_report(list(lines), events)


def test_a_tested_window_gives_each_leg_with_the_offsets_sign():
    """Pair b's fast way in narrows the offset, so pair a's legs aren't an even split."""
    # pair a is made at 1000.000, the phone reads at .020 and the echo is back at .035
    run = run_of(
        gesture(1000.002, 7, 1000.000),
        echo(1000.035, 7, 1000.000, t_phone=1100.020),  # [99.985, 100.020]
        gesture(1010.002, 8, 1010.000),
        echo(1010.040, 8, 1010.000, t_phone=1110.004),  # [99.964, 100.004]
    )

    a, b = run.pairs
    assert a.window_status == b.window_status == "tested"
    # the overlap is [99.985, 100.004]
    assert a.offset == pytest.approx(99.9945)
    assert a.bound == pytest.approx(0.0095)
    assert abs(a.offset - 100.0) <= a.bound  # the truth is inside the bound
    # on the board's clock the phone read at 1100.020 - 99.9945 = 1000.0255
    assert a.legs["event_to_phone"] == pytest.approx(25.5)
    assert a.legs["phone_to_board"] == pytest.approx(9.5)
    assert a.legs["round_trip"] == pytest.approx(33.0)
    assert a.legs["event_to_phone"] + a.legs["phone_to_board"] == pytest.approx(35.0)


def test_a_window_of_one_pair_gives_its_round_trip_but_no_one_way_leg():
    """A window with only one echo gives its round trip but no one-way times."""
    event = latency_report.Event(0, "1", 1000.000, event_id="ultra96-relay:s1:7")
    event.hops["laptop_rx_to_event"] = 12.0
    run = run_of(
        gesture(1000.002, 7, 1000.000),
        echo(1000.035, 7, 1000.000, t_phone=1100.020),
        events=[event],
    )

    (pair,) = run.pairs
    assert pair.window_status == "untested"  # one pair can't show the offset moved
    # the offset lies in [1100.020 - 1000.035, 1100.020 - 1000.000] = [99.985, 100.020]
    assert pair.offset == pytest.approx(100.0025)
    assert pair.bound == pytest.approx(0.0175)
    assert pair.legs == {"round_trip": pytest.approx(33.0)}  # so no laptop rx to phone leg either
    assert run.unjoined == 0  # it joined but has no one-way leg to add to


def test_only_tested_windows_reach_the_summary_and_csv_legs(tmp_path, capsys):
    """The one-way legs and the worst bound come from the tested window only."""
    # a tested window with bound 9.5 ms, then a one-pair window with bound 17.5 ms 40 s later
    capture = tmp_path / "echo.log"
    capture.write_text("\n".join([
        gesture(1000.002, 7, 1000.000),
        echo(1000.035, 7, 1000.000, t_phone=1100.020),
        gesture(1010.002, 8, 1010.000),
        echo(1010.040, 8, 1010.000, t_phone=1110.004),
        gesture(1040.002, 9, 1040.000),
        echo(1040.035, 9, 1040.000, t_phone=1140.020),
    ]) + "\n", encoding="utf-8")
    out_csv = tmp_path / "e.csv"

    latency_report.main(["--config", "B", "--echo", str(capture), "--csv", str(out_csv)])
    out = capsys.readouterr().out

    assert "windows: 1 tested, 1 untested (one pair, round trip only), 0 rejected" in out
    assert "worst bound 9.500 ms" in out  # not the one-pair window's 17.500
    # n is the fourth field from the end of each leg's row
    n = {label: line.split()[-4] for line in out.splitlines()
         for _, label in latency_report.ECHO_LEGS if line.strip().startswith(label)}
    assert n == {
        "phone -> board": "2",  # the tested window's two pairs only
        "4 -> phone  AI event -> phone": "2",
        "round trip from the capture, no offset": "3",  # every pair
    }

    with open(out_csv, newline="", encoding="utf-8") as f:
        by_seq = {row["seq"]: row for row in csv.DictReader(f)}
    assert by_seq["9"]["window_status"] == "untested"
    assert float(by_seq["9"]["offset"]) == pytest.approx(100.0025)  # still kept
    assert by_seq["9"]["phone_to_board"] == ""
    assert by_seq["9"]["event_to_phone"] == ""
    assert float(by_seq["9"]["round_trip"]) == pytest.approx(33.0)
    assert float(by_seq["7"]["phone_to_board"]) == pytest.approx(9.5)


def test_a_capture_that_prints_after_the_phone_gives_no_negative_leg():
    """The capture can print a gesture after the phone gets it, so no leg starts at the capture."""
    # the phone handles each gesture 20 ms after it is made, the capture prints it 10 ms later still
    run = run_of(
        gesture(1000.030, 1, 1000.000),
        echo(1000.040, 1, 1000.000, t_phone=1100.020),
        gesture(1010.030, 2, 1010.000),
        echo(1010.040, 2, 1010.000, t_phone=1110.020),
    )

    for p in run.pairs:
        assert p.window_status == "tested"
        assert set(p.legs) == {"event_to_phone", "phone_to_board", "round_trip"}
        assert p.legs["event_to_phone"] == pytest.approx(20.0)
        assert p.legs["phone_to_board"] == pytest.approx(20.0)


def test_a_window_intersects_its_pairs_and_is_tighter_than_any_one():
    """A fast way in for one pair and a fast way back for the other pin the offset to 4 ms."""
    run = run_of(
        gesture(1000.001, 1, 1000.000),
        echo(1000.040, 1, 1000.000, t_phone=1100.002),  # interval [99.962, 100.002]
        gesture(1010.001, 2, 1010.000),
        echo(1010.032, 2, 1010.000, t_phone=1110.030),  # interval [99.998, 100.030]
    )

    a, b = run.pairs
    assert a.window_status == b.window_status == "tested"
    assert a.offset == pytest.approx(100.000)
    assert a.bound == pytest.approx(0.002)  # 0.020 and 0.016 for each pair alone
    assert b.legs["event_to_phone"] == pytest.approx(30.0)


def test_a_window_whose_offset_moved_is_rejected_but_keeps_its_round_trips():
    """Same pairs as above, but the phone's clock stepped 50 ms forward between them."""
    run = run_of(
        gesture(1000.001, 1, 1000.000),
        echo(1000.040, 1, 1000.000, t_phone=1100.002),  # [99.962, 100.002]
        gesture(1010.001, 2, 1010.000),
        echo(1010.032, 2, 1010.000, t_phone=1110.080),  # [100.048, 100.080], so no overlap
    )

    assert all(p.window_status == "rejected" for p in run.pairs)
    assert all(p.offset is None and "event_to_phone" not in p.legs for p in run.pairs)
    assert [round(p.legs["round_trip"]) for p in run.pairs] == [39, 31]
    assert run.phones["phone-visualiser-a"]["windows rejected"] == 1


def test_a_board_restart_reuses_seq_but_each_echo_pairs_with_its_own_gesture():
    """Each echo pairs with its own gesture when a board restart reuses seq 3."""
    # the first echo comes back after the second gesture, so seq alone gives a 10 ms round trip
    run = run_of(
        gesture(1000.002, 3, 1000.000, session="s1"),
        gesture(1020.002, 3, 1020.000, session="s2"),
        echo(1020.012, 3, 1000.000, t_phone=1120.005),
        echo(1020.040, 3, 1020.000, t_phone=1120.020),
    )

    late, fresh = run.pairs
    assert late.gesture.event_id == "ultra96-relay:s1:3"
    assert late.legs["round_trip"] == pytest.approx(20010.0)
    assert fresh.gesture.event_id == "ultra96-relay:s2:3"
    assert fresh.legs["round_trip"] == pytest.approx(38.0)


def test_a_gesture_captured_twice_is_ambiguous_and_its_echo_unused():
    """A gesture captured twice, as when the publisher resends after a lost PUBACK, is ambiguous."""
    run = run_of(
        gesture(999.002, 4, 999.000),
        echo(999.030, 4, 999.000, t_phone=1099.015),
        gesture(1000.002, 5, 1000.000),
        gesture(1001.502, 5, 1000.000),  # the resend
        echo(1001.510, 5, 1000.000, t_phone=1101.505),
        gesture(1002.002, 6, 1002.000),
        echo(1002.030, 6, 1002.000, t_phone=1102.015),
    )

    assert [p.gesture.seq for p in run.pairs] == [4, 6]
    counts = run.phones["phone-visualiser-a"]
    assert counts["ambiguous"] == 1
    assert counts["gestures with no echo"] == 0  # 5 was answered, just not usably


def test_two_echoes_from_one_phone_for_one_gesture_are_ambiguous():
    """Two echoes of one gesture from one phone are both left out."""
    run = run_of(
        gesture(1000.002, 5, 1000.000),
        echo(1000.030, 5, 1000.000, t_phone=1100.015),
        echo(1000.050, 5, 1000.000, t_phone=1100.040),
    )

    assert run.pairs == []
    assert run.phones["phone-visualiser-a"]["ambiguous"] == 2


def test_two_phones_on_one_player_keep_their_own_clocks():
    """Two phones on player 1 keep their own clocks, b 50 s behind the board and a 100 s ahead."""
    run = run_of(
        gesture(1000.002, 1, 1000.000),
        echo(1000.030, 1, 1000.000, t_phone=1100.015, phone="phone-visualiser-a"),
        echo(1000.032, 1, 1000.000, t_phone=950.016, phone="phone-visualiser-b"),
    )

    offsets = {p.echo.phone: p.offset for p in run.pairs}
    assert offsets["phone-visualiser-a"] == pytest.approx(100.0, abs=0.02)
    assert offsets["phone-visualiser-b"] == pytest.approx(-50.0, abs=0.02)


def test_echo_alone_runs_and_counts_what_did_not_pair(tmp_path, capsys):
    """--echo runs without --board, and counts a missed gesture only inside the phone's span."""
    # gesture 1 comes before the phone's first echo so it isn't a miss, and gesture 3 is
    capture = tmp_path / "echo.log"
    capture.write_text("\n".join([
        gesture(999.002, 1, 999.000),
        gesture(1000.002, 2, 1000.000),
        echo(1000.030, 2, 1000.000, t_phone=1100.015),
        gesture(1005.002, 3, 1005.000),
        gesture(1010.002, 4, 1010.000),
        echo(1010.030, 4, 1010.000, t_phone=1110.015),
        echo(1011.000, 9, 1011.000, t_phone=1111.000),  # nothing captured with this seq
        "not a capture line",
    ]) + "\n", encoding="utf-8")

    latency_report.main(["--config", "B", "--echo", str(capture), "--csv", str(tmp_path / "e.csv")])
    out = capsys.readouterr().out

    assert "echo capture: 4 gestures, 3 echoes, 1 unreadable lines" in out
    assert "phone phone-visualiser-a (player 1): 2 pairs" in out
    assert "gestures with no echo: 1" in out
    assert "echoes with no gesture: 1" in out
    rows = (tmp_path / "e.csv").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3  # header + one row per pair


@pytest.mark.parametrize("seq", [[1], {"n": 1}, True, 1.0, "1"])
def test_a_seq_that_is_not_an_integer_makes_the_line_unreadable(seq):
    """A list once crashed the whole report, and True paired with seq 1."""
    good = gesture(1000.002, 1, 1000.000)
    bad = json.loads(echo(1000.030, 1, 1000.000, t_phone=1100.015).split(" ", 2)[2])
    bad["payload"]["seq"] = seq
    run = run_of(good, f"1000.030 viz/echo/1 {json.dumps(bad)}")

    assert run.pairs == [] and run.unreadable == 1


@pytest.mark.parametrize("value", [["x"], {"a": 1}, 5])
@pytest.mark.parametrize("which", ["gesture event_id", "echo source"])
def test_a_key_field_that_is_not_a_string_makes_the_line_unreadable(which, value):
    """A list once crashed the whole report, so event_id and source must be strings."""
    lines = [gesture(1000.002, 1, 1000.000), echo(1000.030, 1, 1000.000, t_phone=1100.015)]
    index, field = (0, "event_id") if which == "gesture event_id" else (1, "source")
    t, topic, raw = lines[index].split(" ", 2)
    env = json.loads(raw)
    env[field] = value
    lines[index] = f"{t} {topic} {json.dumps(env)}"
    run = run_of(*lines)

    assert run.pairs == [] and run.unreadable == 1


@pytest.mark.parametrize(
    "value",
    [float("nan"), "NaN", float("inf"), True, "1110.020", 10**1000],
    ids=["nan", "nan string", "inf", "true", "number string", "too big for a float"],
)
@pytest.mark.parametrize("field", ["gesture timestamp", "echo t_event", "echo ts"])
def test_a_number_that_is_not_a_finite_number_makes_the_line_unreadable(field, value):
    """One bad number spoils neither its window nor the good pairs in it."""
    lines = [
        gesture(1000.002, 1, 1000.000), echo(1000.035, 1, 1000.000, t_phone=1100.020),
        gesture(1010.002, 2, 1010.000), echo(1010.040, 2, 1010.000, t_phone=1110.004),
        gesture(1020.002, 3, 1020.000), echo(1020.035, 3, 1020.000, t_phone=1120.020),
    ]
    index = 4 if field == "gesture timestamp" else 5
    t, topic, raw = lines[index].split(" ", 2)
    env = json.loads(raw)
    if field == "echo ts":
        env["ts"] = value
    else:
        env["payload"][field.split()[1]] = value
    lines[index] = f"{t} {topic} {json.dumps(env)}"
    run = run_of(*lines)

    assert run.unreadable == 1
    assert [p.window_status for p in run.pairs] == ["tested", "tested"]
    assert all(math.isfinite(v) for p in run.pairs for v in p.legs.values())


@pytest.mark.parametrize("t", ["nan", "inf", "x"])
def test_a_capture_time_that_is_not_a_finite_number_makes_the_line_unreadable(t):
    """The capture's own time is checked the same way."""
    bad = echo(1000.035, 1, 1000.000, t_phone=1100.020).split(" ", 1)[1]
    run = run_of(gesture(1000.002, 1, 1000.000), f"{t} {bad}")

    assert run.pairs == [] and run.unreadable == 1


def test_json_nested_too_deep_makes_the_line_unreadable():
    """A line nested deeper than the parser can recurse is unreadable, not a crash."""
    run = run_of(gesture(1000.002, 1, 1000.000), "1000.030 viz/echo/1 " + "[" * 100000)

    assert run.unreadable == 1


def test_echo_is_configuration_b_only(tmp_path):
    capture = tmp_path / "echo.log"
    capture.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit):
        latency_report.main(["--config", "A", "--echo", str(capture)])


def test_with_the_board_log_a_pair_adds_laptop_rx_to_phone():
    """With the board log, laptop rx to phone is the board's 1 to 4 hop plus the pair's 4 to phone."""
    event = latency_report.Event(0, "1", 1000.000, event_id="ultra96-relay:s1:7")
    event.hops["laptop_rx_to_event"] = 12.0
    run = run_of(
        gesture(1000.002, 7, 1000.000),
        echo(1000.035, 7, 1000.000, t_phone=1100.020),
        gesture(1001.002, 8, 1001.000),  # published, but not in this board log
        echo(1001.035, 8, 1001.000, t_phone=1101.020),
        events=[event],
    )

    joined, other = run.pairs
    assert joined.legs["laptop_rx_to_phone"] == pytest.approx(12.0 + joined.legs["event_to_phone"])
    assert "laptop_rx_to_phone" not in other.legs
    assert run.unjoined == 1


def test_the_board_publishes_what_the_parser_reads():
    """A gesture built by the board's own code, not by this file's helper."""
    import ai_events
    import envelope

    event = ai_events.make_event(2, 11)
    env = envelope.build_envelope(event, 11)
    line = f"1000.000000001 {envelope.gesture_topic(2)} {json.dumps(env)}"

    (g,), echoes, unreadable = latency_report.parse_capture([line])
    assert (g.event_id, g.seq, g.t_event) == (env["event_id"], 11, event["timestamp"])
    assert echoes == [] and unreadable == 0


def test_the_phone_builds_the_echo_the_parser_reads():
    """GestureEcho.cs writes the fields, topic and filter the parser reads."""
    code = GESTURE_ECHO_CS.read_text(encoding="utf-8")

    assert re.findall(r'\["(\w+)"\] = ', code) == ["seq", "t_event"]
    assert re.findall(r'payload\["(\w+)"\]', code) == ["seq", "timestamp"]  # and the fields it reads
    assert 'Topic(int player) => $"viz/echo/{player}";' in code
    assert 'GestureFilter = "game/gesture/+";' in code

    payload = dict.fromkeys(["seq", "t_event"])
    payload.update(seq=4, t_event=1000.0)
    env = {"event_id": "p:x:1", "source": "p", "seq": 1, "ts": 1100.0, "payload": payload}
    _, (e,), _ = latency_report.parse_capture([f"1000.1 viz/echo/2 {json.dumps(env)}"])
    assert (e.seq, e.t_event, e.t_phone, e.player) == (4, 1000.0, 1100.0, "2")
