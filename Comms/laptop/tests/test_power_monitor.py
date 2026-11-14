"""Tests for ultra96/power_monitor.py, off the board.

Fake rails stand in for pynq and a plain function for the broker, so neither is needed here.
"""

import csv
import json
import pathlib
import threading
import time

import pytest

import power_monitor as pm

ULTRA96 = pathlib.Path(__file__).resolve().parents[2] / "ultra96"
WATTS = {"PSINT_FP": 0.7, "INT": 0.21875, "PSDDR": 0.0625, "3V3": 0.40625, "AUX": 0.0625}


def fake_rails(watts=WATTS):
    return {name: (lambda w=w: w) for name, w in watts.items()}


def match_message(phase):
    """A game/match message as the engine sends it: an envelope around the payload."""
    return json.dumps({"event_id": "e", "source": "ultra96-engine", "seq": 0, "ts": 0,
                       "payload": {"match_id": "m", "phase": phase}}).encode()


# --- the match phase -----------------------------------------------------------


def test_the_topic_is_the_one_the_engine_publishes():
    """Read from the source: importing the engine here would pull in its whole core."""
    assert f'MATCH_TOPIC = "{pm.MATCH_TOPIC}"' in (ULTRA96 / "game_engine.py").read_text()


@pytest.mark.parametrize("phase, state", [
    ("running", "playing"), ("waiting", "idle"), ("countdown", "idle"), ("paused", "idle"),
    ("ended", "idle"), ("offline", "idle"), (None, "idle"),
])
def test_only_a_running_match_counts_as_playing(phase, state):
    assert pm.state_of(phase) == state


@pytest.mark.parametrize("phase, since, state", [
    ("waiting", 5.0, "playing"),  # VS CPU: the engine never leaves waiting, but gestures come in
    ("waiting", 30.0, "playing"),
    ("waiting", 31.0, "idle"),
    (None, None, "idle"),
    ("running", 300.0, "playing"),  # a board match counts however quiet the gloves are
])
def test_a_recent_gesture_also_counts_as_playing(phase, since, state):
    assert pm.state_of(phase, since) == state


def test_the_watcher_counts_gestures_but_not_a_retained_one():
    now = [100.0]
    watcher = pm.MatchWatcher(clock=lambda: now[0])
    assert watcher.snapshot() == (None, 0, None)

    watcher.on_gesture("game/gesture/1", retained=True)  # replayed on subscribe
    assert watcher.snapshot() == (None, 0, None)

    watcher.on_gesture("game/gesture/1")
    now[0] = 110.0
    watcher.on_gesture("game/gesture/2")
    now[0] = 115.0
    assert watcher.snapshot() == (None, 2, 5.0)


def test_the_watcher_keeps_the_latest_phase_and_ignores_junk():
    watcher = pm.MatchWatcher()
    assert watcher.phase is None

    watcher.on_message(match_message("running"))
    watcher.on_message(b"not json")
    watcher.on_message(json.dumps({"payload": "no phase"}).encode())
    assert watcher.phase == "running"

    watcher.on_message(match_message("ended"))
    assert watcher.phase == "ended"


# --- recording -------------------------------------------------------------------


def record_with(phases, tmp_path, seconds=0.6, status=None):
    """Record with the phase changing every 1/len(phases) of `seconds`, and no gestures."""
    start = time.monotonic()

    def phase_only():
        i = int((time.monotonic() - start) / seconds * len(phases))
        return phases[min(i, len(phases) - 1)], 0, None

    stop = threading.Event()
    threading.Timer(seconds, stop.set).start()
    row, _ = pm.record(fake_rails(), status or phase_only, stop, interval=0.01, out_dir=tmp_path,
                       summary_every=0.1)
    return row, sorted(tmp_path.glob("power_*Z.csv"))[-1]


def test_each_reading_is_labelled_and_the_session_is_summarised(tmp_path):
    row, readings = record_with([None, "running", "ended"], tmp_path)

    rows = list(csv.DictReader(open(readings, newline="")))
    assert {r["state"] for r in rows} == {"idle", "playing"}
    assert {r["match_phase"] for r in rows} == {"none", "running", "ended"}
    assert set(rows[0]) == {"utc", "state", "match_phase", "gestures", *WATTS, "TOTAL"}
    assert float(rows[0]["TOTAL"]) == pytest.approx(sum(WATTS.values()), abs=1e-4)

    assert row["idle_TOTAL"] == pytest.approx(1.45) and row["playing_TOTAL"] == pytest.approx(1.45)
    assert row["playing_minus_idle_W"] == pytest.approx(0.0)
    assert row["readings_file"] == readings.name
    # time is split about 2:1, idle:playing, and adds up to the whole session
    assert row["idle_s"] > row["playing_s"] > 0
    assert row["idle_s"] + row["playing_s"] == pytest.approx(0.6, abs=0.15)

    summary = list(csv.DictReader(open(tmp_path / "power_summary.csv", newline="")))
    assert len(summary) == 1 and summary[0]["readings_file"] == readings.name


def test_a_session_with_no_match_leaves_playing_empty(tmp_path):
    row, _ = record_with(["waiting"], tmp_path, seconds=0.2)

    assert row["playing_s"] == 0 and row["playing_TOTAL"] == ""
    assert row["playing_minus_idle_W"] == ""
    assert row["idle_TOTAL"] == pytest.approx(1.45)


def test_a_vs_cpu_session_counts_as_playing_while_gestures_come_in(tmp_path, monkeypatch):
    """The board's engine stays at waiting; only the gestures say someone is playing."""
    monkeypatch.setattr(pm, "GESTURE_WINDOW_S", 0.2)
    start = time.monotonic()

    def status():  # one gesture at 0.1 s, so playing from then until 0.3 s
        t = time.monotonic() - start
        return ("waiting", 0, None) if t < 0.1 else ("waiting", 1, t - 0.1)

    row, readings = record_with([], tmp_path, seconds=0.6, status=status)

    states = [r["state"] for r in csv.DictReader(open(readings, newline=""))]
    assert states[0] == "idle" and "playing" in states and states[-1] == "idle"
    assert row["gestures"] == 1
    assert row["playing_s"] == pytest.approx(0.2, abs=0.1)


def test_sessions_add_rows_under_one_header(tmp_path):
    record_with(["waiting"], tmp_path, seconds=0.1)
    time.sleep(1.1)  # the readings file is named by the second
    record_with(["running"], tmp_path, seconds=0.1)

    summary = list(csv.DictReader(open(tmp_path / "power_summary.csv", newline="")))
    assert len(summary) == 2


def test_a_failing_read_is_skipped_not_fatal(tmp_path, caplog):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("i2c timeout")
        return 1.0

    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    row, _ = pm.record({"PSINT_FP": flaky, "INT": flaky, "PSDDR": flaky, "3V3": flaky},
                    lambda: (None, 0, None), stop, interval=0.01, out_dir=tmp_path)

    assert "power read failed: i2c timeout" in caplog.text
    assert row["idle_TOTAL"] == pytest.approx(4.0)


def test_the_summary_line_names_the_four_main_rails_and_the_rest():
    means = pm.Means()
    means.add({"PSINT_FP": 0.5, "INT": 0.2, "PSDDR": 0.1, "3V3": 0.4, "AUX": 0.1}, 0.25)
    means.add({"PSINT_FP": 0.7, "INT": 0.2, "PSDDR": 0.1, "3V3": 0.4, "AUX": 0.3}, 0.25)

    assert means.seconds == 0.5
    assert pm.describe(means.mean()) == (
        "1.500 W (PSINT_FP 0.600, INT 0.200, PSDDR 0.100, 3V3 0.400, other 0.200)")


def test_the_watcher_reads_the_retained_phase_from_a_real_broker(broker):
    """Subscribing after the engine published must still give the phase: the message is retained."""
    from paho.mqtt import publish

    publish.single(pm.MATCH_TOPIC, match_message("running"), qos=1, retain=True,
                   hostname="127.0.0.1", port=broker.port)
    watcher = pm.MatchWatcher(port=broker.port).start()
    try:
        deadline = time.monotonic() + 5
        while watcher.phase is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert watcher.phase == "running"

        publish.single(pm.MATCH_TOPIC, match_message("ended"), qos=1, retain=True,
                       hostname="127.0.0.1", port=broker.port)
        while watcher.phase == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert watcher.phase == "ended"
    finally:
        watcher.stop()


def test_messages_are_routed_by_topic():
    """What paho hands on_message: the match topic sets the phase, a gesture topic counts."""
    from types import SimpleNamespace

    watcher = pm.MatchWatcher()
    watcher._route(SimpleNamespace(topic="game/match", payload=match_message("running"), retain=1))
    watcher._route(SimpleNamespace(topic="game/gesture/1", payload=b"{}", retain=0))
    watcher._route(SimpleNamespace(topic="game/gesture/2", payload=b"{}", retain=1))

    assert watcher.phase == "running" and watcher.gestures == 1


def test_the_gesture_topics_cover_what_the_client_publishes():
    import envelope

    prefix = pm.GESTURE_TOPICS.rstrip("+")
    assert all(envelope.gesture_topic(p).startswith(prefix) for p in envelope.PLAYERS)


def test_the_dashboard_gets_the_latest_readings_every_few_seconds_then_the_totals(tmp_path):
    sent = []
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    row, final = pm.record(fake_rails(), lambda: ("running", 4, 1.0), stop, interval=0.01,
                           out_dir=tmp_path, publish=sent.append, publish_every=0.1)

    assert len(sent) >= 3
    latest = sent[-1]
    assert latest["state"] == "playing" and latest["phase"] == "running" and latest["gestures"] == 4
    assert latest["now_W"] == pytest.approx(1.45) and latest["now_s"] == pytest.approx(0.1, abs=0.06)
    assert latest["now_rails"] == {"PSINT_FP": 0.7, "INT": 0.219, "PSDDR": 0.062, "3V3": 0.406}
    assert latest["idle_W"] is None and latest["playing_W"] == pytest.approx(1.45)
    json.dumps(latest)  # it goes over MQTT as JSON
    assert final["state"] == "stopped" and final["playing_s"] == row["playing_s"]


def test_without_a_broker_nothing_is_published():
    """A watcher that never connected must not fail at the end either."""
    watcher = pm.MatchWatcher()
    watcher.publish({"state": "idle"})
    watcher.stop({"state": "stopped"})


def test_a_summary_from_an_older_version_is_rewritten_so_every_row_lines_up(tmp_path):
    """The bug of 13 Nov: the gestures column was added under the old header, shifting the rows."""
    path = tmp_path / "power_summary.csv"
    old_header = "start_utc,end_utc,readings_file,idle_s,idle_TOTAL"
    path.write_text(f"{old_header}\n"
                    "a,b,old.csv,141.0,2.16\n"         # written by the old version
                    "c,d,shifted.csv,72,4330.3,2.15\n",  # new version's row under the old header
                    encoding="utf-8")
    row = {"start_utc": "e", "end_utc": "f", "readings_file": "new.csv", "gestures": 50,
           "idle_s": 68.5, "idle_TOTAL": 2.2}

    pm.append_summary(path, row)

    rows = list(csv.DictReader(open(path, newline="")))
    assert list(rows[0]) == list(row)
    assert [(r["readings_file"], r["gestures"], r["idle_s"]) for r in rows] == [
        ("old.csv", "", "141.0"), ("shifted.csv", "72", "4330.3"), ("new.csv", "50", "68.5")]


# --- tuning ------------------------------------------------------------------------

TUNED = {"now": {"cpu_mhz": 300, "cores": 1, "pl_mhz": 25.0},
         "was": {"cpu_mhz": 1200, "cores": 4, "pl_mhz": 100.0}}


def test_the_dashboard_and_the_summary_get_the_boards_settings(tmp_path):
    sent = []
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    row, final = pm.record(fake_rails(), lambda: (None, 0, None), stop, interval=0.01,
                           out_dir=tmp_path, publish=sent.append, publish_every=0.1,
                           tuning=lambda: TUNED)

    assert sent and all(p["tuning"] == TUNED for p in sent) and final["tuning"] == TUNED
    assert (row["cpu_mhz"], row["cores"], row["pl_mhz"]) == (300, 1, 25.0)
    summary = list(csv.DictReader(open(tmp_path / "power_summary.csv", newline="")))
    assert summary[0]["cpu_mhz"] == "300" and summary[0]["pl_mhz"] == "25.0"


def test_settings_that_cannot_be_read_never_stop_the_recording(tmp_path, caplog):
    def broken():
        raise OSError("no pynq")

    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    row, final = pm.record(fake_rails(), lambda: (None, 0, None), stop, interval=0.01,
                           out_dir=tmp_path, publish=lambda p: None, publish_every=0.05,
                           tuning=broken)

    assert "could not read the board's settings: no pynq" in caplog.text
    assert row["idle_TOTAL"] == pytest.approx(1.45) and row["cpu_mhz"] == ""
    assert final["tuning"] is None
