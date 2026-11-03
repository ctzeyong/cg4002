"""Experiment: the new 7-gesture vocabulary (new_gesture_100hz).

Reads like a spec — the AugConfig line IS the applied augmentation set, `channels`
picks 6-ch (raw, recommended) vs 10-ch (feature model), and build_model is the architecture.

    python -m gesture_pipeline.exp_new_vocab                   # 6-ch lean, native, background filler
    python -m gesture_pipeline.exp_new_vocab --filler gesture  # gesture-chunk filler instead
    python -m gesture_pipeline.exp_new_vocab --feat            # 10-ch feature model
    python -m gesture_pipeline.exp_new_vocab --smoke           # quick sanity run
    python -m gesture_pipeline.exp_new_vocab --resample        # resample to GEST (the original new_lean)
    python -m gesture_pipeline.exp_new_vocab --noise           # + 8th class noise (new_lean_native_bg_noise)
    python -m gesture_pipeline.exp_new_vocab --noise --data new_gesture_100hz_v2 --tag new_lean_v2_noise

Tags: new_lean + _native unless --resample + _bg with background filler, so runs never overwrite
each other (default: new_lean_native_bg; --resample --filler gesture reproduces new_lean).
"""
import argparse

from .config import RunConfig, DataConfig, AugConfig, TrainConfig, NEW_LABELS, NEW_ROLE, FILLERS
from .train import train_eval


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feat", action="store_true", help="10-channel feature model (default: 6-ch raw)")
    ap.add_argument("--smoke", action="store_true", help="1 seed, 3 epochs, tiny mult")
    ap.add_argument("--resample", action="store_true", help="resample every gesture to GEST steps")
    ap.add_argument("--filler", choices=FILLERS, default=AugConfig.filler,
                    help=f"what surrounds the gesture in the window (default: {AugConfig.filler})")
    ap.add_argument("--noise", action="store_true",
                    help="8th class 'noise' (index 7) from unlabelled background; needs --filler background")
    ap.add_argument("--data", default="new_gesture_100hz",
                    help="dataset folder under DATA_ROOT (e.g. one made by export_dataset)")
    ap.add_argument("--tag", help="run/model name (default: built from the options above)")
    a = ap.parse_args()

    aug = AugConfig.lean()                          # amp + time-warp + rotate + mag-warp
    aug.resample = a.resample
    aug.filler = a.filler
    cfg = RunConfig(
        data=DataConfig(dir=a.data, labels=NEW_LABELS, roles=NEW_ROLE, noise=a.noise),
        aug=aug,
        channels=10 if a.feat else 6,
        seeds=(1, 2, 3),
        tag=("new_feat" if a.feat else "new_lean") + ("" if a.resample else "_native")
            + ("_bg" if a.filler == "background" else "") + ("_noise" if a.noise else ""),
    )
    if a.tag:
        cfg.tag = a.tag
    if a.smoke:
        cfg.seeds = (1,)
        cfg.train = TrainConfig(mult=4, epochs=3, patience=3)
        cfg.tag += "_smoke"

    print(train_eval(cfg).summary())


if __name__ == "__main__":
    main()
