"""Save the exact test windows a run was scored on, plus the model's reference outputs, so a
converted model (hls4ml C-model, board) can be checked window by window against training.

    # from AI/ (paths are relative to where you run it)
    python -m gesture_pipeline.export_test_windows --data new_gesture_100hz_v2 --noise \
        --model gesture_pipeline/results/models/new_lean_v2_noise_s2.h5 \
        --out gesture_pipeline/results/test_windows_v2_noise.npz

The windows are rebuilt with the same seeds as train_eval, so they ARE the training test windows:
jitter (data.build_test), slide_bg (data.build_slide_test on held-out real background) and, with
--noise, the background-only noise windows (data.build_noise_test). The npz holds:
  X          (N, 150, 6) float32, already on the board's input grid (round(x*1024)/1024, int16
             range), so Keras, the hls4ml C-model and the board all see identical inputs
  y          (N,) true class index           set  (N,) "jitter" | "slide_bg" | "noise"
  logits_ref (N, classes) the .h5's outputs on X (with --model): the reference every conversion
             is compared to, logit by logit
Per-set accuracies are printed; they must equal the run's numbers in run_<tag>.json.
"""
import argparse
import os

import numpy as np

from .config import DataConfig, AugConfig, NEW_LABELS, NOISE
from . import data as D

SCALE = 1 << 10   # the board's to_fixed: int16 = round(x * 1024)


def on_board_grid(X):
    """What the board feeds the accelerator: round(x*1024) clipped to int16, back in units."""
    return (np.clip(np.round(X.astype(np.float64) * SCALE), -32768, 32767) / SCALE).astype(np.float32)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default="new_gesture_100hz_v2")
    ap.add_argument("--noise", action="store_true", help="model has the noise class (adds noise windows)")
    ap.add_argument("--resample", action="store_true", help="model was trained with resample=True")
    ap.add_argument("--channels", type=int, default=6)
    ap.add_argument("--model", help=".h5 to store reference outputs from and score")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    dc = DataConfig(dir=a.data, labels=NEW_LABELS, noise=a.noise)
    aug = AugConfig.lean(); aug.resample = a.resample
    sp = D.stratified_split(D.load(dc), dc)
    bg = D.split_background(D.load_background(dc), dc)
    Xj, yj, _, _ = D.build_test(sp["test"], aug, a.channels)
    Xb, yb, _ = D.build_slide_test(sp["test"], aug, a.channels, bg=bg["test"])
    sets = {"jitter": (Xj, yj), "slide_bg": (Xb, yb)}
    if a.noise:
        sets["noise"] = D.build_noise_test(sp["test"], aug, a.channels, bg["test"],
                                           int(np.bincount(yj).max()), dc.names().index(NOISE))
    X = np.concatenate([x for x, _ in sets.values()])
    y = np.concatenate([t for _, t in sets.values()])
    s = np.concatenate([[k] * len(t) for k, (_, t) in sets.items()])
    out = dict(X=on_board_grid(X), y=y, set=s, names=np.array(dc.names()))

    if a.model:
        from qkeras.utils import load_qmodel
        model = load_qmodel(a.model)
        ref_raw = model.predict(X, verbose=0)               # how train_eval scored it
        out["logits_ref"] = model.predict(out["X"], verbose=0)
        print(f"model {a.model}: outputs {out['logits_ref'].shape[1]}; "
              f"input grid changes logits by at most {np.abs(out['logits_ref'] - ref_raw).max():.4f}")
        for k in sets:
            m = s == k
            print(f"  {k:9s} {m.sum():5d} windows  acc (as trained) {(ref_raw[m].argmax(1) == y[m]).mean():.4f}"
                  f"  acc (board grid) {(out['logits_ref'][m].argmax(1) == y[m]).mean():.4f}")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    np.savez(a.out, **out)
    print(f"saved {len(y)} windows -> {a.out}")


if __name__ == "__main__":
    main()
