"""Dataset: load the per-gesture int16 CSVs, decode to SI, split, and serve windows.

A DataConfig says which folder + which labels; the label map is derived from it (no
globals, no monkeypatching). GestureStream turns gestures into augmented WIN-step windows
on the fly; make_sequence wraps it as a Keras Sequence (adding the feature channels when
channels == 10). build_test produces a fixed, reused test set.

load_background / split_background read the real background (the unlabelled time) out of
the continuous recordings, for AugConfig.filler="background" and the slide_bg test.
"""
import csv
import glob
import json
import os

import numpy as np

from .config import (WIN, GEST, CROP, RAW_PERIOD_US, ACC_SCALE, GYR_SCALE, DATA_ROOT, NOISE,
                     DataConfig, AugConfig)
from . import augment

COUNT_CHANNELS = ["ax_counts", "ay_counts", "az_counts", "gx_counts", "gy_counts", "gz_counts"]
SESSION_CHANNELS = ["ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps"]   # record.py CSV
SESSION_COUNTS = np.array([8192, 8192, 8192, 65.5, 65.5, 65.5])            # g, deg/s -> counts
DECIMATE = 5                                                                 # 500 Hz -> 100 Hz


def _decode(counts):
    out = np.empty(counts.shape, dtype=np.float64)
    out[:, 0:3] = counts[:, 0:3] * ACC_SCALE
    out[:, 3:6] = counts[:, 3:6] * GYR_SCALE
    return out


def load(data_cfg: DataConfig):
    """Read the dataset -> list of {si (T,6) SI, label (int), strata}."""
    lmap = data_cfg.label_map()
    rows = list(csv.DictReader(open(os.path.join(data_cfg.path(), "index.csv"), newline="")))
    items = []
    for r in rows:
        # exported `noise` intervals are random gestures, never the noise class (that is background)
        if r["label"] not in lmap or r["label"] == NOISE:
            continue
        path = os.path.join(data_cfg.path(), r["file"])
        if not os.path.exists(path):
            continue
        rr = list(csv.DictReader(open(path, newline="")))
        counts = np.array([[int(x[c]) for c in COUNT_CHANNELS] for x in rr], dtype=np.float64)
        if counts.ndim != 2 or counts.shape[1] != 6 or len(counts) < 2:
            continue
        items.append(dict(si=_decode(counts), label=lmap[r["label"]],
                          strata="|".join((r["label"], r.get("drawing_mode", ""), r.get("direction", "")))))
    return items


def stratified_split(items, data_cfg: DataConfig):
    rng = np.random.default_rng(data_cfg.split_seed)
    strata = np.array([it["strata"] for it in items])
    tr, va = data_cfg.ratios[0], data_cfg.ratios[1]
    out = {"train": [], "val": [], "test": []}
    for s in np.unique(strata):
        idx = np.where(strata == s)[0]
        rng.shuffle(idx)
        n = len(idx); n_tr = round(n * tr); n_va = round(n * va)
        out["train"] += list(idx[:n_tr])
        out["val"]   += list(idx[n_tr:n_tr + n_va])
        out["test"]  += list(idx[n_tr + n_va:])
    return {k: [items[i] for i in v] for k, v in out.items()}


def _session_background(csv_path, intervals, margin_us):
    """One recording -> its background runs: (T,6) SI arrays at 100 Hz, clear of EVERY
    labelled interval by margin_us and split at sensor gaps. That includes `noise` intervals:
    in the noise recordings those are random gestures of the 7 classes, so they are neither
    background nor a class. Encoded exactly as the gesture set: every 5th 500 Hz sample, rounded
    to int16 counts, decoded with ACC/GYR_SCALE."""
    import pandas as pd
    df = pd.read_csv(csv_path, usecols=["esp_time_us", *SESSION_CHANNELS])
    t = df["esp_time_us"].to_numpy(np.int64)
    x = df[SESSION_CHANNELS].to_numpy(np.float64)
    keep = np.ones(len(t), bool)
    for iv in intervals:                                             # symbols, noise AND ignore
        keep &= ~((t >= iv["start_us"] - margin_us) & (t < iv["end_us"] + margin_us))
    # a run ends at a sensor gap or where `keep` flips
    starts = np.flatnonzero(np.r_[True, (np.diff(t) != RAW_PERIOD_US) | (keep[1:] != keep[:-1])])
    runs = []
    for s, e in zip(starts, np.r_[starts[1:], len(t)]):
        if keep[s] and (e - s) // DECIMATE >= WIN:
            counts = np.clip(np.round(x[s:e:DECIMATE] * SESSION_COUNTS), -32768, 32767)
            runs.append(_decode(counts))
    return runs


def load_background(data_cfg: DataConfig):
    """Background runs (list of (T,6) SI arrays): the unlabelled time of every reviewed
    recording under sessions_dir (subfolders included; gesture AND noise recordings), or None if
    there are none. Unreviewed recordings are skipped: they may hide unmarked gestures. Cached in
    DATA_ROOT (reading the 500 Hz CSVs is slow); rebuilt when the set of recordings changes, used
    as-is if the recordings are absent."""
    # v2: noise-labelled intervals are excluded (v1 counted them as background)
    cache = os.path.join(DATA_ROOT, f"background_v2_m{data_cfg.bg_margin_us // 1000}.npz")
    labels = sorted(glob.glob(os.path.join(data_cfg.sessions_dir, "**", "*.labels.json"), recursive=True))
    names = [os.path.relpath(p, data_cfg.sessions_dir) for p in labels]
    if os.path.exists(cache):
        z = np.load(cache)
        if not labels or list(z["names"]) == names:
            return np.split(z["x"], np.cumsum(z["lens"])[:-1])
    if not labels:
        return None
    runs = []
    for lp in labels:
        cp = lp[:-len(".labels.json")] + ".csv"
        doc = json.load(open(lp))
        if os.path.exists(cp) and doc.get("reviewed"):
            runs += _session_background(cp, doc["intervals"], data_cfg.bg_margin_us)
    os.makedirs(DATA_ROOT, exist_ok=True)
    np.savez(cache, x=np.concatenate(runs), lens=[len(r) for r in runs], names=names)
    return runs


def split_background(runs, data_cfg: DataConfig):
    """Chop the runs into bg_block-step blocks (a shorter tail kept if >= WIN) and deal them
    into train/val/test by data_cfg.ratios, shuffled with split_seed, so test filler is never
    seen in training."""
    b = data_cfg.bg_block
    blocks = [r[s:s + b].astype(np.float32) for r in runs for s in range(0, len(r), b)
              if len(r[s:s + b]) >= WIN]
    np.random.default_rng(data_cfg.split_seed).shuffle(blocks)
    n_tr = round(len(blocks) * data_cfg.ratios[0]); n_va = round(len(blocks) * data_cfg.ratios[1])
    return {"train": blocks[:n_tr], "val": blocks[n_tr:n_tr + n_va], "test": blocks[n_tr + n_va:]}


class GestureStream:
    """One split's gestures + a clean pool (for filler & per-channel std). Produces fresh
    augmented (WIN, channels) windows on demand. channels 6 = raw, 10 = + feature channels.
    `bg` (that split's background blocks) makes the filler real background instead of
    gesture chunks. `n_noise` > 0 appends that many noise-class slots (label `noise_label`),
    each a fresh augment.noise_window from `bg` every time it is drawn."""

    def __init__(self, items, aug_cfg: AugConfig, channels=6, augment_on=False, seed=0, bg=None,
                 n_noise=0, noise_label=None):
        if n_noise and not bg:
            raise ValueError("noise windows need background blocks (bg)")
        self.bg = bg
        self.items = items
        self.cfg = aug_cfg
        self.channels = channels
        self.augment_on = augment_on
        self.rng = np.random.default_rng(seed)
        self.y = np.array([it["label"] for it in items] + [noise_label] * n_noise, dtype=np.int64)
        self.pool = [augment.to_gesture(it["si"], self.rng, aug_cfg, None, augment=False) for it in items]
        self.ch_std = np.concatenate(self.pool).std(axis=0)   # native pool lengths vary

    def __len__(self):
        return len(self.y)                                     # gestures + noise slots

    def window(self, i, offset=None, shift=None):
        """A window for item i. offset = absolute start step; shift = steps from the
        reference position (augment.ref_offset); neither = random placement. An i past the
        gestures is a noise slot: a fresh background-only window."""
        if i >= len(self.items):
            w = augment.noise_window(self.bg, self.pool, self.rng, self.ch_std, self.cfg, self.augment_on)
            return augment.add_features(w) if self.channels == 10 else w
        g = augment.to_gesture(self.items[i]["si"], self.rng, self.cfg, self.ch_std, self.augment_on)
        if shift is not None:
            offset = augment.ref_offset(len(g)) + shift
        w = augment.make_window(g, self.pool, self.rng, self.ch_std, self.cfg, offset, self.bg)
        return augment.add_features(w) if self.channels == 10 else w

    def fragment(self, i, vis, where="head"):
        """A window with exactly `vis` clean gesture steps visible (for the leakage sweep)."""
        g = augment.to_gesture(self.items[i]["si"], self.rng, self.cfg, self.ch_std, False)
        w = augment.make_fragment(g, self.pool, self.rng, self.ch_std, self.cfg, vis, where, self.bg)
        return augment.add_features(w) if self.channels == 10 else w


def noise_count(items, data_cfg: DataConfig):
    """Noise slots for a split: noise_ratio x its largest gesture class (0 without the noise class)."""
    if not data_cfg.noise or not items:
        return 0
    return round(data_cfg.noise_ratio * np.bincount([it["label"] for it in items]).max())


def make_sequence(items, aug_cfg, channels, augment_on, batch, mult, seed, bg=None,
                  n_noise=0, noise_label=None):
    """Keras Sequence over GestureStream; reshuffles + re-randomises each epoch. `bg` = that
    split's background blocks (filler="background"), else gesture-chunk filler. n_noise noise
    slots (label noise_label) join the gestures, each also repeated `mult` times."""
    import math as _math
    from tensorflow.keras.utils import Sequence

    class _Seq(Sequence):
        def __init__(self):
            self.stream = GestureStream(items, aug_cfg, channels, augment_on, seed, bg,
                                        n_noise, noise_label)
            self._order = np.repeat(np.arange(len(self.stream)), mult)
            if augment_on:
                self.stream.rng.shuffle(self._order)

        def __len__(self):
            return _math.ceil(len(self._order) / batch)

        def __getitem__(self, b):
            idx = self._order[b * batch:(b + 1) * batch]
            X = np.stack([self.stream.window(i) for i in idx]).astype(np.float32)
            return X, self.stream.y[idx]

        def on_epoch_end(self):
            if augment_on:
                self.stream.rng.shuffle(self._order)

    return _Seq()


def build_test(test_items, aug_cfg, channels, seed=777, mult_j=4):
    """Fixed, never-augmented test windows (the original test, kept so runs stay comparable):
    jitter = random start offsets in -CROP..CROP x mult_j, ceiling = centred."""
    st = GestureStream(test_items, aug_cfg, channels, augment_on=False, seed=seed)
    rng = np.random.default_rng(seed)
    Xj, yj, Xc, yc = [], [], [], []
    for _ in range(mult_j):
        for i in range(len(st)):
            Xj.append(st.window(i, offset=int(rng.integers(-CROP, CROP + 1)))); yj.append(st.y[i])
    for i in range(len(st)):
        Xc.append(st.window(i, shift=0)); yc.append(st.y[i])
    return np.stack(Xj).astype(np.float32), np.array(yj), np.stack(Xc).astype(np.float32), np.array(yc)


def build_slide_test(test_items, aug_cfg, channels, seed=778, positions=5, bg=None):
    """Fixed sliding test, alongside build_test: every test gesture at `positions` evenly spaced
    start offsets across augment.slide_range (head cut by slide_max_out ... tail cut by the same),
    filler both sides, never augmented. The same rule for resampled and native models, so they
    compare. Gesture-chunk filler, or real background with `bg` (the test blocks -> slide_bg).
    Returns X, y and each window's position index (0 = head cut, last = tail cut)."""
    st = GestureStream(test_items, aug_cfg, channels, augment_on=False, seed=seed, bg=bg)
    X, y, pos = [], [], []
    for i in range(len(st)):
        first, last = augment.slide_range(len(st.pool[i]), aug_cfg)   # pool = this item, unaugmented
        for k, off in enumerate(np.linspace(first, last, positions).round().astype(int)):
            X.append(st.window(i, offset=int(off))); y.append(st.y[i]); pos.append(k)
    return np.stack(X).astype(np.float32), np.array(y), np.array(pos)


def build_noise_test(test_items, aug_cfg, channels, bg_test, n, noise_label, seed=779):
    """Fixed noise test: n never-augmented background-only windows from the TEST background
    blocks (never trained on), labelled noise_label (pass -1 for a model without the class:
    every prediction is then a false accept). test_items only supply the filler pool/ch_std."""
    st = GestureStream(test_items, aug_cfg, channels, augment_on=False, seed=seed, bg=bg_test,
                       n_noise=n, noise_label=noise_label)
    k = len(test_items)
    return np.stack([st.window(k + i) for i in range(n)]).astype(np.float32), st.y[k:]
