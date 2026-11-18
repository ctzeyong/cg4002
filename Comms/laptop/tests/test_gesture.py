"""The transport rules Comms applies before a SENSOR sample reaches AI, and what AI is given.

A mistake here gives wrong predictions with no error anywhere.
"""

import ast
import dataclasses
import logging
import math
import re
import sys
import types
from pathlib import Path

import pytest

import ai_events  # noqa: E402 - ultra96/, on sys.path via conftest
import gesture  # noqa: E402

ZERO = dict.fromkeys(gesture.CHANNELS, 0)


def packet(seq, node=1, imu=(0, 0, 0, 0, 0, 0), status=gesture.IMU_OK, t=None, flag=0):
    msg = {
        "type": "SENSOR",
        "node": node,
        "seq": seq & 0xFF,
        "imu": dict(zip(gesture.CHANNELS, imu)),
        "reserved": [status, flag],
    }
    if t is not None:
        msg["timestamp"] = t
    return msg


# --- AI's side, faked ---------------------------------------------------------


class FakeAI:
    """Stands in for gesture.AI, keeping every call in order. Gives `answer` at a gesture's end."""

    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer

    def on_sample(self, player, record):
        self.calls.append(("sample", player, record))
        return self.answer if record["end_of_gesture"] else None

    def reset(self, player=None):
        self.calls.append(("reset", player))


def fed(answer=None, ai=None, **kwargs):
    ai = ai or FakeAI(answer)
    w = gesture.Windows(ai, **kwargs)
    ai.calls.clear()  # the session's first reset has its own test
    return w


def seqs_seen(w):
    return [call[2]["ax"] for call in w.ai.calls if call[0] == "sample"]


def ends_seen(w):
    return [call[2]["end_of_gesture"] for call in w.ai.calls if call[0] == "sample"]


def flagged(w, seqs, node=1, t0=None, ax0=0):
    """Feed one flagged packet per seq, 10 ms apart from t0, with ax counting up from ax0."""
    for i, seq in enumerate(seqs):
        t = None if t0 is None else t0 + i * 0.01
        assert w.feed(packet(seq, node, imu=(ax0 + i, 1, 1, 1, 1, 1), t=t, flag=1)) is None


def resets(w):
    return [c[1] for c in w.ai.calls if c[0] == "reset"]


# --- seq rules ---------------------------------------------------------------


def test_the_wrap_from_255_to_0_is_not_a_gap():
    w = fed(answer=("0", 1.0))
    flagged(w, range(200, 300))  # 200..255, then 0..43
    assert w.feed(packet(300)).missing == 0


@pytest.mark.parametrize("old", [99, 98], ids=["repeat", "late"])
def test_a_repeat_or_late_seq_is_dropped_and_not_counted_as_missing(old):
    w = fed(answer=("0", 1.0))
    flagged(w, range(100))
    assert w.feed(packet(old, imu=(999,) * 6, flag=1)) is None
    assert 999 not in seqs_seen(w)  # its sample never went in
    assert w.feed(packet(100)).missing == 0


def test_malformed_packets_are_skipped():
    w = fed()
    good = packet(0)
    bad = [
        {**good, "imu": None},
        {**good, "imu": {"ax": 1}},
        {**good, "imu": {**good["imu"], "gz": 1.5}},
        {**good, "imu": {**good["imu"], "gz": True}},
        {**good, "node": "1"},
        {key: value for key, value in good.items() if key != "seq"},
    ]
    assert [w.feed(msg) for msg in bad] == [None] * len(bad)
    assert w.ai.calls == []


# --- a stream that stopped starts again --------------------------------------
# The firmware's seq only moves while it streams, and restarts at 0 on a reboot, so after a
# BLE reconnect seq alone can't tell old samples from new. The laptop's receive time can.


@pytest.mark.parametrize(
    "before, after",
    [
        (range(0, 100), range(100, 200)),  # reconnect: seq carries on as if nothing happened
        (range(50, 150), range(0, 100)),  # reboot after seq 149: 0 would pass as a forward gap
        (range(0, 100), range(0, 100)),  # reboot after seq 99: 0 would be dropped as late
    ],
    ids=["reconnect", "reboot-looks-like-a-gap", "reboot-looks-late"],
)
def test_after_a_gap_ai_gets_only_the_new_stream(before, after):
    w = fed(answer=("0", 1.0))
    flagged(w, before, t0=10.0, ax0=1000)
    w.ai.calls.clear()
    t = 10.0 + len(before) * 0.01 + 3.0  # 3 s with nothing, as in a BLE reconnect
    flagged(w, after, t0=t, ax0=5000)
    result = w.feed(packet(after[-1] + 1, t=t + len(after) * 0.01))
    assert w.ai.calls[0] == ("reset", 1) and resets(w) == [1]
    assert seqs_seen(w)[:-1] == list(range(5000, 5100))
    assert result.missing == 0


def test_a_gap_under_the_limit_keeps_the_gesture():
    w = fed(answer=("0", 1.0))
    flagged(w, range(100), t0=10.0)
    last = 10.0 + 99 * 0.01
    result = w.feed(packet(100, t=last + gesture.RESET_GAP_S - 0.01))
    assert resets(w) == []
    assert result is not None


@pytest.mark.parametrize("bad", [None, "11.0", float("nan"), float("inf"), True, 0.5])
def test_an_unusable_or_earlier_time_neither_resets_nor_moves_the_clock(bad):
    w = fed()
    flagged(w, range(100), t0=0.0)  # times 0.00 .. 0.99
    msg = packet(100, imu=(999,) * 6)
    msg["timestamp"] = bad
    w.feed(msg)  # judged on seq alone
    assert resets(w) == [] and seqs_seen(w)[-1] == 999
    w.feed(packet(101, t=1.01))  # 20 ms after the last good time, so no reset
    assert resets(w) == []
    w.feed(packet(102, t=4.0))  # but a real gap after it still resets
    assert resets(w) == [1]


# --- only IMU readings reach AI ----------------------------------------------


def test_the_board_reads_the_status_the_laptop_and_firmware_use():
    import ble_packet  # laptop/, the copy of firebeetle/imu.h's ImuStatus

    assert gesture.IMU_OK == ble_packet.IMU_OK


NO_READING = [
    {"reserved": [0, 0]},  # old firmware's dummy ramp
    {"reserved": [2, 0]},  # IMU missing, zeros
    {"reserved": [3, 0]},  # IMU recovering, zeros
    {"reserved": [7, 0]},  # unknown
    {"reserved": None},
    {"reserved": []},
    {"reserved": [True, 0]},
]


@pytest.mark.parametrize("change", NO_READING, ids=lambda c: repr(c["reserved"]))
def test_a_packet_without_an_imu_reading_resets_ai_and_never_reaches_it(change):
    w = fed()
    flagged(w, range(3))
    assert w.feed({**packet(3, imu=(7,) * 6), **change}) is None
    assert resets(w) == [1]
    assert 7 not in seqs_seen(w)


def test_a_packet_with_no_reserved_field_is_not_a_reading():
    w = fed()
    msg = packet(0)
    del msg["reserved"]
    assert w.feed(msg) is None
    assert w.ai.calls == [("reset", 1)]


def test_after_an_outage_ai_gets_only_readings_from_after_it():
    w = fed(answer=("0", 1.0))
    flagged(w, range(100))
    # Unplugged for 2 s: zeros at status 2, then one status 3. Longer than half the seq
    # range, so if these packets didn't move the seq on, the next reading would look late.
    for seq in range(100, 300):
        assert w.feed(packet(seq, status=2 if seq < 299 else 3, flag=1)) is None
    w.ai.calls.clear()
    flagged(w, range(300, 400), ax0=300)
    result = w.feed(packet(400, imu=(400, 1, 1, 1, 1, 1)))
    assert seqs_seen(w) == list(range(300, 401))
    assert result.missing == 0  # the outage's packets arrived, so none were lost


def test_a_repeat_or_late_packet_without_a_reading_leaves_ai_alone():
    w = fed(answer=("0", 1.0))
    flagged(w, range(10))
    assert w.feed(packet(9, status=2)) is None  # a repeat
    assert w.feed(packet(8, status=2)) is None  # late
    assert resets(w) == []
    assert w.feed(packet(10)) is not None  # the gesture survived them


def test_ai_gets_only_the_six_imu_channels():
    w = fed()
    msg = packet(0, imu=(1, 2, 3, 4, 5, 6), t=1.0)
    msg["imu"]["temp"] = 99  # an extra value, if the firmware ever sends one
    msg.update(fields=[1, 2, 3, 4, 5, 6, 1, 0], crc=0)
    w.feed(msg)
    record = w.ai.calls[0][2]
    assert {k: record[k] for k in gesture.CHANNELS} == dict(zip(gesture.CHANNELS, (1, 2, 3, 4, 5, 6)))
    assert set(record) == {*gesture.CHANNELS, "end_of_gesture", "timestamp"}


# --- --gesture-flag: what reaches AI ----------------------------------------


def test_each_sample_reaches_ai_as_its_imu_sample_fields_with_the_player():
    w = fed()
    w.feed(packet(0, node=2, imu=(1, 2, 3, 4, 5, 6), t=12.5, flag=1))
    record = dict(zip(gesture.CHANNELS, (1, 2, 3, 4, 5, 6)), end_of_gesture=False, timestamp=12.5)
    assert w.ai.calls == [("sample", 2, record)]


def test_end_of_gesture_is_set_on_the_first_sample_after_the_flag_drops():
    w = fed()
    for seq, flag in enumerate((0, 1, 1, 1, 0, 0, 1, 0)):
        w.feed(packet(seq, flag=flag))
    assert ends_seen(w) == [False, False, False, False, True, False, False, True]


def test_each_node_ends_its_own_gesture():
    w = fed()
    w.feed(packet(0, node=1, flag=1))
    w.feed(packet(0, node=2))  # node 2 was never flagged
    w.feed(packet(1, node=1))
    ends = [(c[1], c[2]["end_of_gesture"]) for c in w.ai.calls]
    assert ends == [(1, False), (2, False), (1, True)]


@pytest.mark.parametrize(
    "reserved, ends",
    [
        ([1, 1], True),
        ([1, 0], False),
        ([1], False),  # no flag field at all
        ([1, None], False),
        ([1, True], False),
        ([1, 3], False),  # an unknown flag is not a gesture
    ],
)
def test_the_flag_is_read_from_reserved_1(reserved, ends):
    w = fed()
    w.feed({**packet(0), "reserved": reserved})
    w.feed(packet(1))
    assert ends_seen(w) == [False, ends]


def test_the_board_reads_the_flag_values_the_firmware_sends():
    values = dict(re.findall(r"(GESTURE_\w+) = (\d+),", IMU_H.read_text()))
    assert gesture.IN_GESTURE == int(values["GESTURE_ACTIVE"])
    assert gesture.REJECTED == int(values["GESTURE_REJECTED"])


def gesture_then_idle(w, start, flagged_n, idle_n, node=1):
    """Feed flagged_n flagged samples then idle_n unflagged ones from seq `start`; the first
    unflagged one ends the gesture. Returns (next seq, labels of the results it gave)."""
    labels = []
    for i in range(flagged_n + idle_n):
        result = w.feed(packet(start + i, node, flag=int(i < flagged_n)))
        if result is not None:
            labels.append(result.label)
    return start + flagged_n + idle_n, labels


class Answers(FakeAI):
    """Gives the next of `answers` at each gesture end (None = noise / unsure)."""

    def __init__(self, *answers):
        super().__init__()
        self.answers = list(answers)

    def on_sample(self, player, record):
        super().on_sample(player, record)
        if not record["end_of_gesture"]:
            return None
        label = self.answers.pop(0)
        return None if label is None else (label, 0.9)


def gestures(w, *spacing, node=1):
    """One 3-sample gesture per entry, each followed by that many idle samples (>= 1, the first
    of which ends it). Returns every result label, in order."""
    seq, labels = 0, []
    for idle in spacing:
        seq, got = gesture_then_idle(w, seq, 3, idle, node)
        labels += got
    return labels


def test_only_the_next_shield_or_warrior_soon_after_a_shield_gives_no_event(caplog):
    w = fed(ai=Answers("shield", "warrior", "warrior"), shield_cooldown_samples=20)
    with caplog.at_level(logging.INFO, logger="gesture"):
        labels = gestures(w, 2, 2, 2)  # ends at 3, 8 and 13
    assert labels == ["shield", "warrior"]  # the lowering's warrior at 8 is dropped, not 13's
    assert ends_seen(w).count(True) == 3  # AI still classifies every end
    assert no_result_lines(caplog) == [
        "no AI event: node 1's warrior came 5 samples after its shield (cooldown 20)",
    ]


def test_shields_can_still_be_spammed():
    # each real shield's lowering comes out as shield too: every other one is dropped
    w = fed(ai=Answers(*["shield"] * 6), shield_cooldown_samples=20)
    assert gestures(w, 1, 1, 1, 1, 1, 1) == ["shield"] * 3  # ends 4 samples apart


def test_a_shield_that_gives_an_event_starts_its_own_cooldown():
    w = fed(ai=Answers("shield", "shield", "shield", "shield"), shield_cooldown_samples=20)
    # 3 event, 8 dropped, 13 event (within 3's cooldown), 18 dropped (5 after 13)
    assert gestures(w, 2, 2, 2, 2) == ["shield", "shield"]


def test_other_gestures_are_never_held_back_after_a_shield():
    w = fed(ai=Answers("shield", "mage", "hero", "rogue"), shield_cooldown_samples=20)
    assert gestures(w, 1, 1, 1, 1) == ["shield", "mage", "hero", "rogue"]


def test_a_warrior_with_no_shield_before_it_is_not_held_back():
    w = fed(ai=Answers("warrior", "warrior"), shield_cooldown_samples=20)
    assert gestures(w, 1, 1) == ["warrior", "warrior"]


def test_a_shield_or_warrior_after_the_cooldown_gives_an_event():
    w = fed(ai=Answers("shield", "warrior"), shield_cooldown_samples=10)
    assert gestures(w, 10, 1) == ["shield", "warrior"]  # ends at 3 and 14, 11 apart


def test_a_noise_answer_is_not_the_one_dropped():
    w = fed(ai=Answers("shield", None, "warrior", "warrior"), shield_cooldown_samples=20)
    assert gestures(w, 2, 2, 2, 1) == ["shield", "warrior"]  # 13 dropped, 18 kept


def test_each_node_has_its_own_shield_cooldown():
    w = fed(ai=Answers("shield", "shield"), shield_cooldown_samples=20)
    assert gestures(w, 1, node=1) == ["shield"]
    assert gestures(w, 1, node=2) == ["shield"]  # node 2 never had a shield


def test_after_a_reset_there_is_no_shield_cooldown():
    w = fed(ai=Answers("shield", "shield"), shield_cooldown_samples=20)
    results = []
    for seq in range(4):
        results.append(w.feed(packet(seq, flag=int(seq < 3), t=10.0 + seq * 0.01)))
    results.append(w.feed(packet(4, flag=1, t=13.0)))  # after a reconnect AI starts again
    results.append(w.feed(packet(5, t=13.01)))
    assert [r.label for r in results if r] == ["shield", "shield"]


def test_the_default_cooldown_is_2_s_at_100_hz_for_shield_and_warrior():
    assert gesture.SHIELD_COOLDOWN_SAMPLES == 200
    assert gesture.SHIELD_FOLLOWERS == ("shield", "warrior")
    assert gesture.Windows(FakeAI()).shield_cooldown_samples == gesture.SHIELD_COOLDOWN_SAMPLES
    assert set(gesture.SHIELD_FOLLOWERS) | {gesture.SHIELD} <= set(ai_events.GESTURES)


def test_a_gesture_the_glove_rejected_never_ends_and_the_next_one_still_does():
    w = fed()
    for seq, flag in enumerate((1, 1, 1, 2, 0, 1, 1, 0)):
        w.feed(packet(seq, flag=flag))
    assert ends_seen(w) == [False] * 7 + [True]
    assert resets(w) == []  # so AI still has a full buffer for the next gesture


def test_a_rejected_flag_with_no_gesture_flagged_is_an_ordinary_sample(caplog):
    w = fed()
    with caplog.at_level(logging.INFO, logger="gesture"):
        for seq, flag in enumerate((0, 2, 0)):
            w.feed(packet(seq, imu=(seq, 0, 0, 0, 0, 0), flag=flag))
    assert seqs_seen(w) == [0, 1, 2]
    assert ends_seen(w) == [False] * 3
    assert resets(w) == []
    assert no_result_lines(caplog) == []


def test_the_record_has_no_timestamp_when_the_message_has_none():
    w = fed()
    w.feed(packet(0))
    assert w.ai.calls[0][2]["timestamp"] is None


def test_an_outage_resets_ai_once_and_its_packets_never_reach_it():
    w = fed()
    for seq, status in enumerate((1, 1, 2, 2, 3, 1)):
        w.feed(packet(seq, imu=(seq, 0, 0, 0, 0, 0), status=status))
    assert [c[:2] if c[0] == "reset" else c[2]["ax"] for c in w.ai.calls] == [
        0, 1, ("reset", 1), 5,
    ]


def test_a_gap_on_one_node_resets_only_that_node():
    w = fed()
    w.feed(packet(0, node=1, t=10.0))
    w.feed(packet(0, node=2, t=10.0))
    w.feed(packet(1, node=2, t=10.4))
    w.feed(packet(1, node=1, t=13.0))
    assert [c[:2] for c in w.ai.calls if c[0] == "reset"] == [("reset", 1)]


def test_a_gesture_cut_by_a_reset_never_ends():
    """After a reset AI has none of the gesture, so the flag dropping must not end it."""
    w = fed()
    for seq in range(3):
        w.feed(packet(seq, flag=1, t=10.0 + seq * 0.01))
    w.feed(packet(3, t=13.0))  # a reconnect, and the flag dropped while it was away
    assert ends_seen(w) == [False] * 4


def test_ais_answer_becomes_a_result_with_the_time_it_took():
    w = fed(answer=("3", 0.5))
    assert w.feed(packet(0, flag=1)) is None
    result = w.feed(packet(1))
    assert (result.label, result.confidence, result.missing) == ("3", 0.5, 0)
    assert 0 <= result.t_infer_ms < 1000


def test_missing_counts_only_the_packets_lost_inside_the_gesture():
    w = fed(answer=("0", 1.0))
    seqs = [0, 5]  # 4 lost before the gesture starts at 5, which don't count
    seqs += list(range(6, 20)) + list(range(22, 30))  # 20 and 21 lost inside it
    for seq in seqs:
        assert w.feed(packet(seq, flag=int(seq >= 5))) is None
    result = w.feed(packet(31))  # 30, the last flagged one, lost too
    assert result.missing == 3


def no_result_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "gesture"]


def test_a_flagged_gesture_ai_gives_nothing_for_is_logged_once(caplog):
    w = fed()
    with caplog.at_level(logging.INFO, logger="gesture"):
        for seq in range(5):
            w.feed(packet(seq, flag=1))
        w.feed(packet(5))
        w.feed(packet(6))
    assert no_result_lines(caplog) == ["no AI event: node 1's flagged gesture of 5 samples gave no result"]
    assert caplog.records[0].levelno == logging.INFO


def test_a_gesture_the_glove_rejected_is_logged_once(caplog):
    w = fed(answer=("3", 0.5))
    with caplog.at_level(logging.INFO, logger="gesture"):
        for seq, flag in enumerate((1, 1, 1, 2, 0, 0)):
            assert w.feed(packet(seq, flag=flag)) is None
    assert no_result_lines(caplog) == ["no AI event: node 1's glove rejected its flagged gesture of 3 samples"]
    assert caplog.records[0].levelno == logging.INFO


def test_nothing_is_logged_for_an_answer_or_an_unflagged_stream(caplog):
    w = fed(answer=("3", 0.5))
    with caplog.at_level(logging.INFO, logger="gesture"):
        for seq in range(2):
            w.feed(packet(seq, flag=1))
        assert w.feed(packet(2)) is not None
        for seq in range(10, 30):
            w.feed(packet(seq, node=2))  # never flagged
    assert no_result_lines(caplog) == []


def test_nothing_is_logged_for_a_gesture_cut_by_a_reset(caplog):
    w = fed()
    with caplog.at_level(logging.INFO, logger="gesture"):
        for seq in range(5):
            w.feed(packet(seq, flag=1, t=10.0 + seq * 0.01))
        w.feed(packet(5, t=13.0))  # after a reconnect: AI was reset, the gesture didn't end
        for seq in range(6, 10):
            w.feed(packet(seq, flag=1, status=2 if seq == 7 else 1, t=13.0 + seq * 0.01))
        w.feed(packet(10, t=13.1))  # the outage at 7 cut the first part off
    assert no_result_lines(caplog) == ["no AI event: node 1's flagged gesture of 2 samples gave no result"]


def test_a_failed_gesture_is_not_also_logged_as_no_result(caplog):
    class Fails(FakeAI):
        def on_sample(self, player, record):
            if record["end_of_gesture"]:
                raise gesture.ClassifierError("DMA busy")

    w = fed(ai=Fails())
    with caplog.at_level(logging.INFO, logger="gesture"):
        for seq in range(30):
            w.feed(packet(seq, flag=1))
        with pytest.raises(gesture.ClassifierError):
            w.feed(packet(30))
    assert no_result_lines(caplog) == []


def test_a_new_session_empties_ais_buffers_and_has_no_gesture_in_progress():
    ai = FakeAI(answer=("0", 1.0))  # the model lives for the whole run, the windows per session
    first = gesture.Windows(ai)
    for seq in range(30):
        first.feed(packet(seq, flag=1))
    ai.calls.clear()
    second = gesture.Windows(ai)
    assert ai.calls == [("reset", None)]
    assert second.feed(packet(30)) is None  # the old session's gesture would end here
    assert ai.calls[-1][2]["end_of_gesture"] is False



# --- AI's model, through gesture.AI ------------------------------------------

AI_STREAM = Path(__file__).resolve().parents[3] / "AI" / "ultra96" / f"{gesture.AI_MODULE}.py"


@dataclasses.dataclass
class FakeSample:
    ax: int
    ay: int
    az: int
    gx: int
    gy: int
    gz: int
    end_of_gesture: bool = False
    timestamp: float = None


class FakeModel:
    """Stands in for wk10_integration.GestureClassifier."""

    def __init__(self, result=None, error=None):
        self.calls, self.result, self.error = [], result, error

    def feed(self, sample, player=None):
        self.calls.append(("feed", sample, player))
        if self.error:
            raise self.error
        return self.result

    def reset(self, player=None):
        self.calls.append(("reset", player))


def adapter(model):
    return gesture.AI(types.SimpleNamespace(IMUSample=FakeSample), model)


def test_the_adapter_feeds_ais_imu_sample_with_the_player_and_returns_label_and_confidence():
    model = FakeModel(result=types.SimpleNamespace(gesture="circle", confidence=0.75))
    record = gesture.to_record(dict(zip(gesture.CHANNELS, range(6))), {"timestamp": 3.5}, True)
    assert adapter(model).on_sample(2, record) == ("circle", 0.75)
    assert model.calls == [("feed", FakeSample(0, 1, 2, 3, 4, 5, True, 3.5), 2)]


def test_no_result_from_ai_is_none():
    record = gesture.to_record(dict(ZERO), {}, False)
    assert adapter(FakeModel()).on_sample(1, record) is None


def test_a_dma_stall_in_ai_becomes_a_classifier_error_so_the_session_survives():
    ai = adapter(FakeModel(error=RuntimeError("DMA RECV stalled after 3.0s")))
    with pytest.raises(gesture.ClassifierError, match="DMA RECV stalled"):
        ai.on_sample(1, gesture.to_record(dict(ZERO), {}, True))


def test_reset_reaches_ai_for_one_player_or_all():
    model = FakeModel()
    ai = adapter(model)
    ai.reset(2)
    ai.reset()
    assert model.calls == [("reset", 2), ("reset", None)]


def test_the_stub_ai_answers_only_at_the_end_of_a_gesture():
    stub = gesture.StubAI()
    record = gesture.to_record(dict(zip(gesture.CHANNELS, range(6))), {}, False)
    assert stub.on_sample(1, record) is None
    assert stub.on_sample(1, {**record, "end_of_gesture": True})[0] in ai_events.GESTURES


def ai_class(name):
    tree = ast.parse(AI_STREAM.read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == name)


IMU_H = Path(__file__).resolve().parents[2] / "firebeetle" / "imu.h"

# counts per unit at each MPU6050 full-scale range, from its datasheet
ACCEL_LSB_PER_G_AT = {2: 16384, 4: 8192, 8: 4096, 16: 2048}
GYRO_LSB_PER_DPS_AT = {250: 131, 500: 65.5, 1000: 32.8, 2000: 16.4}


def ai_constant(name):
    tree = ast.parse(AI_STREAM.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == name)
    return ast.literal_eval(node.value)


def test_ais_model_decodes_counts_at_the_ranges_the_firmware_sets():
    """A range mismatch hands the model values at the wrong size, with no error."""
    ranges = dict(re.findall(r"constexpr int16_t (IMU_\w+_RANGE_\w+) = (\d+);", IMU_H.read_text()))
    accel = ACCEL_LSB_PER_G_AT[int(ranges["IMU_ACCEL_RANGE_G"])]
    gyro = GYRO_LSB_PER_DPS_AT[int(ranges["IMU_GYRO_RANGE_DPS"])]
    assert ai_constant("ACCEL_TO_M_S2") == pytest.approx(9.80665 / accel, rel=1e-9)
    assert ai_constant("GYRO_TO_RAD_S") == pytest.approx(math.radians(1) / gyro, rel=1e-9)


def test_comms_fills_exactly_ais_imu_sample_fields():
    """AI's file needs numpy, which the laptop lacks, so its contract is read from the source."""
    fields = {n.target.id for n in ai_class("IMUSample").body if isinstance(n, ast.AnnAssign)}
    assert set(gesture.to_record(dict(ZERO), {}, False)) == fields


def test_ais_feed_and_reset_take_a_player():
    body = ai_class("GestureClassifier").body
    methods = {n.name: n for n in body if isinstance(n, ast.FunctionDef)}
    for name in ("feed", "reset"):
        assert "player" in [a.arg for a in methods[name].args.args], name


def test_the_fpga_path_needs_a_bitstream(monkeypatch, tmp_path):
    monkeypatch.setattr(gesture, "FPGA_MANAGER", tmp_path)  # pretend we are on the board
    with pytest.raises(ValueError):
        gesture.load_classifier()  # no --bitstream given


def test_off_the_board_the_flag_path_gets_the_stub(monkeypatch, tmp_path):
    monkeypatch.setattr(gesture, "FPGA_MANAGER", tmp_path / "absent")
    assert isinstance(gesture.load_classifier(), gesture.StubAI)


def test_on_the_board_the_flag_path_loads_ais_model_with_its_bitstream(monkeypatch, tmp_path):
    built = []
    module = types.ModuleType(gesture.AI_MODULE)
    module.__file__ = str(tmp_path / f"{gesture.AI_MODULE}.py")
    module.GestureClassifier = lambda bitstream, config=None: built.append(bitstream) or "model"
    module.vocab_config = lambda vocab=None: "config"
    monkeypatch.setitem(sys.modules, gesture.AI_MODULE, module)
    monkeypatch.setattr(gesture, "FPGA_MANAGER", tmp_path)
    ai = gesture.load_classifier(bitstream="gesture_model.xsa")
    assert isinstance(ai, gesture.AI) and (ai.module, ai.model) == (module, "model")
    assert built == [str(tmp_path / "gesture_model.xsa")]


def test_the_pl_clock_is_set_only_after_the_overlay_loads(monkeypatch, tmp_path):
    """Loading an overlay resets fclk0, so setting it first would be undone at once."""
    order = []
    module = types.ModuleType(gesture.AI_MODULE)
    module.__file__ = str(tmp_path / f"{gesture.AI_MODULE}.py")
    module.GestureClassifier = lambda bitstream, config=None: order.append("overlay") or "model"
    module.vocab_config = lambda vocab=None: "config"
    monkeypatch.setitem(sys.modules, gesture.AI_MODULE, module)
    monkeypatch.setattr(gesture, "FPGA_MANAGER", tmp_path)
    monkeypatch.setattr(gesture, "tune_pl", lambda mhz: order.append(("pl", mhz)) or "settings")

    ai = gesture.load_classifier(bitstream="x.xsa", pl_mhz=25.0)

    assert order == ["overlay", ("pl", 25.0)] and ai.pl_settings == "settings"
    order.clear()
    gesture.load_classifier(bitstream="x.xsa")
    assert order == ["overlay"]  # no --pl-mhz: the clock is left as the overlay set it


def test_off_the_board_the_pl_clock_is_ignored_with_a_warning(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(gesture, "FPGA_MANAGER", tmp_path / "absent")
    monkeypatch.setattr(gesture, "tune_pl", lambda mhz: pytest.fail("no FPGA to slow down"))

    assert isinstance(gesture.load_classifier(pl_mhz=25.0), gesture.StubAI)
    assert "--pl-mhz ignored" in caplog.text


def test_a_pl_clock_the_board_refuses_leaves_the_game_running(monkeypatch, caplog, tmp_path):
    import board_settings

    class Refusing:
        def __init__(self, state_file):
            pass

        def apply(self, wanted):
            raise ValueError("pl=150: keep it between 10 and the current 100.0 MHz")

    monkeypatch.setattr(board_settings, "Settings", Refusing)
    assert gesture.tune_pl(150.0) is None
    assert "PL clock not changed: pl=150" in caplog.text


def test_both_clients_take_pl_mhz():
    import argparse

    import tcp_client

    parser = argparse.ArgumentParser()
    tcp_client.add_gesture_flag(parser)
    assert parser.parse_args(["--pl-mhz", "25"]).pl_mhz == 25.0
    assert parser.parse_args([]).pl_mhz is None


def test_on_the_board_a_model_that_will_not_import_is_an_error_not_the_stub(monkeypatch, tmp_path):
    monkeypatch.setattr(gesture, "FPGA_MANAGER", tmp_path)
    monkeypatch.setitem(sys.modules, gesture.AI_MODULE, None)  # importing it now raises ImportError
    with pytest.raises(ImportError):
        gesture.load_classifier(bitstream="x.xsa")


# --- the log fields ----------------------------------------------------------


def test_log_fields_are_empty_for_a_dummy_event():
    assert gesture.log_fields(None) == ""
    assert gesture.log_fields(gesture.Result("3", 0.5, 0.8125, 2)) == (
        " conf=0.500 t_infer_ms=0.812 missing=2"
    )
