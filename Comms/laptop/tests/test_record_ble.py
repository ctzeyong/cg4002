"""record_ble.py: the BLE recorder's timeline, rows and files, without a glove.

Packets are built with the real ble_packet encoder and decoded the way ble_client does, so a
change to the wire format shows up here as well as in test_ble_packet.py.
"""
import csv
import io

import pytest

import record_ble
from ble_packet import IMU_OK, IMU_RECOVERING, TYPE_ECHO, TYPE_SENSOR, BlePacket
from ble_packet import build_fixed_packet, decode


def packet(seq, t, imu=(0, 0, 0, 0, 0, 0), status=IMU_OK, flag=0, ptype=TYPE_SENSOR):
    """A decoded SENSOR (or other) packet as ble_client puts it on the queue."""
    fields = [*imu, status, flag]
    ptype, seq, payload = decode(build_fixed_packet(ptype, seq, fields))
    return BlePacket(node_id=1, type=ptype, seq=seq, payload=payload, t_recv=t)


class FakeLabels:
    def __init__(self):
        self.samples = []
        self.saves = 0

    def sample(self, timestamp, gap=False):
        self.samples.append((timestamp, gap))

    def save(self):
        self.saves += 1


# ---- SampleClock: 8-bit seq -> running count on the 10 ms grid ----

def test_clock_counts_consecutive_packets_without_gaps():
    clock = record_ble.SampleClock()
    assert [clock.feed(s, s * 0.01) for s in range(3)] == [(0, False), (1, False), (2, False)]


def test_clock_marks_lost_packets_as_a_gap_and_keeps_their_slots():
    clock = record_ble.SampleClock()
    clock.feed(10, 0.0)
    assert clock.feed(13, 0.03) == (3, True)   # 11 and 12 lost


def test_clock_wraps_from_255_to_0():
    clock = record_ble.SampleClock()
    clock.feed(254, 0.0)
    assert clock.feed(255, 0.01) == (1, False)
    assert clock.feed(0, 0.02) == (2, False)


def test_clock_drops_repeats_and_late_packets_without_moving_time():
    clock = record_ble.SampleClock()
    clock.feed(50, 0.0)
    clock.feed(51, 0.01)
    assert clock.feed(51, 0.02) is None         # repeat
    assert clock.feed(49, 0.02) is None         # late
    assert clock.feed(52, 0.02) == (2, False)


def test_clock_resyncs_from_arrival_time_after_a_dropout():
    clock = record_ble.SampleClock()
    clock.feed(0, 0.0)
    # 3 s away: the 8-bit seq has wrapped, so the count comes from arrival time
    assert clock.feed(44, 3.0) == (300, True)


# ---- rows ----

def test_row_is_in_record_py_units_on_the_10ms_grid():
    fields = [8192, -4096, 0, 655, -131, 0, IMU_OK, 1]
    row = record_ble.sample_row(fields, 7, 123)
    assert row == [7, 70_000, 1.0, -0.5, 0.0, 10.0, -2.0, 0.0, 123, IMU_OK, 1]


def test_header_matches_record_py_plus_status_and_flag():
    assert record_ble.HEADER == ["sequence", "esp_time_us", "ax_g", "ay_g", "az_g",
                                 "gx_dps", "gy_dps", "gz_dps", "host_rx_unix_ns",
                                 "imu_status", "gesture_flag"]


# ---- Recorder ----

def rows_of(buffer):
    return list(csv.DictReader(io.StringIO(buffer.getvalue())))


def test_recorder_writes_sensor_rows_and_feeds_the_labels_timeline():
    buffer, labels = io.StringIO(), FakeLabels()
    rec = record_ble.Recorder(buffer, labels)
    assert rec.add(packet(0, 0.00, imu=(8192, 0, 0, 0, 0, 0), flag=1))
    assert rec.add(packet(1, 0.01))
    rows = rows_of(buffer)
    assert [r["esp_time_us"] for r in rows] == ["0", "10000"]
    assert rows[0]["ax_g"] == "1.0" and rows[0]["gesture_flag"] == "1"
    assert labels.samples == [(0, False), (10_000, False)]


def test_recorder_skips_non_sensor_and_repeated_packets():
    rec = record_ble.Recorder(io.StringIO())
    assert rec.add(packet(0, 0.0))
    assert not rec.add(packet(0, 0.0))                        # repeat
    assert not rec.add(packet(9, 0.01, ptype=TYPE_ECHO))      # an ECHO is not sensor data
    assert (rec.samples, rec.late) == (1, 1)


def test_no_imu_reading_leaves_a_gap_the_next_row_reports():
    buffer, labels = io.StringIO(), FakeLabels()
    rec = record_ble.Recorder(buffer, labels)
    rec.add(packet(0, 0.00))
    assert not rec.add(packet(1, 0.01, status=IMU_RECOVERING))  # zeros, not a reading
    rec.add(packet(2, 0.02))
    assert [r["esp_time_us"] for r in rows_of(buffer)] == ["0", "20000"]   # the hole stays
    assert labels.samples[-1] == (20_000, True)
    assert (rec.no_imu, rec.gaps) == (1, 1)


# ---- files and arguments ----

def test_unique_output_never_overwrites(tmp_path):
    first = tmp_path / "p1_day1.csv"
    first.write_text("x")
    assert record_ble.unique_output(first) == tmp_path / "p1_day1_2.csv"
    (tmp_path / "p1_day1_2.labels.json").write_text("{}")
    assert record_ble.unique_output(first) == tmp_path / "p1_day1_3.csv"


def test_default_name_matches_record_py_under_sessions_ble():
    args = record_ble.parse_args(["--participant", "p2", "--session", "day3", "--symbol", "chop",
                                  "--drawing-mode", "air_front", "--direction", "cw",
                                  "--posture", "standing"])
    path = record_ble.default_name(args)
    assert path.parent == record_ble.SESSIONS_DIR and path.parent.name == "ble"
    assert path.name == "p2_day3_chop_air_front_cw_standing.csv"


def test_live_labels_need_a_symbol():
    with pytest.raises(SystemExit):
        record_ble.parse_args(["--participant", "p1", "--session", "d1", "--live-labels"])
