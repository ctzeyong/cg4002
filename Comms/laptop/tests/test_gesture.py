"""The gesture windows' seq rules, and the promises AI's ai_api.py makes to Comms.

A mistake here gives wrong predictions with no error anywhere. notes/ai_integration.md 3.2.
"""

import csv
import math
import re
import sys
import time
import types
from pathlib import Path

import pytest

import gesture  # noqa: E402 - ultra96/, on sys.path via conftest

ai_api = gesture.ai_api  # imported via gesture, which knows where to find it

AI_TEST_CSV = Path(__file__).resolve().parents[3] / "AI" / "laptop" / "gestures_test.csv"
IMU_H = Path(__file__).resolve().parents[2] / "firebeetle" / "imu.h"
ZERO = dict.fromkeys(gesture.CHANNELS, 0)


class Recording:
    """A classifier that keeps what it was given."""

    def __init__(self):
        self.samples = self.timeout = None

    def classify(self, samples, timeout=None):
        self.samples, self.timeout = samples, timeout
        return "0", 1.0


def packet(seq, node=1, imu=(0, 0, 0, 0, 0, 0), status=gesture.IMU_OK, t=None):
    msg = {
        "type": "SENSOR",
        "node": node,
        "seq": seq & 0xFF,
        "imu": dict(zip(gesture.CHANNELS, imu)),
        "reserved": [status, 0, 0, 0, 0],
    }
    if t is not None:
        msg["timestamp"] = t
    return msg


def windows(classifier=None):
    return gesture.Windows(classifier or gesture.StubClassifier())


def fill(w, seqs, node=1):
    for seq in seqs:
        assert w.add(packet(seq, node))


# --- seq rules ---------------------------------------------------------------


def test_the_wrap_from_255_to_0_is_not_a_gap():
    w = windows()
    fill(w, range(200, 300))  # 200..255, then 0..43
    assert w.classify(1).missing == 0


def test_a_backwards_seq_is_dropped_and_not_counted_as_missing():
    rec = Recording()
    w = windows(rec)
    fill(w, range(100))
    assert not w.add(packet(98, imu=(7,) * 6))  # one step back reads as a jump of 255
    w.classify(1)
    assert rec.samples[-1] == ZERO  # its sample never went in
    assert w.classify(1).missing == 0


def test_a_repeated_seq_is_dropped():
    rec = Recording()
    w = windows(rec)
    fill(w, range(100))
    assert not w.add(packet(99, imu=(7,) * 6))
    w.classify(1)
    assert rec.samples[-1] == ZERO
    assert w.classify(1).missing == 0


def test_forward_gaps_inside_the_window_are_counted():
    w = windows()
    fill(w, [*range(50), *range(53, 103)])  # 50, 51 and 52 lost
    assert w.size(1) == 100
    assert w.classify(1).missing == 3


def test_a_gap_before_the_window_starts_is_not_counted():
    w = windows()
    fill(w, [*range(10), *range(15, 115)])  # the window is 15..114
    assert w.classify(1).missing == 0


def test_nodes_keep_separate_windows_and_seqs():
    w = windows()
    fill(w, range(100), node=1)
    fill(w, range(50), node=2)  # seq 0 again, but for another node: not a repeat
    assert w.classify(1) is not None
    assert w.classify(2) is None


def test_a_window_is_classified_only_once_it_is_full():
    w = windows()
    fill(w, range(99))
    assert w.classify(1) is None
    assert w.size(1) == 99
    fill(w, [99])
    assert w.classify(1).label in gesture.LABELS


def test_malformed_packets_are_skipped():
    w = windows()
    good = packet(0)
    bad = [
        {**good, "imu": None},
        {**good, "imu": {"ax": 1}},
        {**good, "imu": {**good["imu"], "gz": 1.5}},
        {**good, "imu": {**good["imu"], "gz": True}},
        {**good, "node": "1"},
        {key: value for key, value in good.items() if key != "seq"},
    ]
    assert [w.add(msg) for msg in bad] == [False] * len(bad)
    assert w.size(1) == 0


# --- a stream that stopped starts a new window -------------------------------
# The firmware's seq only moves while it streams, and restarts at 0 on a reboot, so after a
# BLE reconnect seq alone can't tell old samples from new. The laptop's receive time can.


def stream(w, seqs, t0, node=1, ax0=0):
    """Add packets 10 ms apart from t0, with ax counting up from ax0. Returns the next time."""
    for i, seq in enumerate(seqs):
        w.add(packet(seq, node, imu=(ax0 + i, 1, 1, 1, 1, 1), t=t0 + i * 0.01))
    return t0 + len(seqs) * 0.01


def window_ax(w, node=1):
    rec = Recording()
    w.classifier = rec
    result = w.classify(node)
    return None if result is None else ([s["ax"] for s in rec.samples], result.missing)


@pytest.mark.parametrize(
    "before, after",
    [
        (range(0, 100), range(100, 200)),  # reconnect: seq carries on as if nothing happened
        (range(50, 150), range(0, 100)),  # reboot after seq 149: 0 would pass as a forward gap
        (range(0, 100), range(0, 100)),  # reboot after seq 99: 0 would be dropped as late
    ],
    ids=["reconnect", "reboot-looks-like-a-gap", "reboot-looks-late"],
)
def test_after_a_gap_the_window_holds_only_the_new_stream(before, after):
    w = windows()
    t = stream(w, before, 10.0, ax0=1000)
    stream(w, after, t + 3.0, ax0=5000)  # 3 s with nothing, as in a BLE reconnect
    assert window_ax(w) == (list(range(5000, 5100)), 0)


def test_right_after_a_gap_there_is_no_full_window_to_classify():
    w = windows()
    t = stream(w, range(100), 10.0)
    stream(w, [100], t + 3.0)
    assert w.size(1) == 1
    assert w.classify(1) is None


def test_a_gap_under_the_limit_keeps_the_window():
    w = windows()
    t = stream(w, range(100), 10.0, ax0=0)
    last = t - 0.01
    w.add(packet(100, imu=(100, 1, 1, 1, 1, 1), t=last + gesture.RESET_GAP_S - 0.01))
    assert window_ax(w) == (list(range(1, 101)), 0)


def test_a_gap_on_one_node_leaves_the_other_alone():
    w = windows()
    stream(w, range(100), 10.0, node=1)
    stream(w, range(100), 10.0, node=2)
    w.add(packet(100, node=1, t=20.0))
    assert w.size(1) == 1
    assert w.size(2) == 100


@pytest.mark.parametrize("bad", [None, "11.0", float("nan"), float("inf"), True, 0.5])
def test_an_unusable_or_earlier_time_neither_resets_nor_moves_the_clock(bad):
    w = windows()
    t = stream(w, range(100), 0.0)  # times 0.00 .. 0.99
    msg = packet(100)
    msg["timestamp"] = bad
    assert w.add(msg)  # judged on seq alone
    assert w.size(1) == 100
    w.add(packet(101, t=t + 0.01))  # 20 ms after the last good time: no reset
    assert w.size(1) == 100
    w.add(packet(102, t=t + 3.0))  # but a real gap after it still resets
    assert w.size(1) == 1


def test_a_gap_also_restarts_the_count_of_packets_without_a_reading():
    w = windows()
    stream(w, range(3), 10.0)
    for seq in range(3, 6):
        w.add(packet(seq, status=2, t=10.0 + seq * 0.01))
    assert w.without_imu(1) == 3
    w.add(packet(6, status=2, t=15.0))
    assert w.without_imu(1) == 1


# --- only IMU readings reach the model ---------------------------------------


def test_the_board_reads_the_status_the_laptop_and_firmware_use():
    import ble_packet  # laptop/, the copy of firebeetle/imu.h's ImuStatus

    assert gesture.IMU_OK == ble_packet.IMU_OK


NO_READING = [
    {"reserved": [0, 0, 0, 0, 0]},  # old firmware's dummy ramp
    {"reserved": [2, 0, 0, 0, 0]},  # IMU missing, zeros
    {"reserved": [3, 0, 0, 0, 0]},  # IMU recovering, zeros
    {"reserved": [7, 0, 0, 0, 0]},  # unknown
    {"reserved": None},
    {"reserved": []},
    {"reserved": [True, 0, 0, 0, 0]},
]


@pytest.mark.parametrize("change", NO_READING, ids=lambda c: repr(c["reserved"]))
def test_a_packet_without_an_imu_reading_starts_the_window_again(change):
    w = windows()
    fill(w, range(100))
    assert not w.add({**packet(100), **change})
    assert w.size(1) == 0
    assert w.classify(1) is None  # the readings from before it are not classified again


def test_a_packet_with_no_reserved_field_is_not_a_reading():
    w = windows()
    msg = packet(0)
    del msg["reserved"]
    assert not w.add(msg)
    assert w.size(1) == 0


def test_after_an_outage_the_model_gets_only_readings_from_after_it():
    rec = Recording()
    w = windows(rec)
    for seq in range(100):
        assert w.add(packet(seq, imu=(seq, 1, 1, 1, 1, 1)))
    # Unplugged for 2 s: zeros at status 2, then one status 3. Longer than half the seq
    # range, so if these packets didn't move the seq on, the next reading would look late.
    for seq in range(100, 300):
        assert not w.add(packet(seq, status=2 if seq < 299 else 3))
    for seq in range(300, 399):
        assert w.add(packet(seq, imu=(seq, 1, 1, 1, 1, 1)))
    assert w.classify(1) is None  # 99 readings since the outage, not enough
    assert w.add(packet(399, imu=(399, 1, 1, 1, 1, 1)))
    result = w.classify(1)
    assert [s["ax"] for s in rec.samples] == list(range(300, 400))
    assert result.missing == 0  # the outage's packets arrived, so none were lost


def test_packets_without_a_reading_are_counted_until_one_returns():
    w = windows()
    fill(w, range(3))
    for seq in range(3, 6):
        w.add(packet(seq, status=2))
    assert w.without_imu(1) == 3
    assert w.without_imu(2) == 0
    w.add(packet(6))
    assert w.without_imu(1) == 0


def test_a_repeat_or_late_packet_without_a_reading_leaves_the_window_alone():
    w = windows()
    fill(w, range(100))
    assert not w.add(packet(99, status=2))  # a repeat
    assert not w.add(packet(98, status=2))  # late
    assert w.size(1) == 100
    assert w.without_imu(1) == 0


def test_the_model_gets_only_the_six_imu_channels():
    rec = Recording()
    w = windows(rec)
    for seq in range(100):
        msg = packet(seq, imu=(1, 2, 3, 4, 5, 6))
        msg["imu"]["temp"] = 99  # an extra value, if the firmware ever sends one
        msg.update(fields=[1, 2, 3, 4, 5, 6, 1, 0, 0, 0, 0], crc=0, timestamp=1.0)
        assert w.add(msg)
    w.classify(1)
    assert all(s == dict(zip(gesture.CHANNELS, (1, 2, 3, 4, 5, 6))) for s in rec.samples)


# --- what Windows hands the API ----------------------------------------------


def test_the_api_gets_the_window_oldest_first_and_comms_deadline():
    rec = Recording()
    w = windows(rec)
    for seq in range(150):
        assert w.add(packet(seq, imu=(seq, 0, 0, 0, 0, 0)))
    w.classify(1)
    assert [s["ax"] for s in rec.samples] == list(range(50, 150))
    assert rec.timeout == gesture.TIMEOUT_S


# --- AI's API: counts in, the model's inputs ---------------------------------


# MPU6050 datasheet: counts per unit at each full-scale range
ACCEL_LSB_PER_G_AT = {2: 16384, 4: 8192, 8: 4096, 16: 2048}
GYRO_LSB_PER_DPS_AT = {250: 131, 500: 65.5, 1000: 32.8, 2000: 16.4}


def test_ai_api_decodes_counts_at_the_ranges_the_firmware_sets():
    """A range mismatch hands the model values at the wrong size, with no error."""
    ranges = dict(re.findall(r"constexpr int16_t (IMU_\w+_RANGE_\w+) = (\d+);", IMU_H.read_text()))
    assert ai_api.ACCEL_LSB_PER_G == ACCEL_LSB_PER_G_AT[int(ranges["IMU_ACCEL_RANGE_G"])]
    assert ai_api.GYRO_LSB_PER_DPS == GYRO_LSB_PER_DPS_AT[int(ranges["IMU_GYRO_RANGE_DPS"])]


def test_one_g_and_two_degrees_per_second_give_hand_computed_inputs():
    # 1 g = 8192 counts = 9.80665 m/s², x1024 = 10042.01. 131 counts = 2 °/s = 0.034907 rad/s,
    # x1024 = 35.74.
    sample = dict(zip(ai_api.CHANNELS, (8192, -8192, 0, 131, -131, 0)))
    assert ai_api.to_inputs([sample]) == [10042, -10042, 0, 36, -36, 0]


def test_to_fixed_saturates_at_the_int16_ends():
    assert ai_api.to_fixed(1.0) == 1024
    assert ai_api.to_fixed(32.0) == 32767
    assert ai_api.to_fixed(-32.0) == -32768
    assert ai_api.to_fixed(-40.0) == -32768


def test_gyro_full_scale_fits_but_acceleration_clips_above_3_26_g():
    """Q6.10 holds ±32: ±500 °/s (±8.7 rad/s) fits, ±4 g (±39.2 m/s²) does not.

    Acceleration saturates above 32 / 9.80665 = 3.26 g. Whether that is acceptable is AI's
    call; this pins today's behaviour so a change to it is seen.
    """
    top = ai_api.to_inputs([dict.fromkeys(ai_api.CHANNELS, 32767)])
    bottom = ai_api.to_inputs([dict.fromkeys(ai_api.CHANNELS, -32768)])
    assert max(top[3:]) < 32767 and min(bottom[3:]) > -32768

    def ax_input(g):
        sample = {**dict.fromkeys(ai_api.CHANNELS, 0), "ax": round(g * ai_api.ACCEL_LSB_PER_G)}
        return ai_api.to_inputs([sample])[0]

    assert ax_input(3.2) < 32767
    assert ax_input(3.3) == 32767


# --- the stub and the log fields ---------------------------------------------


def test_the_stub_gives_the_same_class_for_the_same_window():
    stub = gesture.StubClassifier()
    samples = [dict(zip(gesture.CHANNELS, range(t, t + 6))) for t in range(100)]
    assert stub.classify(samples)[0] == stub.classify([dict(s) for s in samples])[0]


def test_log_fields_are_empty_for_a_dummy_event():
    assert gesture.log_fields(None) == ""
    assert gesture.log_fields(gesture.Result("3", 0.5, 0.8125, 2)) == (
        " conf=0.500 t_infer_ms=0.812 missing=2"
    )


# --- AI's classifier, against a fake pynq ------------------------------------


class FakeChannel:
    def __init__(self, overlay, name):
        self.overlay, self.name = overlay, name
        self.busy_until = 0.0

    @property
    def idle(self):
        return time.perf_counter() >= self.busy_until

    def transfer(self, buffer):
        if self.overlay.refuse:
            raise RuntimeError("DMA channel not idle")
        self.overlay.calls.append(self.name)
        self.busy_until = time.perf_counter() + self.overlay.stall_s
        if self.name == "send":  # the "IP": logit 2.0 for class inputs[0] % 7, the rest 0
            out = self.overlay.outb
            out[:] = [0] * len(out)
            out[buffer[0] % len(out)] = 2 << ai_api.OUT_FRAC


class FakeOverlay:
    def __init__(self, path, ip=("cnn_axi_0", "axi_dma_0")):
        self.ip_dict = dict.fromkeys(ip)
        self.calls, self.refuse, self.stall_s, self.inb, self.outb = [], False, 0.0, None, None
        self.axi_dma_0 = types.SimpleNamespace(
            sendchannel=FakeChannel(self, "send"), recvchannel=FakeChannel(self, "recv")
        )
        self.cnn_axi_0 = types.SimpleNamespace(write=lambda addr, value: self.calls.append("start"))
        FakeOverlay.last = self


@pytest.fixture
def fake_pynq(monkeypatch):
    def allocate(shape, dtype, cacheable):
        assert dtype == "int16" and cacheable is False
        buffer = [0] * shape[0]
        if shape[0] == len(ai_api.LABELS):
            FakeOverlay.last.outb = buffer
        else:
            FakeOverlay.last.inb = buffer
        return buffer

    monkeypatch.setitem(sys.modules, "pynq", types.SimpleNamespace(Overlay=FakeOverlay, allocate=allocate))
    return FakeOverlay


def test_ai_rows_sent_as_counts_reach_the_fpga_as_the_same_row(fake_pynq):
    """The AI team's own rows, streamed in as counts, must reach the DMA as that row.

    Goes through Windows and classify(), the path the board takes. A channel-major mix-up
    fails this; fingerprint_board.py shows it is a real risk.
    """
    with open(AI_TEST_CSV, newline="") as f:
        reader = csv.reader(f)
        next(reader)
        rows = [next(reader) for _ in range(3)]

    w = windows(ai_api.GestureClassifier("x.xsa"))
    n = len(ai_api.CHANNELS)
    for row in rows:
        values = [float(v) for v in row[1:]]  # row[0] is the label
        assert len(values) == ai_api.WINDOW * n
        for t in range(ai_api.WINDOW):
            step = values[t * n : (t + 1) * n]
            assert w.add(packet(t, imu=[round(v / s) for v, s in zip(step, ai_api.SCALES)]))
        w.classify(1)

        expected = [ai_api.to_fixed(v) for v in values]
        assert max(abs(a - b) for a, b in zip(fake_pynq.last.inb, expected)) <= 1
        w = windows(w.classifier)  # a fresh window, seq 0 again


def test_classify_gives_the_top_class_and_its_softmax(fake_pynq):
    model = ai_api.GestureClassifier("x.xsa")
    label, confidence = model.classify([ZERO] * ai_api.WINDOW)  # input word 0 -> class 0
    assert label == "0"
    assert confidence == pytest.approx(math.exp(2) / (math.exp(2) + 6))
    assert model.logits([5] + [0] * 599)[5] == 2.0  # 2048 in Q6.10


def test_classify_refuses_a_window_of_the_wrong_length(fake_pynq):
    model = ai_api.GestureClassifier("x.xsa")
    with pytest.raises(ai_api.ClassifyError, match="need 100 samples, got 99"):
        model.classify([ZERO] * 99)


def test_the_ip_is_started_and_the_receive_armed_before_sending(fake_pynq):
    model = ai_api.GestureClassifier("x.xsa")
    fake_pynq.last.calls.clear()
    model.classify([ZERO] * ai_api.WINDOW)
    assert fake_pynq.last.calls == ["start", "recv", "send"]


def test_loading_runs_one_warm_up_inference(fake_pynq):
    model = ai_api.GestureClassifier("x.xsa")
    assert fake_pynq.last.calls == ["start", "recv", "send"]
    assert model.cold_ms >= 0


def test_a_stalled_dma_fails_that_window_within_the_timeout(fake_pynq):
    model = ai_api.GestureClassifier("x.xsa")
    fake_pynq.last.stall_s = 0.3  # it would finish, but far too late
    started = time.perf_counter()
    with pytest.raises(gesture.ClassifierError):
        model.classify([ZERO] * ai_api.WINDOW, timeout=gesture.TIMEOUT_S)
    assert time.perf_counter() - started < 0.2


def test_a_refused_transfer_is_a_classify_error(fake_pynq):
    model = ai_api.GestureClassifier("x.xsa")
    fake_pynq.last.refuse = True
    with pytest.raises(gesture.ClassifierError):
        model.classify([ZERO] * ai_api.WINDOW)


def test_a_bitstream_without_the_control_port_is_refused(fake_pynq, monkeypatch):
    monkeypatch.setattr(sys.modules["pynq"], "Overlay", lambda path: FakeOverlay(path, ip=("axi_dma_0",)))
    with pytest.raises(RuntimeError, match="cnn_axi_0"):
        ai_api.GestureClassifier("old.xsa")


def test_the_board_gets_the_fpga_classifier_and_anywhere_else_the_stub(fake_pynq, monkeypatch, tmp_path):
    monkeypatch.setattr(gesture, "FPGA_MANAGER", tmp_path / "absent")
    assert isinstance(gesture.load_classifier(), gesture.StubClassifier)
    monkeypatch.setattr(gesture, "FPGA_MANAGER", tmp_path)
    assert isinstance(gesture.load_classifier(), ai_api.GestureClassifier)


def test_on_the_board_a_missing_pynq_is_an_error_not_the_stub(monkeypatch, tmp_path):
    monkeypatch.setattr(gesture, "FPGA_MANAGER", tmp_path)
    monkeypatch.setitem(sys.modules, "pynq", None)  # importing it now raises ImportError
    with pytest.raises(ImportError):
        gesture.load_classifier()
