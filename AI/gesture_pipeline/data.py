"""Dataset: load the per-gesture int16 CSVs, decode to SI, split, and serve windows.

A DataConfig says which folder + which labels; the label map is derived from it (no
globals, no monkeypatching). GestureStream turns gestures into augmented WIN-step windows
on the fly; make_sequence wraps it as a Keras Sequence (adding the feature channels when
channels == 10). build_test produces a fixed, reused test set.
"""
import csv
import os

import numpy as np

from .config import WIN, GEST, CROP, RAW_PERIOD_US, ACC_SCALE, GYR_SCALE, DataConfig, AugConfig
from . import augment

COUNT_CHANNELS = ["ax_counts", "ay_counts", "az_counts", "gx_counts", "gy_counts", "gz_counts"]


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
        if r["label"] not in lmap:
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


class GestureStream:
    """One split's gestures + a clean pool (for filler & per-channel std). Produces fresh
    augmented (WIN, channels) windows on demand. channels 6 = raw, 10 = + feature channels."""

    def __init__(self, items, aug_cfg: AugConfig, channels=6, augment_on=False, seed=0):
        self.items = items
        self.cfg = aug_cfg
        self.channels = channels
        self.augment_on = augment_on
        self.rng = np.random.default_rng(seed)
        self.y = np.array([it["label"] for it in items], dtype=np.int64)
        self.pool = [augment.to_gesture(it["si"], self.rng, aug_cfg, None, augment=False) for it in items]
        self.ch_std = np.stack(self.pool).reshape(-1, 6).std(axis=0)

    def __len__(self):
        return len(self.items)

    def window(self, i, offset=None):
        g = augment.to_gesture(self.items[i]["si"], self.rng, self.cfg, self.ch_std, self.augment_on)
        w = augment.make_window(g, self.pool, self.rng, self.ch_std, self.cfg, offset)
        return augment.add_features(w) if self.channels == 10 else w

    def fragment(self, i, vis, where="head"):
        """A window with exactly `vis` clean gesture steps visible (for the leakage sweep)."""
        g = augment.to_gesture(self.items[i]["si"], self.rng, self.cfg, self.ch_std, False)
        w = augment.make_fragment(g, self.pool, self.rng, self.ch_std, self.cfg, vis, where)
        return augment.add_features(w) if self.channels == 10 else w


def make_sequence(items, aug_cfg, channels, augment_on, batch, mult, seed):
    """Keras Sequence over GestureStream; reshuffles + re-randomises each epoch."""
    import math as _math
    from tensorflow.keras.utils import Sequence

    class _Seq(Sequence):
        def __init__(self):
            self.stream = GestureStream(items, aug_cfg, channels, augment_on, seed)
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
    """Fixed, never-augmented test windows: jitter = random offsets x mult_j, ceiling = centred."""
    st = GestureStream(test_items, aug_cfg, channels, augment_on=False, seed=seed)
    rng = np.random.default_rng(seed); center = (WIN - GEST) // 2
    Xj, yj, Xc, yc = [], [], [], []
    for _ in range(mult_j):
        for i in range(len(st)):
            Xj.append(st.window(i, offset=int(rng.integers(-CROP, CROP + 1)))); yj.append(st.y[i])
    for i in range(len(st)):
        Xc.append(st.window(i, offset=center)); yc.append(st.y[i])
    return np.stack(Xj).astype(np.float32), np.array(yj), np.stack(Xc).astype(np.float32), np.array(yc)
