"""Experiment: the new 7-gesture vocabulary (new_gesture_100hz).

Reads like a spec — the AugConfig line IS the applied augmentation set, `channels`
picks 6-ch (raw, recommended) vs 10-ch (feature model), and build_model is the architecture.

    python -m train.exp_new_vocab                   # 6-ch lean, native, background filler
    python -m train.exp_new_vocab --filler gesture  # gesture-chunk filler instead
    python -m train.exp_new_vocab --feat            # 10-ch feature model
    python -m train.exp_new_vocab --smoke           # quick sanity run
    python -m train.exp_new_vocab --resample        # resample to GEST (the original new_lean)
    python -m train.exp_new_vocab --noise           # + 8th class noise (new_lean_native_bg_noise)
    python -m train.exp_new_vocab --noise --data new_gesture_100hz_v2 --tag new_lean_v2_noise
    python -m train.exp_new_vocab --noise --data shapes_yuwen --labels shapes \
        --sessions "../Hardware/Data Collection/sessions/ble" --tag shapes_yuwen_noise

--labels picks the classes (a vocabulary name: new | shapes, or a comma list); --sessions the
recordings folder(s) the background (filler, noise class, slide_bg) is read from.

Tags: new_lean + _native unless --resample + _bg with background filler, so runs never overwrite
each other (default: new_lean_native_bg; --resample --filler gesture reproduces new_lean).
"""
import argparse

from .config import (RunConfig, DataConfig, AugConfig, TrainConfig, NEW_ROLE, FILLERS,
                     SESSIONS_DIR, VOCABS, parse_labels)
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
    ap.add_argument("--labels", type=parse_labels, default=parse_labels("new"),
                    help=f"classes: a vocabulary {list(VOCABS)} or a comma list (default: new)")
    ap.add_argument("--sessions", nargs="+", default=[SESSIONS_DIR],
                    help="recordings for the background: folders, single recordings or wildcard patterns (default: SESSIONS_DIR)")
    ap.add_argument("--tag", help="run/model name (default: built from the options above)")
    a = ap.parse_args()

    aug = AugConfig.lean()                          # amp + time-warp + rotate + mag-warp
    aug.resample = a.resample
    aug.filler = a.filler
    cfg = RunConfig(
        data=DataConfig(dir=a.data, labels=a.labels, roles=NEW_ROLE, noise=a.noise,
                        sessions_dir=a.sessions),
        aug=aug,
        channels=10 if a.feat else 6,
        seeds=(2,),
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
