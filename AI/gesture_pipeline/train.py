"""The one training loop. train_eval(RunConfig) trains the model for each seed on the
augmented stream, scores every seed on a single fixed test set, saves models + metrics,
and returns a Result. Every experiment goes through here."""
import csv
import json
import os
from dataclasses import dataclass

import numpy as np

from .config import RunConfig
from . import data as D
from . import evaluate as E


@dataclass
class Result:
    tag: str
    names: list
    seeds: list
    jitter: list          # per-seed jitter accuracy
    ceiling: list          # per-seed ceiling accuracy
    slide: list            # per-seed sliding-test accuracy (data.build_slide_test)
    params: int
    per_class: dict        # seed-averaged recall
    confusion: object      # row-normalised matrix
    worst_pairs: list

    def summary(self):
        j, c = float(np.mean(self.jitter)), float(np.mean(self.ceiling))
        pc = ", ".join(f"{k} {v:.3f}" for k, v in self.per_class.items() if v is not None)
        worst = ", ".join(f"{a}->{b} {r:.1%}" for r, a, b in self.worst_pairs[:4])
        s = float(np.mean(self.slide))
        return (f"[{self.tag}] jitter {j:.4f} (seeds {[round(x, 4) for x in self.jitter]})  "
                f"ceiling {c:.4f}  slide {s:.4f}  params {self.params}\n"
                f"  per-class: {pc}\n  worst: {worst or 'none'}")


def train_eval(cfg: RunConfig) -> Result:
    import tensorflow as tf
    from .model import build_model

    os.makedirs(cfg.out_dir, exist_ok=True)
    models = os.path.join(cfg.out_dir, "models"); os.makedirs(models, exist_ok=True)
    names = cfg.data.names()

    sp = D.stratified_split(D.load(cfg.data), cfg.data)
    Xj, yj, Xc, yc = D.build_test(sp["test"], cfg.aug, cfg.channels)
    Xs, ys, _ = D.build_slide_test(sp["test"], cfg.aug, cfg.channels)
    print(cfg.describe(), flush=True)
    print(f"split train/val/test = {len(sp['train'])}/{len(sp['val'])}/{len(sp['test'])}; "
          f"test windows {len(Xj)} jitter / {len(Xc)} ceiling / {len(Xs)} slide", flush=True)

    jit, ceil, slide, preds_j, params = [], [], [], [], 0
    for seed in cfg.seeds:
        np.random.seed(seed); tf.random.set_seed(seed); tf.keras.backend.clear_session()
        tr = D.make_sequence(sp["train"], cfg.aug, cfg.channels, True,  cfg.train.batch, cfg.train.mult, seed)
        va = D.make_sequence(sp["val"],   cfg.aug, cfg.channels, False, cfg.train.batch, 2, seed)
        model = build_model(cfg.channels, cfg.model)
        model.compile(optimizer=tf.keras.optimizers.Adam(cfg.train.lr),
                      loss=tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True), metrics=["accuracy"])
        params = model.count_params()
        print(f"-- {cfg.tag} seed {seed}: training (per-epoch train/val accuracy below) --", flush=True)
        model.fit(tr, validation_data=va, epochs=cfg.train.epochs, verbose=2,
                  callbacks=[tf.keras.callbacks.EarlyStopping(
                      monitor="val_loss", patience=cfg.train.patience, restore_best_weights=True)])
        pj = E.predict(model, Xj)
        jit.append(float((pj == yj).mean())); ceil.append(E.accuracy(model, Xc, yc)); preds_j.append(pj)
        slide.append(E.accuracy(model, Xs, ys))
        model.save(os.path.join(models, f"{cfg.tag}_s{seed}.h5"))
        print(f"  seed {seed}: jitter {jit[-1]:.4f} ceiling {ceil[-1]:.4f} slide {slide[-1]:.4f}", flush=True)

    pc = E.per_class(np.stack(preds_j), yj, names)
    row, pairs = E.confusion(np.stack(preds_j), yj, names)
    result = Result(cfg.tag, names, list(cfg.seeds), jit, ceil, slide, int(params), pc, row, pairs)

    rc = os.path.join(cfg.out_dir, "results.csv")
    new = not os.path.exists(rc)
    with open(rc, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["tag", "channels", "aug", "jitter_mean", "ceiling_mean", "params", "seeds"])
        w.writerow([cfg.tag, cfg.channels, "+".join(cfg.aug.enabled()),
                    round(float(np.mean(jit)), 4), round(float(np.mean(ceil)), 4), params, list(cfg.seeds)])
    json.dump(dict(tag=cfg.tag, channels=cfg.channels, enabled=cfg.aug.enabled(),
                   resample=cfg.aug.resample,
                   slide_max_out=cfg.aug.slide_max_out,
                   jitter=jit, ceiling=ceil, slide=slide, per_class=pc, names=names),
              open(os.path.join(cfg.out_dir, f"run_{cfg.tag}.json"), "w"), indent=2)
    return result
