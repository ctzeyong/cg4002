"""Declarative configuration. Everything an experiment specifies lives here, so an
experiment file reads like a spec: compose a RunConfig, call train_eval, done."""
import math
import os
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))

# Where the datasets live — the ONE place the pipeline reads data paths from. Edit this line
# to point at your data, or set the GESTURE_DATA_ROOT env var (overrides without editing).
# Defaults to the package's own `dataset/` folder, so the package stays self-contained.
DATA_ROOT = os.environ.get("GESTURE_DATA_ROOT") or os.path.abspath(os.path.join(HERE, "dataset"))

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


@dataclass
class ModelConfig:
    """QCNN hyperparameters (see model.build_model)."""
    f1: int = 16
    f2: int = 32
    f3: int = 32
    f4: int = 0           # 0 = no 4th conv block
    ksize: int = 5
    dropout: float = 0.1
    pool: str = "max"     # max | avg | both


@dataclass
class AugConfig:
    """Every augmentation with on/off, probability, strength. Printed by each run so the
    applied set is visible at a glance. Factory methods give the named presets."""
    amp: bool = True;      amp_p: float = 0.9;  amp_lo: float = 0.85; amp_hi: float = 1.15
    timewarp: bool = True; tw_sigma: float = 0.20; tw_knots: int = 5
    jitter: bool = True;   jit_p: float = 0.9;  jit_frac: float = 0.05
    rotate: bool = True;   rot_p: float = 0.8;  rot_max_deg: float = 12.0
    magwarp: bool = True;  mw_p: float = 0.7;   mw_sigma: float = 0.12; mw_knots: int = 4
    bias: bool = True;     bias_p: float = 0.5; bias_frac: float = 0.05
    boundary: bool = True; bnd_p: float = 0.6;  bnd_max_frac: float = 0.10
    noise_frac: float = NOISE_FRAC

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

    def path(self):
        return self.dir if os.path.isabs(self.dir) else os.path.join(DATA_ROOT, self.dir)

    def label_map(self):
        return {name: i for i, name in enumerate(sorted(self.labels))}

    def names(self):
        return sorted(self.labels)


@dataclass
class TrainConfig:
    mult: int = 12          # augmented windows per gesture, per epoch
    epochs: int = 120
    patience: int = 15
    batch: int = 64
    lr: float = 1e-3


@dataclass
class RunConfig:
    """One training run, fully specified. This is the object an experiment composes."""
    data: DataConfig
    aug: AugConfig = field(default_factory=AugConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    train: TrainConfig = field(default_factory=TrainConfig)
    channels: int = 6                       # 6 raw, or 10 to add the feature channels
    seeds: tuple = (1, 2, 3)
    tag: str = "run"
    out_dir: str = os.path.join(HERE, "results")

    def describe(self):
        return (f"[{self.tag}] data={os.path.basename(self.data.path())} channels={self.channels} "
                f"aug={self.aug.enabled() or 'none'} seeds={list(self.seeds)} "
                f"mult={self.train.mult} epochs<={self.train.epochs}")
