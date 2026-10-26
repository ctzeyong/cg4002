"""Evaluate a saved .h5: per-class recall, confusion matrix, the offset-robustness sweep,
and the visible-length leakage probe. Replaces the old eval_perclass / confusion one-offs
and carries over gesture_real_baseline_v3's eval_offsets / eval_visible_lengths.

    python -m gesture_pipeline.exp_eval --model results/models/new_lean_s1.h5 --resample
    python -m gesture_pipeline.exp_eval --model results/models/new_lean_native_s1.h5
    python -m gesture_pipeline.exp_eval --model results/models/feat_s1.h5 --channels 10 --resample

Pass --resample for a model trained with AugConfig.resample=True (every model trained before
the switch existed), so the test windows match how it was trained.
"""
import argparse

from .config import DataConfig, AugConfig, NEW_LABELS, OLD_LABELS
from . import data as D
from . import evaluate as E


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--data", default="new_gesture_100hz")
    ap.add_argument("--channels", type=int, default=6)
    ap.add_argument("--old", action="store_true", help="old 7-class vocab (arc..z)")
    ap.add_argument("--resample", action="store_true", help="model was trained with resample=True")
    a = ap.parse_args()
    from qkeras.utils import load_qmodel

    dc = DataConfig(dir=a.data, labels=(OLD_LABELS if a.old else NEW_LABELS))
    names = dc.names()
    sp = D.stratified_split(D.load(dc), dc)
    model = load_qmodel(a.model)
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

    st = D.GestureStream(sp["test"], aug, a.channels, augment_on=False, seed=777)
    print("\noffset sweep (acc at a fixed shift from the centred position):")
    for s, acc in E.offset_sweep(model, st, [-40, -20, 0, 20, 40]).items():
        print(f"  shift {s:>+3d}: {acc:.3f}")
    print("\nvisible-length sweep (acc vs gesture steps in window; 0 = pure filler):")
    for n, acc in E.visible_sweep(model, st, [0, 10, 25, 50, 75, 100]).items():
        print(f"  visible {n:>3d}: {acc:.3f}")


if __name__ == "__main__":
    main()
