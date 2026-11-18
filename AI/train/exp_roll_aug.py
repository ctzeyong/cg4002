"""Experiment: the noise-class run (exp_new_vocab --noise) plus a wrist-roll augmentation.

The wrist roll turns a training window about the sensor axis along the forearm, which the
recordings put at y. Gravity sits on z in the circle and star poses, moves to +x for chop and to
-x for tank, and y stays small. It applies to circle, star and the noise class only:
    - Circle and star still read as circle and star when the hand is rolled.
    - Tank and chop are told apart partly by the roll, so they are left out.
    - Noise rolls too, so an odd angle alone never means circle or star.
The whole window turns, gesture and the still part around it, as a rolled wrist would record
it. Every other augmentation is exactly exp_new_vocab's (AugConfig.lean(), background filler).

    python -m train.exp_roll_aug --data new_gestures_all_jabs \
        --sessions "C:/Users/USER/Desktop/cg4002/Hardware/Data Collection/sessions/" \
        --tag new_gestures_all_jabs_roll --seed 1
    python -m train.exp_roll_aug --smoke ...        # quick sanity run

--roll-p, --roll-deg, --roll-axis and --roll-classes change the roll; the noise class always
rolls with the chosen classes.
"""
import argparse
from dataclasses import dataclass, field

from .config import (RunConfig, DataConfig, AugConfig, TrainConfig, NEW_ROLE, NOISE, SESSIONS_DIR,
                     VOCABS, parse_labels)
from .train import train_eval

AXES = "xyz"


@dataclass
class RollAugConfig(AugConfig):
    """AugConfig + the wrist roll. data.GestureStream applies it to a training window whose
    label is in roll_labels (class indices), with probability roll_p, by a uniform angle in
    +-roll_max_deg about sensor axis roll_axis (0/1/2 = x/y/z)."""
    roll: bool = True
    roll_p: float = 0.5
    roll_max_deg: float = 90.0
    roll_axis: int = 1
    roll_labels: tuple = field(default_factory=tuple)

    SIGNAL = AugConfig.SIGNAL + ("roll",)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="new_gestures_all_jabs", help="dataset folder under DATA_ROOT")
    ap.add_argument("--labels", type=parse_labels, default=parse_labels("new"),
                    help=f"classes: a vocabulary {list(VOCABS)} or a comma list (default: new)")
    ap.add_argument("--sessions", nargs="+", default=[SESSIONS_DIR],
                    help="recordings for the background: folders, single recordings or wildcard patterns")
    ap.add_argument("--tag", default="new_gestures_all_jabs_roll", help="run/model name")
    ap.add_argument("--seed", type=int, nargs="+", default=[1], help="training seed(s) (default: 1)")
    ap.add_argument("--roll-p", type=float, default=RollAugConfig.roll_p,
                    help="share of those classes' training windows that are rolled")
    ap.add_argument("--roll-deg", type=float, default=RollAugConfig.roll_max_deg,
                    help="largest roll either way, in degrees")
    ap.add_argument("--roll-axis", choices=AXES, default=AXES[RollAugConfig.roll_axis],
                    help="sensor axis along the forearm")
    ap.add_argument("--roll-classes", default="circle,star",
                    help="gestures that roll (comma list); the noise class rolls too")
    ap.add_argument("--smoke", action="store_true", help="3 epochs, tiny mult")
    a = ap.parse_args()

    data = DataConfig(dir=a.data, labels=a.labels, roles=NEW_ROLE, noise=True, sessions_dir=a.sessions)
    lmap = data.label_map()
    rolled = [c.strip() for c in a.roll_classes.split(",") if c.strip()]
    unknown = [c for c in rolled if c not in lmap]
    if unknown:
        raise SystemExit(f"--roll-classes {unknown} not in {sorted(lmap)}")

    aug = RollAugConfig.lean()                      # amp + time-warp + rotate + mag-warp, as exp_new_vocab
    aug.filler = "background"
    aug.roll_p = a.roll_p
    aug.roll_max_deg = a.roll_deg
    aug.roll_axis = AXES.index(a.roll_axis)
    aug.roll_labels = tuple(lmap[c] for c in rolled + [NOISE])
    print(f"roll: p={aug.roll_p} +-{aug.roll_max_deg} deg about {a.roll_axis} for "
          f"{rolled + [NOISE]} (indices {list(aug.roll_labels)})")

    cfg = RunConfig(data=data, aug=aug, channels=6, seeds=tuple(a.seed), tag=a.tag)
    if a.smoke:
        cfg.train = TrainConfig(mult=4, epochs=3, patience=3)
        cfg.tag += "_smoke"

    print(train_eval(cfg).summary())


if __name__ == "__main__":
    main()
