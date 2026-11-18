"""Evaluate a saved .h5: per-class recall, confusion matrix, the offset-robustness sweep,
and the visible-length leakage probe. Replaces the old eval_perclass / confusion one-offs
and carries over gesture_real_baseline_v3's eval_offsets / eval_visible_lengths.

    python -m train.exp_eval --model results/models/new_lean_s1.h5 --resample
    python -m train.exp_eval --model results/models/new_lean_native_s1.h5
    python -m train.exp_eval --model results/models/feat_s1.h5 --channels 10 --resample

Pass --resample for a model trained with AugConfig.resample=True (every model trained before
the switch existed), so the test windows match how it was trained. A model with one output more
than the vocabulary is taken as having the noise class (last index).
"""
import argparse

import numpy as np

from .config import DataConfig, AugConfig, OLD_LABELS, NOISE, SESSIONS_DIR, VOCABS, parse_labels
from . import data as D
from . import evaluate as E

THRESHOLDS = (0.0, 0.5, 0.7, 0.9)    # confidence thresholds for the no-event trade-off table


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--data", default="new_gesture_100hz")
    ap.add_argument("--channels", type=int, default=6)
    ap.add_argument("--old", action="store_true", help="old 7-class vocab (arc..z) = --labels shapes")
    ap.add_argument("--labels", type=parse_labels, default=parse_labels("new"),
                    help=f"the model's classes: a vocabulary {list(VOCABS)} or a comma list (default: new)")
    ap.add_argument("--sessions", nargs="+", default=[SESSIONS_DIR],
                    help="recordings the model's background came from: folders, recordings or patterns (default: SESSIONS_DIR)")
    ap.add_argument("--resample", action="store_true", help="model was trained with resample=True")
    a = ap.parse_args()
    from qkeras.utils import load_qmodel

    dc = DataConfig(dir=a.data, labels=(list(OLD_LABELS) if a.old else a.labels), sessions_dir=a.sessions)
    model = load_qmodel(a.model)
    dc.noise = int(model.output_shape[-1]) == len(dc.labels) + 1
    names = dc.names()
    noise_label = names.index(NOISE) if dc.noise else None
    sp = D.stratified_split(D.load(dc), dc)
    aug = AugConfig(resample=a.resample)
    print(f"model {a.model}  channels={a.channels}  length={'resample' if a.resample else 'native'}"
          f"  classes={names}")

    Xj, yj, _, _ = D.build_test(sp["test"], aug, a.channels)
    pj = E.predict(model, Xj)
    print("\nper-class recall (jitter):")
    for k, v in E.per_class(pj, yj, names).items():
        print(f"  {k:10s} {v:.3f}" if v is not None else f"  {k:10s}   -")
    row, pairs = E.confusion(pj, yj, names)
    print("\n" + E.format_confusion(row, names))
    print("worst pairs:", ", ".join(f"{a_}->{b} {r:.1%}" for r, a_, b in pairs[:5]))

    Xs, ys, ps = D.build_slide_test(sp["test"], aug, a.channels)
    pred_s = E.predict(model, Xs)
    print(f"\nsliding test (up to {aug.slide_max_out:.0%} cut off either edge): "
          f"{(pred_s == ys).mean():.3f}")
    for k, acc in E.by_position(pred_s, ys, ps).items():
        where = "head cut" if k == 0 else "tail cut" if k == ps.max() else ""
        print(f"  position {k}: {acc:.3f}  {where}")

    runs = D.load_background(dc)
    if runs:
        bg_test = D.split_background(runs, dc)["test"]
        Xb, yb, pb = D.build_slide_test(sp["test"], aug, a.channels, bg=bg_test)
        prob_b = E.probs(model, Xb)
        pred_b = prob_b.argmax(1)
        print(f"\nsliding test on real background (slide_bg): {(pred_b == yb).mean():.3f}")
        for k, acc in E.by_position(pred_b, yb, pb).items():
            where = "head cut" if k == 0 else "tail cut" if k == pb.max() else ""
            print(f"  position {k}: {acc:.3f}  {where}")

        # no-gesture windows: held-out background only. Without the noise class every one of
        # them is a gesture prediction, so only the confidence threshold can reject it.
        Xn, _ = D.build_noise_test(sp["test"], aug, a.channels, bg_test, int(np.bincount(yj).max()),
                                   -1 if noise_label is None else noise_label)
        prob_n = E.probs(model, Xn)
        print(f"\nno-gesture test ({len(Xn)} background-only windows): "
              + (f"predicted noise {(prob_n.argmax(1) == noise_label).mean():.3f}, " if dc.noise else
                 "no noise class, ")
              + f"mean top confidence {prob_n.max(1).mean():.3f}")
        print("game events, by confidence threshold (rule: no event if top = noise or top p < threshold):")
        print("  threshold   false accept (no gesture -> event)   false reject (gesture -> no event)")
        for t in THRESHOLDS:
            print(f"  {t:9.2f}   {1 - E.reject_rates(prob_n, noise_label, t):34.3f}"
                  f"   {E.reject_rates(prob_b, noise_label, t):33.3f}")
    else:
        print(f"\n(no recordings in {dc.sessions_dir}: slide_bg and the no-gesture test skipped)")

    st = D.GestureStream(sp["test"], aug, a.channels, augment_on=False, seed=777)
    print("\noffset sweep (acc at a fixed shift from the centred position):")
    for s, acc in E.offset_sweep(model, st, [-40, -20, 0, 20, 40]).items():
        print(f"  shift {s:>+3d}: {acc:.3f}")
    print("\nvisible-length sweep (acc vs gesture steps in window; 0 = pure filler):")
    for n, acc in E.visible_sweep(model, st, [0, 10, 25, 50, 75, 100]).items():
        print(f"  visible {n:>3d}: {acc:.3f}")


if __name__ == "__main__":
    main()
