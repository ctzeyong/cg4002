"""Experiment: the augmentation ablation on the OLD vocabulary.

  - none / all anchors
  - leave-one-out: remove each signal aug from the full set
  - a small strength sweep on the load-bearing augs
Screening budget (1 seed, mult 8, <=100 epochs), as in the original ablation.

    python -m gesture_pipeline.exp_ablation
"""
from .config import RunConfig, DataConfig, AugConfig, TrainConfig, OLD_LABELS
from .train import train_eval

OLD = dict(dir="cloud_manual_day1_raw100hz_int16", labels=OLD_LABELS)
SCREEN = TrainConfig(mult=8, epochs=100, patience=12)


def _run(tag, aug):
    aug.resample = True                         # the original ablation resampled to GEST
    cfg = RunConfig(data=DataConfig(**OLD), aug=aug, channels=6, seeds=(1,), train=SCREEN, tag=tag)
    print(train_eval(cfg).summary(), "\n")


def _variant(**overrides):
    a = AugConfig()
    for k, v in overrides.items():
        setattr(a, k, v)
    return a


def main():
    _run("none", AugConfig.none())
    _run("all", AugConfig())
    for name in AugConfig.SIGNAL:                        # leave-one-out
        _run(f"no_{name}", _variant(**{name: False}))
    for tag, ov in [("tw10", dict(tw_sigma=0.10)), ("tw35", dict(tw_sigma=0.35)),
                    ("mw06", dict(mw_sigma=0.06)), ("mw20", dict(mw_sigma=0.20)),
                    ("rot6", dict(rot_max_deg=6.0)), ("rot20", dict(rot_max_deg=20.0))]:
        _run(f"str_{tag}", _variant(**ov))


if __name__ == "__main__":
    main()
