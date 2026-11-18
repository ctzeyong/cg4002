"""The one training loop. train_eval(RunConfig) trains the model for each seed on the
augmented stream, scores every seed on a single fixed test set, saves models + metrics,
and returns a Result. Every experiment goes through here."""
import csv
import json
import os
from dataclasses import dataclass

import numpy as np

from .config import RunConfig, NOISE
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
    slide_bg: list         # same on real-background filler; empty without recordings
    false_reject: list     # noise class only: slide_bg gesture windows predicted noise
    false_accept: list     # noise class only: background-only test windows predicted a gesture
    params: int
    per_class: dict        # seed-averaged recall (gestures on jitter windows; noise on the noise test)
    confusion: object      # row-normalised matrix
    worst_pairs: list

    def summary(self):
        j, c = float(np.mean(self.jitter)), float(np.mean(self.ceiling))
        pc = ", ".join(f"{k} {v:.3f}" for k, v in self.per_class.items() if v is not None)
        worst = ", ".join(f"{a}->{b} {r:.1%}" for r, a, b in self.worst_pairs[:4])
        s = float(np.mean(self.slide))
        sb = f"  slide_bg {float(np.mean(self.slide_bg)):.4f}" if self.slide_bg else ""
        nz = (f"\n  noise: false reject {float(np.mean(self.false_reject)):.4f} (gesture -> noise)  "
              f"false accept {float(np.mean(self.false_accept)):.4f} (noise -> gesture)"
              if self.false_accept else "")
        return (f"[{self.tag}] jitter {j:.4f} (seeds {[round(x, 4) for x in self.jitter]})  "
                f"ceiling {c:.4f}  slide {s:.4f}{sb}  params {self.params}{nz}\n"
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
    runs = D.load_background(cfg.data)
    bg = D.split_background(runs, cfg.data) if runs else None
    if bg is None and (cfg.aug.filler == "background" or cfg.data.noise):
        raise FileNotFoundError(f"filler='background' / the noise class need recordings in {cfg.data.sessions_dir}")
    if cfg.data.noise and cfg.aug.filler != "background":
        # noise windows are pure background; with gesture-chunk filler around every gesture the
        # model could learn "gesture chunks in the window -> gesture", which never holds live
        raise ValueError("the noise class needs AugConfig.filler='background'")
    # slide_bg: the slide test on held-out real background, for every model (the most live-like)
    Xb, yb, _ = D.build_slide_test(sp["test"], cfg.aug, cfg.channels, bg=bg["test"]) if bg else (None, None, None)
    noise_label = names.index(NOISE) if cfg.data.noise else None
    if cfg.data.noise:   # background-only test windows, ~ as many as one class has jitter windows
        n_test = int(np.bincount(yj).max())
        Xn, yn = D.build_noise_test(sp["test"], cfg.aug, cfg.channels, bg["test"], n_test, noise_label)
    print(cfg.describe(), flush=True)
    print(f"split train/val/test = {len(sp['train'])}/{len(sp['val'])}/{len(sp['test'])}; "
          f"test windows {len(Xj)} jitter / {len(Xc)} ceiling / {len(Xs)} slide"
          + (f" / {len(Xb)} slide_bg; background blocks "
             f"{len(bg['train'])}/{len(bg['val'])}/{len(bg['test'])}" if bg else " (no recordings: no slide_bg)")
          + (f"; noise: {D.noise_count(sp['train'], cfg.data)} train / {D.noise_count(sp['val'], cfg.data)} val "
             f"slots, {len(Xn)} test windows" if cfg.data.noise else ""),
          flush=True)

    fill = cfg.aug.filler == "background"
    jit, ceil, slide, slide_bg, f_rej, f_acc, preds_j, preds_n, params = [], [], [], [], [], [], [], [], 0
    for seed in cfg.seeds:
        np.random.seed(seed); tf.random.set_seed(seed); tf.keras.backend.clear_session()
        tr = D.make_sequence(sp["train"], cfg.aug, cfg.channels, True,  cfg.train.batch, cfg.train.mult, seed,
                             bg["train"] if fill else None, D.noise_count(sp["train"], cfg.data), noise_label)
        va = D.make_sequence(sp["val"],   cfg.aug, cfg.channels, False, cfg.train.batch, 2, seed,
                             bg["val"] if fill else None, D.noise_count(sp["val"], cfg.data), noise_label)
        model = build_model(cfg.channels, cfg.model, n_classes=len(names))
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
        if bg:
            pb = E.predict(model, Xb)
            slide_bg.append(float((pb == yb).mean()))
        if cfg.data.noise:
            f_rej.append(float((pb == noise_label).mean()))
            pn = E.predict(model, Xn)
            f_acc.append(float((pn != noise_label).mean())); preds_n.append(pn)
        model.save(os.path.join(models, f"{cfg.tag}_s{seed}.h5"))
        print(f"  seed {seed}: jitter {jit[-1]:.4f} ceiling {ceil[-1]:.4f} slide {slide[-1]:.4f}"
              + (f" slide_bg {slide_bg[-1]:.4f}" if bg else "")
              + (f" false_reject {f_rej[-1]:.4f} false_accept {f_acc[-1]:.4f}" if cfg.data.noise else ""),
              flush=True)

    # per-class / confusion over the jitter windows, plus the noise test windows with the noise class
    P, Y = np.stack(preds_j), yj
    if cfg.data.noise:
        P, Y = np.concatenate([P, np.stack(preds_n)], axis=1), np.concatenate([yj, yn])
    pc = E.per_class(P, Y, names)
    row, pairs = E.confusion(P, Y, names)
    result = Result(cfg.tag, names, list(cfg.seeds), jit, ceil, slide, slide_bg, f_rej, f_acc,
                    int(params), pc, row, pairs)

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
                   slide_max_out=cfg.aug.slide_max_out, filler=cfg.aug.filler, noise=cfg.data.noise,
                   jitter=jit, ceiling=ceil, slide=slide, slide_bg=slide_bg,
                   false_reject=f_rej, false_accept=f_acc, per_class=pc, names=names),
              open(os.path.join(cfg.out_dir, f"run_{cfg.tag}.json"), "w"), indent=2)
    return result
