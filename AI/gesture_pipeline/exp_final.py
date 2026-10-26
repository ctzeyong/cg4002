"""Experiment: the final models on the OLD 7-gesture vocabulary (cloud_manual_day1_raw100hz_int16).

Reproduces the headline runs: lean (6-ch), none (window-aug only), feat (10-ch) — 3 seeds each.

    python -m gesture_pipeline.exp_final
"""
from .config import RunConfig, DataConfig, AugConfig, OLD_LABELS
from .train import train_eval

OLD = dict(dir="cloud_manual_day1_raw100hz_int16", labels=OLD_LABELS)


def main():
    for tag, aug, ch in [("lean", AugConfig.lean(), 6),
                         ("none3", AugConfig.none(), 6),
                         ("feat", AugConfig.lean(), 10)]:
        cfg = RunConfig(data=DataConfig(**OLD), aug=aug, channels=ch, seeds=(1, 2, 3), tag=tag)
        print(train_eval(cfg).summary(), "\n")


if __name__ == "__main__":
    main()
