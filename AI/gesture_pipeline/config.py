"""Declarative configuration. Everything an experiment specifies lives here, so an
experiment file reads like a spec: compose a RunConfig, call train_eval, done."""
import math
import os
from dataclasses import dataclass, field
from typing import Literal, get_args

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- the allowed values of every option-type field (the Literal IS the list of choices) ----
Filler = Literal["gesture", "background"]     # AugConfig.filler
Pool = Literal["max", "avg", "both"]          # ModelConfig.pool
Channels = Literal[6, 10]                     # RunConfig.channels
FILLERS, POOLS, CHANNELS = get_args(Filler), get_args(Pool), get_args(Channels)


class _Options:
    """Rejects a value outside a field's allowed choices (_OPTIONS: field -> tuple), both on
    construction and on any later assignment (`aug.filler = ...`), so a typo fails at once
    and the error lists the valid choices."""
    _OPTIONS = {}

    def __setattr__(self, name, value):
        choices = self._OPTIONS.get(name)
        if choices is not None and value not in choices:
            raise ValueError(f"{type(self).__name__}.{name}={value!r}; choose one of {choices}")
        super().__setattr__(name, value)

# Where the datasets live — the ONE place the pipeline reads data paths from. Edit this line
# to point at your data, or set the GESTURE_DATA_ROOT env var (overrides without editing).
# Defaults to the package's own `dataset/` folder, so the package stays self-contained.
DATA_ROOT = os.environ.get("GESTURE_DATA_ROOT") or os.path.abspath(os.path.join(HERE, "dataset"))

# The continuous 500 Hz recordings (record.py output: <name>.csv + <name>.labels.json). Their
# unlabelled time is real background, used as window filler (AugConfig.filler="background") and
# for the slide_bg test. Defaults to the repo's data-collection folder; GESTURE_SESSIONS_DIR overrides.
SESSIONS_DIR = os.environ.get("GESTURE_SESSIONS_DIR") or os.path.abspath(
    os.path.join(HERE, "..", "..", "Hardware", "Data Collection", "sessions"))

# ---- fixed data/board contract (matches prepare_real_dataset.py and keras_stream.py) ----
WIN, GEST, N_CLASSES = 150, 100, 7
RAW_PERIOD_US = 2000                       # 500 Hz raw grid
ACC_SCALE = 9.80665 / 8192.0               # int16 counts -> m/s^2
GYR_SCALE = (math.pi / 180.0) / 65.5       # int16 counts -> rad/s
CROP = 40                                  # window offset range (+/- around centred)
NOISE_FRAC = 0.05                          # filler noise, fraction of per-channel std

# ---- gesture vocabularies ----
OLD_LABELS = ["arc", "caret", "circle", "infinity", "star", "triangle", "z"]
NEW_LABELS = ["chop", "circle", "fireball", "jabs", "lightning", "star", "tank"]
NEW_ROLE = {"chop": "warrior", "jabs": "rogue", "tank": "shield", "circle": "mage",
            "star": "hero", "fireball": "fireball", "lightning": "lightning"}
# named vocabularies for the experiments' --labels flag ("shapes" = the game's GestureSymbol names)
VOCABS = {"new": NEW_LABELS, "shapes": OLD_LABELS, "old": OLD_LABELS}


def parse_labels(spec):
    """--labels value -> class list: a VOCABS name, or a comma-separated list
    (e.g. "shapes" or "arc,caret,circle,infinity,lightning,star,triangle,z")."""
    if spec in VOCABS:
        return list(VOCABS[spec])
    labels = [s.strip() for s in spec.split(",") if s.strip()]
    if len(labels) < 2 or len(set(labels)) != len(labels):
        raise ValueError(f"--labels {spec!r}: name a vocabulary {list(VOCABS)} or give 2+ distinct labels")
    return labels


# the no-gesture class (DataConfig.noise): windows of unlabelled recorded background. Always the
# LAST class index (7 for 7 gestures), so the gesture indices 0..6 never move.
NOISE = "noise"
NOISE_SPAN = (80, 250)    # resample mode: background span length (steps) treated like a gesture


@dataclass
class ModelConfig(_Options):
    """QCNN hyperparameters (see model.build_model)."""
    _OPTIONS = {"pool": POOLS}
    f1: int = 16
    f2: int = 32
    f3: int = 32
    f4: int = 0           # 0 = no 4th conv block
    ksize: int = 5
    dropout: float = 0.1
    pool: Pool = "max"    # global pooling: one of POOLS


@dataclass
class AugConfig(_Options):
    """Every augmentation with on/off, probability, strength. Printed by each run so the
    applied set is visible at a glance. Factory methods give the named presets."""
    _OPTIONS = {"filler": FILLERS}
    amp: bool = True;      amp_p: float = 0.9;  amp_lo: float = 0.85; amp_hi: float = 1.15
    timewarp: bool = True; tw_sigma: float = 0.20; tw_knots: int = 5
    jitter: bool = True;   jit_p: float = 0.9;  jit_frac: float = 0.05
    rotate: bool = True;   rot_p: float = 0.8;  rot_max_deg: float = 12.0
    magwarp: bool = True;  mw_p: float = 0.7;   mw_sigma: float = 0.12; mw_knots: int = 4
    bias: bool = True;     bias_p: float = 0.5; bias_frac: float = 0.05
    boundary: bool = True; bnd_p: float = 0.6;  bnd_max_frac: float = 0.10
    noise_frac: float = NOISE_FRAC
    # resample=True: every gesture -> GEST steps, placed at a random offset in -CROP..WIN-GEST+CROP
    # (the original pipeline; pair with the live --resample path). False: native 100 Hz length,
    # slid anywhere across the window with up to slide_max_out of it cut off either edge;
    # time-warp then also varies overall speed.
    resample: bool = False
    speed_lo: float = 0.9; speed_hi: float = 1.1   # native mode only, with timewarp
    # most of the gesture (fraction 0..1) allowed outside the window: native training placement,
    # and the sliding test set for every model
    slide_max_out: float = 0.3
    # what surrounds the gesture in the window, one of FILLERS: "gesture" = 5-24-step chunks of
    # other gestures (the original); "background" = one contiguous WIN-step slice of real recorded
    # background. noise_frac Gaussian noise is added on top of either (0 = pure recorded background).
    filler: Filler = "background"

    SIGNAL = ("amp", "timewarp", "jitter", "rotate", "magwarp", "bias", "boundary")

    @classmethod
    def none(cls):
        """Window placement/filler/noise only; no signal augmentation."""
        c = cls()
        for n in cls.SIGNAL:
            setattr(c, n, False)
        return c

    @classmethod
    def lean(cls):
        """The ablation winner: amp + time-warp + rotate + mag-warp (jitter/bias/boundary off)."""
        c = cls()
        for n in ("jitter", "bias", "boundary"):
            setattr(c, n, False)
        return c

    def enabled(self):
        return [n for n in self.SIGNAL if getattr(self, n)]


@dataclass
class DataConfig:
    """Which dataset + which labels. label_map is derived (sorted), never monkeypatched."""
    dir: str                                        # abs, or relative to config.DATA_ROOT
    labels: list = field(default_factory=lambda: list(NEW_LABELS))
    roles: dict = field(default_factory=lambda: dict(NEW_ROLE))
    split_seed: int = 42
    ratios: tuple = (0.70, 0.15, 0.15)
    sessions_dir: object = SESSIONS_DIR             # recordings folder(s) for background: str or list
    bg_margin_us: int = 200_000                     # background keeps this clear of any label
    bg_block: int = 600                             # background split unit, in 100 Hz steps (6 s)
    # 8th class `noise` (last index): windows of unlabelled background, drawn fresh from that
    # split's background blocks. Needs AugConfig.filler="background" (see train_eval).
    noise: bool = False
    noise_ratio: float = 1.0                        # noise windows = ratio x the largest gesture class

    def path(self):
        return self.dir if os.path.isabs(self.dir) else os.path.join(DATA_ROOT, self.dir)

    def label_map(self):
        return {name: i for i, name in enumerate(self.names())}

    def names(self):
        """Class names in index order: the sorted gestures, then NOISE last when enabled."""
        return sorted(self.labels) + ([NOISE] if self.noise else [])


@dataclass
class TrainConfig:
    mult: int = 12          # augmented windows per gesture, per epoch
    epochs: int = 120
    patience: int = 15
    batch: int = 64
    lr: float = 1e-3


@dataclass
class RunConfig(_Options):
    """One training run, fully specified. This is the object an experiment composes."""
    _OPTIONS = {"channels": CHANNELS}
    data: DataConfig
    aug: AugConfig = field(default_factory=AugConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    train: TrainConfig = field(default_factory=TrainConfig)
    channels: Channels = 6                  # 6 raw, or 10 to add the feature channels
    seeds: tuple = (1, 2, 3)
    tag: str = "run"
    out_dir: str = os.path.join(HERE, "results")

    def describe(self):
        return (f"[{self.tag}] data={os.path.basename(self.data.path())} channels={self.channels} "
                f"aug={self.aug.enabled() or 'none'} "
                f"length={'resample' if self.aug.resample else 'native'} "
                f"filler={self.aug.filler} classes={len(self.data.names())}"
                f"{' (+noise)' if self.data.noise else ''} seeds={list(self.seeds)} "
                f"mult={self.train.mult} epochs<={self.train.epochs}")
