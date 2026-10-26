"""Experiment: the new 7-gesture vocabulary (new_gesture_100hz).

Reads like a spec — the AugConfig line IS the applied augmentation set, `channels`
picks 6-ch (raw, recommended) vs 10-ch (feature model), and build_model is the architecture.

    python -m gesture_pipeline.exp_new_vocab            # 6-ch lean (recommended)
    python -m gesture_pipeline.exp_new_vocab --feat     # 10-ch feature model
    python -m gesture_pipeline.exp_new_vocab --smoke     # quick sanity run
    python -m gesture_pipeline.exp_new_vocab --resample  # resample to GEST (the original new_lean)

Native length is the default (tag new_lean_native); --resample reproduces new_lean.
"""
import argparse

from .config import RunConfig, DataConfig, AugConfig, TrainConfig, NEW_LABELS, NEW_ROLE
from .train import train_eval


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feat", action="store_true", help="10-channel feature model (default: 6-ch raw)")
    ap.add_argument("--smoke", action="store_true", help="1 seed, 3 epochs, tiny mult")
    ap.add_argument("--resample", action="store_true", help="resample every gesture to GEST steps")
    a = ap.parse_args()

    aug = AugConfig.lean()                          # amp + time-warp + rotate + mag-warp
    aug.resample = a.resample
    cfg = RunConfig(
        data=DataConfig(dir="new_gesture_100hz", labels=NEW_LABELS, roles=NEW_ROLE),
        aug=aug,
        channels=10 if a.feat else 6,
        seeds=(1, 2, 3),
        # a distinct tag per length mode, so a native run never overwrites a resampled .h5
        tag=("new_feat" if a.feat else "new_lean") + ("" if a.resample else "_native"),
    )
    if a.smoke:
        cfg.seeds = (1,)
        cfg.train = TrainConfig(mult=4, epochs=3, patience=3)
        cfg.tag += "_smoke"

    print(train_eval(cfg).summary())


if __name__ == "__main__":
    main()
