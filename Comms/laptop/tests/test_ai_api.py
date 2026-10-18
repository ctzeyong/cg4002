"""ai_api.Gestures, the placeholder per-sample API: buffering, the 1 to 0 edge and resampling.

AI's to own once they take the file over. notes/integration/gesture_detection.md §8.
"""

import pytest

import gesture  # noqa: E402 - ultra96/, on sys.path via conftest

ai_api = gesture.ai_api


class Model:
    """A model that keeps every window it was given."""

    def __init__(self, fail_first=False):
        self.windows = []
        self.timeouts = []
        self.fail_first = fail_first

    def classify(self, samples, timeout=None):
        self.windows.append(samples)
        self.timeouts.append(timeout)
        if self.fail_first and len(self.windows) == 1:
            raise ai_api.ClassifyError("DMA busy")
        return "2", 0.9


def record(ax=0, flag=1, t=0.0):
    return {"ax": ax, "ay": 1, "az": 2, "gx": 3, "gy": 4, "gz": 5, "in_gesture": flag, "timestamp": t}


def gesture_of(gestures, n, player=1, start=0):
    """Feed n flagged samples with ax counting up from start, then one unflagged one."""
    for i in range(n):
        assert gestures.on_sample(player, record(ax=start + i)) is None
    return gestures.on_sample(player, record(flag=0), timeout=0.05)


def test_a_gesture_is_classified_once_when_its_flag_drops():
    model = Model()
    g = ai_api.Gestures(model)
    assert gesture_of(g, 50) == ("2", 0.9)
    assert g.on_sample(1, record(flag=0)) is None  # still down, nothing more
    assert len(model.windows) == 1
    assert model.timeouts == [0.05]


def test_an_unflagged_stream_is_never_classified():
    model = Model()
    g = ai_api.Gestures(model)
    for _ in range(300):
        assert g.on_sample(1, record(flag=0)) is None
    assert model.windows == []


def test_the_model_gets_the_gesture_oldest_first_stretched_to_its_window():
    model = Model()
    gesture_of(ai_api.Gestures(model), 50)
    ax = [s["ax"] for s in model.windows[0]]
    assert len(ax) == ai_api.WINDOW
    assert (ax[0], ax[-1]) == (0, 49)
    assert ax == sorted(ax)
    assert set(model.windows[0][0]) == set(ai_api.CHANNELS)  # no flag or timestamp


def test_a_gesture_shorter_than_the_minimum_gives_nothing():
    model = Model()
    g = ai_api.Gestures(model)
    assert gesture_of(g, ai_api.MIN_SAMPLES - 1) is None
    assert gesture_of(g, ai_api.MIN_SAMPLES) is not None
    assert len(model.windows) == 1


def test_a_stuck_flag_keeps_only_the_last_samples():
    model = Model()
    gesture_of(ai_api.Gestures(model), ai_api.MAX_SAMPLES + 50)
    ax = [s["ax"] for s in model.windows[0]]
    assert (ax[0], ax[-1]) == (50, ai_api.MAX_SAMPLES + 49)


def test_players_have_separate_gestures():
    model = Model()
    g = ai_api.Gestures(model)
    for i in range(30):
        g.on_sample(1, record(ax=i))
        g.on_sample(2, record(ax=1000 + i))
    assert g.on_sample(2, record(flag=0)) is not None
    assert [s["ax"] for s in model.windows[0]][-1] == 1029
    assert g.on_sample(1, record(flag=0)) is not None
    assert [s["ax"] for s in model.windows[1]][-1] == 29


def test_reset_forgets_only_that_players_gesture():
    model = Model()
    g = ai_api.Gestures(model)
    for i in range(30):
        g.on_sample(1, record(ax=i))
        g.on_sample(2, record(ax=i))
    g.reset(1)
    g.reset(3)  # a player with nothing in progress
    assert g.on_sample(1, record(flag=0)) is None
    assert g.on_sample(2, record(flag=0)) is not None


def test_a_failed_gesture_is_not_tried_again():
    model = Model(fail_first=True)
    g = ai_api.Gestures(model)
    with pytest.raises(ai_api.ClassifyError):
        gesture_of(g, 30)
    assert g.on_sample(1, record(flag=0)) is None
    assert gesture_of(g, 30) == ("2", 0.9)


# --- resample -----------------------------------------------------------------


def samples(axs):
    return [dict.fromkeys(ai_api.CHANNELS, 0) | {"ax": ax} for ax in axs]


def test_a_window_of_the_right_length_is_unchanged():
    given = samples(range(ai_api.WINDOW))
    assert ai_api.resample(given) == given


def test_resampling_keeps_both_ends_and_interpolates_between():
    out = [s["ax"] for s in ai_api.resample(samples([0, 99]))]
    assert out == list(range(100))


def test_a_long_gesture_is_squeezed_evenly():
    out = [s["ax"] for s in ai_api.resample(samples(range(0, 397)), steps=100)]
    assert out == [4 * i for i in range(100)]


def test_resampled_values_are_whole_counts_rounded_away_from_zero():
    assert [s["ax"] for s in ai_api.resample(samples([0, 1]), steps=3)] == [0, 1, 1]
    assert [s["ax"] for s in ai_api.resample(samples([0, -1]), steps=3)] == [0, -1, -1]
    assert [s["ax"] for s in ai_api.resample(samples([0, 10]), steps=4)] == [0, 3, 7, 10]
    assert all(isinstance(v, int) for s in ai_api.resample(samples([0, 7, 3])) for v in s.values())


def test_one_sample_is_repeated():
    out = ai_api.resample(samples([7]), steps=5)
    assert [s["ax"] for s in out] == [7] * 5
