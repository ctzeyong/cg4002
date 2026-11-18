# Training and evaluating the gesture model

One module per job, and experiment files that read like a spec. Run everything from `AI/` as
`python -m train.<module>`. Datasets come from [data_collection](../data_collection/README.md);
results go to `train/results/` (ignored by git).

| Module | Job |
|---|---|
| `config.py` | Every tunable as dataclasses (`RunConfig`, `ModelConfig`, `AugConfig`, `DataConfig`, `TrainConfig`), label maps, `DATA_ROOT` / `SESSIONS_DIR` |
| `model.py` | The architecture: `build_model(channels, cfg)` and its quantizers |
| `augment.py` | On-the-fly augmentation: signal augs, window synthesis, the 10-channel feature channels |
| `data.py` | Load a dataset, decode to SI, stratified split, windowed Keras sequences, the fixed test sets, the recorded background |
| `train.py` | The one `train_eval(RunConfig) -> Result` loop every experiment uses |
| `evaluate.py` | Accuracy, per-class recall, confusion matrix, softmax probabilities, the no-event rule |
| `exp_new_vocab.py` | The current experiment: the 7 game gestures, optionally + `noise` |
| `exp_roll_aug.py` | `exp_new_vocab --noise` plus a wrist-roll augmentation on circle, star and noise |
| `exp_ablation.py` / `exp_final.py` | The augmentation ablation and the final lean / none / feat models, on the old vocabulary |
| `exp_eval.py` | Evaluate a saved `.h5` |
| `export_test_windows.py` | Save the test windows + the `.h5`'s outputs for checking a converted model |

## An experiment reads like a spec

```python
cfg = RunConfig(
    data = DataConfig(dir="new_gestures_all", labels=NEW_LABELS, noise=True),
    aug  = AugConfig.lean(),     # amp + time-warp + rotate + mag-warp
    channels = 6,                # 6 raw, or 10 to add the feature channels
    seeds = (1, 2, 3),
)
print(train_eval(cfg).summary())
```
The architecture is `build_model`, the augmentations are the `AugConfig` line, the data is the
`DataConfig`, and the result is a `Result`. To add an experiment, make `exp_<name>.py`, compose a
`RunConfig` and call `train_eval`. A new knob goes into the right dataclass in `config.py`, not
into the experiment.

## Train

```bash
python -m train.exp_new_vocab --noise --data new_gestures_all --sessions "../Hardware/Data Collection/sessions" --tag new_gestures_all
```

It trains 3 seeds, prints the summary, and saves `results/models/<tag>_s1.h5` … `_s3.h5`,
`results/run_<tag>.json` and a row in `results/results.csv`.

| Flag | Meaning |
|---|---|
| `--data NAME` | Dataset folder under `DATA_ROOT` (default `new_gesture_100hz`) |
| `--labels SPEC` | Classes: `new` (chop, circle, fireball, jabs, lightning, star, tank; default), `shapes` (arc, caret, circle, infinity, star, triangle, z), or a comma list. Class index = sorted name order, with `noise` last |
| `--noise` | Add the 8th class `noise`, taken from the unlabelled time in the recordings. Needs `--filler background` (the default) |
| `--sessions A [B ...]` | Recordings the **background** is read from: the window filler, the noise class and the `slide_bg` / false-accept tests. Same entry types as the export. It can differ from the export's: `sessions/` with subfolders also adds the noise-only recordings |
| `--tag NAME` | Run and model name (default built from the flags, e.g. `new_lean_native_bg_noise`). Reusing a tag overwrites that run |
| `--filler {background,gesture}` | What surrounds the gesture in a training window: real background (default) or chunks of other gestures |
| `--resample` | Stretch every gesture to 100 steps (the old behaviour). Default: native length with a slide |
| `--feat` | 10-channel feature model instead of 6-channel raw |
| `--smoke` | Quick check: 1 seed, 3 epochs, tag gets `_smoke` |

`exp_roll_aug` takes the same `--data` / `--labels` / `--sessions` / `--tag`, plus `--seed`,
`--roll-p`, `--roll-deg`, `--roll-axis` and `--roll-classes`.

Runs in the venv from [`../requirements.txt`](../requirements.txt) (see [Setup](../README.md#setup)); `__init__.py` sets the legacy-Keras env var.

## Evaluate a saved model

```bash
python -m train.exp_eval --model train/results/models/new_gestures_all_s1.h5 --data new_gestures_all --sessions "../Hardware/Data Collection/sessions"
```

Pass the same `--data` / `--labels` / `--sessions` the model was trained with, and `--resample` for a
model trained with `resample=True`. An 8-class model is detected from its output size. It prints
per-class recall, the confusion matrix, slide and slide_bg accuracy per position, the visible-length
leakage probe, and the **threshold table**: false accept vs false reject at confidence
0 / 0.5 / 0.7 / 0.9 under the comms rule (no event if the top class is `noise` or its probability is
below the threshold). Pick the row that suits the game and set `CONF_THRESHOLD` in
[`ultra96/wk10_integration.py`](../ultra96/wk10_integration.py) (currently 0.7).

## Export the test windows for conversion

```bash
python -m train.export_test_windows --data <dataset> --noise \
    --model train/results/models/<tag>_s<seed>.h5 --out train/results/test_windows_<tag>.npz
```

Pass the same `--data` / `--labels` / `--sessions` the model was trained with. It rebuilds the run's test windows with the same seeds (jitter, slide_bg and, with `--noise`, the
background-only windows), already on the board's int16/1024 input grid, and stores the `.h5`'s logits
on them. The per-set accuracies it prints must equal the run's `run_<tag>.json`. The `.npz` is the
input to [hls4ml/convert.py](../hls4ml/README.md).

## Gesture length: native vs resample

`AugConfig.resample` (default `False`) must match how the live pipeline feeds the model:

| `resample` | Training | Live pipeline | Tag suffix |
|---|---|---|---|
| `False` (native) | The gesture keeps its 100 Hz length (time-warp varies speed ×0.9–1.1) and slides anywhere across the 150-step window, up to `slide_max_out` (0.3) cut off either edge | The ring buffer's last 150 samples at end-of-gesture | `_native` |
| `True` | Every gesture resampled to 100 steps, random offset −40…90 | Not used live any more | none |

`exp_final` / `exp_ablation` pin `resample=True` and `filler="gesture"`, so they reproduce the
old-vocabulary results.

## Window filler and the `noise` class

`AugConfig.filler` picks what surrounds the gesture in a training window:

- `"background"` (default, tag `_bg`): one contiguous 150-step slice of **real background**, the
  unlabelled time of the reviewed recordings, kept 200 ms (`bg_margin_us`) clear of every labelled
  interval and split at sensor gaps. Labelled `noise` intervals count as labelled: they are random
  gestures, so they are never background. Unreviewed recordings are skipped.
- `"gesture"` (the original): 5–24-step chunks of other gestures + `noise_frac` noise.

`DataConfig(noise=True)` (`--noise`) adds a no-gesture class:

- **Index 7, always last**, so gesture indices 0–6 never move and the comms label map only gains
  `7: noise`.
- **A noise window** is a fresh 150-step slice of real background, augmented like a gesture.
- **How many:** `noise_ratio` (1.0) × the largest gesture class per split.
- **Needs `filler="background"`** (enforced): with gesture-chunk filler, "gesture chunks present"
  would separate the classes, which never holds live.

The background is chopped into 6 s blocks and split 70/15/15 with `split_seed`, so test filler is
never trained on.

## Test sets and metrics

Every test window is built once (fixed seeds) from the **test split only**, never augmented, and
scored by every seed. All numbers are fractions of windows.

| Metric | Windows | Correct when | Answers |
|---|---|---|---|
| **jitter** | each test gesture ×4, start offset −40…40, gesture-chunk filler | predicted = true gesture | The original headline number; compares any two models |
| **ceiling** | each test gesture once, centred | predicted = true gesture | Upper bound |
| **slide** | each test gesture at 5 positions, from 30% cut off the left edge to 30% off the right | predicted = true gesture | Does it still recognise a cut-off gesture? |
| **slide_bg** | slide, with held-out **real background** as filler | predicted = true gesture | The most live-like accuracy |
| **false reject** | the slide_bg gesture windows | predicted `noise` | How often a real gesture is swallowed (no game event) |
| **false accept** | held-out background-only windows | predicted a gesture | How often idle motion fires a game event |
| **per-class recall** | jitter windows (+ the noise windows for `noise`) | predicted = that class | Which gesture is weakest |

A gesture window ends one of three ways: right gesture (slide_bg), wrong gesture, or `noise` (false
reject), so slide_bg + wrong + false reject = 1. A 7-class model has no noise output, so its false
accept is always 1.0 before any threshold, which is why the noise class exists.

`train_eval` prints all of them per seed; slide, slide_bg, false reject/accept and per-class are in
`run_<tag>.json`, and `results.csv` keeps jitter / ceiling. Every run so far is in
[docs/results.md](../../docs/results.md).
