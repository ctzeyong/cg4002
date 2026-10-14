"""Gesture windows: the seq rules, the unit conversion and the model's input order.

A mistake here gives wrong predictions with no error anywhere. notes/ai_integration.md 3.2.
"""

import csv
import math
import sys
import time
import types
from pathlib import Path

import pytest

import gesture  # noqa: E402 - ultra96/, on sys.path via conftest

accelerator = gesture.accelerator  # the AI team's module, found by gesture

AI_TEST_CSV = Path(__file__).resolve().parents[3] / "AI" / "laptop" / "gestures_test.csv"


class Recording:
    """A classifier that keeps the inputs it was given."""

    def __init__(self):
        self.features = None

    def classify(self, features):
        self.features = features
        return "0", 1.0, 0.0


def packet(seq, node=1, imu=(0, 0, 0, 0, 0, 0)):
    return {"type": "SENSOR", "node": node, "seq": seq & 0xFF, "imu": dict(zip(gesture.CHANNELS, imu))}


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
    assert rec.features[-6:] == [0] * 6  # its sample never went in
    assert w.classify(1).missing == 0


def test_a_repeated_seq_is_dropped():
    rec = Recording()
    w = windows(rec)
    fill(w, range(100))
    assert not w.add(packet(99, imu=(7,) * 6))
    w.classify(1)
    assert rec.features[-6:] == [0] * 6
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


# --- units -------------------------------------------------------------------


def test_one_g_and_two_degrees_per_second_give_hand_computed_inputs():
    # 1 g = 9.80665 m/s², x1024 = 10042.01. 131 counts = 2 °/s = 0.034907 rad/s, x1024 = 35.74.
    sample = (16384, -16384, 0, 131, -131, 0)
    assert gesture.to_features([sample]) == [10042, -10042, 0, 36, -36, 0]


def test_quantise_saturates_at_the_int16_ends():
    assert gesture.quantise(1.0) == 1024
    assert gesture.quantise(32.0) == 32767
    assert gesture.quantise(-32.0) == -32768
    assert gesture.quantise(-40.0) == -32768


def test_full_scale_counts_fit_without_saturating():
    """±2 g and ±500 °/s both stay inside Q6.10's ±32."""
    top = gesture.to_features([(32767,) * 6])
    bottom = gesture.to_features([(-32768,) * 6])
    assert max(top) < 32767 and min(bottom) > -32768


# --- the model's input order -------------------------------------------------


def test_ai_rows_come_back_in_the_models_order():
    """The AI team's own rows, sent through as counts, must come back as the same row.

    A channel-major mix-up fails this; fingerprint_board.py shows it is a real risk.
    """
    with open(AI_TEST_CSV, newline="") as f:
        reader = csv.reader(f)
        next(reader)
        rows = [next(reader) for _ in range(3)]

    for row in rows:
        values = [float(v) for v in row[1:]]  # row[0] is the label
        assert len(values) == gesture.WINDOW * len(gesture.CHANNELS)
        rec = Recording()
        w = windows(rec)
        for t in range(gesture.WINDOW):
            step = values[t * 6 : (t + 1) * 6]
            assert w.add(packet(t, imu=[round(v / s) for v, s in zip(step, gesture.SCALES)]))
        w.classify(1)

        expected = [gesture.quantise(v) for v in values]
        assert max(abs(a - b) for a, b in zip(rec.features, expected)) <= 1


# --- the stub and the log fields ---------------------------------------------


def test_the_stub_gives_the_same_class_for_the_same_window():
    stub = gesture.StubClassifier()
    features = list(range(600))
    assert stub.classify(features)[0] == stub.classify(list(features))[0]


def test_log_fields_are_empty_for_a_dummy_event():
    assert gesture.log_fields(None) == ""
    assert gesture.log_fields(gesture.Result("3", 0.5, 0.8125, 2)) == (
        " conf=0.500 t_infer_ms=0.812 missing=2"
    )


# --- the FPGA classifier, against a fake pynq --------------------------------


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
            out[buffer[0] % len(out)] = 2 << accelerator.OUT_FRAC


class FakeOverlay:
    def __init__(self, path, ip=("cnn_axi_0", "axi_dma_0")):
        self.ip_dict = dict.fromkeys(ip)
        self.calls, self.refuse, self.stall_s, self.outb = [], False, 0.0, None
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
        if shape[0] == len(gesture.LABELS):
            FakeOverlay.last.outb = buffer
        return buffer

    monkeypatch.setitem(sys.modules, "pynq", types.SimpleNamespace(Overlay=FakeOverlay, allocate=allocate))
    return FakeOverlay


def test_the_classifier_gives_the_top_class_and_its_softmax(fake_pynq):
    clf = gesture.Classifier("x.xsa")
    label, confidence, t_infer_ms = clf.classify([3] + [0] * 599)
    assert label == "3"
    assert confidence == pytest.approx(math.exp(2) / (math.exp(2) + 6))
    assert t_infer_ms > 0
    assert clf.infer([5] + [0] * 599)[5] == 2.0  # 2048 in Q6.10


def test_the_ip_is_started_and_the_receive_armed_before_sending(fake_pynq):
    clf = gesture.Classifier("x.xsa")
    fake_pynq.last.calls.clear()
    clf.classify([0] * 600)
    assert fake_pynq.last.calls == ["start", "recv", "send"]


def test_loading_runs_one_warm_up_inference(fake_pynq):
    clf = gesture.Classifier("x.xsa")
    assert fake_pynq.last.calls == ["start", "recv", "send"]
    assert clf.cold_ms >= 0


def test_a_stalled_dma_fails_that_window_quickly(fake_pynq):
    clf = gesture.Classifier("x.xsa")
    fake_pynq.last.stall_s = 0.3  # it would finish, but far too late
    started = time.perf_counter()
    with pytest.raises(gesture.ClassifierError):
        clf.classify([0] * 600)
    assert time.perf_counter() - started < 0.2


def test_a_refused_transfer_is_a_classifier_error(fake_pynq):
    clf = gesture.Classifier("x.xsa")
    fake_pynq.last.refuse = True
    with pytest.raises(gesture.ClassifierError):
        clf.classify([0] * 600)


def test_a_bitstream_without_the_control_port_is_refused(fake_pynq, monkeypatch):
    monkeypatch.setattr(sys.modules["pynq"], "Overlay", lambda path: FakeOverlay(path, ip=("axi_dma_0",)))
    with pytest.raises(RuntimeError, match="cnn_axi_0"):
        gesture.Classifier("old.xsa")


def test_the_board_gets_the_fpga_classifier_and_anywhere_else_the_stub(fake_pynq, monkeypatch, tmp_path):
    monkeypatch.setattr(gesture, "FPGA_MANAGER", tmp_path / "absent")
    assert isinstance(gesture.load_classifier(), gesture.StubClassifier)
    monkeypatch.setattr(gesture, "FPGA_MANAGER", tmp_path)
    assert isinstance(gesture.load_classifier(), gesture.Classifier)


def test_on_the_board_a_missing_pynq_is_an_error_not_the_stub(monkeypatch, tmp_path):
    monkeypatch.setattr(gesture, "FPGA_MANAGER", tmp_path)
    monkeypatch.setitem(sys.modules, "pynq", None)  # importing it now raises ImportError
    with pytest.raises(ImportError):
        gesture.load_classifier()
