# gesture_pipeline

A standalone, self-contained pipeline for the gesture QCNN. One folder, one job per module,
and an experiment file that reads like a spec. Nothing here imports `gesture_real_baseline_v3.py`.

## Layout

| module | responsibility |
|---|---|
| `config.py` | all tunables as dataclasses (`RunConfig`, `ModelConfig`, `AugConfig`, `DataConfig`, `TrainConfig`); label maps; constants |
| `model.py` | **the architecture** — `build_model(channels, cfg)` + quantizers, owned here (a standalone copy of the baseline QCNN) |
| `augment.py` | on-the-fly augmentation: signal augs + window synthesis + the 10-ch feature channels |
| `data.py` | load per-gesture CSVs, decode to SI, stratified split, windowed Keras sequences, fixed test set |
| `train.py` | the one `train_eval(RunConfig) -> Result` loop (used by every experiment) |
| `evaluate.py` | accuracy, per-class recall, confusion matrix, softmax probabilities, the no-event rule |
| `export_dataset.py` | regenerate a dataset folder from the continuous recordings (see *Datasets*) |
| `exp_*.py` | thin experiments: compose a `RunConfig`, call `train_eval` |
| `results/` | `results.csv`, `run_*.json`, `models/`, `logs/` |

## An experiment reads like a spec

```python
cfg = RunConfig(
    data = DataConfig(dir="new_gesture_100hz", labels=NEW_LABELS),
    aug  = AugConfig.lean(),     # amp + time-warp + rotate + mag-warp  <- augmentations at a glance
    channels = 6,                # 6 raw, or 10 to add the feature channels
    seeds = (1, 2, 3),
)
print(train_eval(cfg).summary())
```
The architecture is `build_model` (in `model.py`); the augmentations are the `AugConfig` line;
the data is the `DataConfig`; the result is a `Result`. That is the whole surface.

## Run (from `AI/`, the folder containing this package)

```bash
python -m gesture_pipeline.export_dataset --out new_gesture_100hz_v2   # regenerate the data (see Datasets)
python -m gesture_pipeline.exp_new_vocab --noise --data new_gesture_100hz_v2 --tag new_lean_v2_noise   # current model
python -m gesture_pipeline.exp_new_vocab              # new vocab, 6-ch lean, native, background filler
python -m gesture_pipeline.exp_new_vocab --filler gesture   # same, gesture-chunk filler
python -m gesture_pipeline.exp_new_vocab --resample --filler gesture   # = the original new_lean
python -m gesture_pipeline.exp_new_vocab --feat       # new vocab, 10-ch feature model
python -m gesture_pipeline.exp_final                  # old vocab: lean / none / feat (3 seeds)
python -m gesture_pipeline.exp_ablation               # old vocab: leave-one-out + strength sweep
python -m gesture_pipeline.exp_eval --model results/models/new_lean_s1.h5 --resample   # eval a saved .h5
```

### Gesture length: native vs resample

`AugConfig.resample` (default `False`) must match how the live pipeline feeds the model:

| `resample` | training | live pipeline | tag suffix |
|---|---|---|---|
| `False` (native) | gesture keeps its 100 Hz length (time-warp varies speed ×0.9–1.1) and slides anywhere across the 150-step window, up to `slide_max_out` (0.3) of it cut off either edge | default ring buffer: last 150 samples at end-of-flag | `_native` |
| `True` | every gesture resampled to 100 steps, random offset −40…90 | `--resample` (resampled span, centred) | none |

`exp_final` / `exp_ablation` pin `resample=True` and `filler="gesture"` so they still reproduce the
old-vocab results. Evaluate a model with the mode it was trained in (`exp_eval --resample` for
resampled models).

**Option fields are validated.** Their choices are `Literal` types at the top of `config.py`
(`Filler`, `Pool`, `Channels`); setting anything else — in the constructor or later — raises
`ValueError` listing the valid choices, and `exp_new_vocab --filler` only accepts those choices.

### Test sets

| test | windows | why |
|---|---|---|
| **jitter** (`build_test`) | each test gesture ×4 at a random start offset −40…40 | the original metric, unchanged, so new runs compare with old ones |
| **ceiling** (`build_test`) | each gesture centred | upper bound |
| **slide** (`build_slide_test`) | each gesture at 5 evenly spaced offsets, from `slide_max_out` cut off the left edge to the same off the right | robustness to cut-off / partial gestures; same rule for every model |
| **slide_bg** (`build_slide_test(bg=...)`) | the slide test with held-out **real recorded background** as filler | the most live-like number; for every model, needs the recordings |

`train_eval` prints all of them per seed (slide / slide_bg are also in `run_<tag>.json`; `results.csv`
keeps its old columns). `exp_eval` prints slide and slide_bg accuracy per position (head cut → tail cut).
jitter / ceiling / slide always use gesture-chunk filler, so they compare across filler modes.

### Metrics: what each number means

Every test window is built once (fixed seeds) from the **test split only**, never augmented, and
scored by every seed. All numbers are fractions of windows (1.0 = all).

| metric | windows it scores | correct when | answers |
|---|---|---|---|
| **jitter** | every test gesture ×4, start offset −40…40, gesture-chunk filler | predicted = true gesture | the original headline number; compare any two models |
| **ceiling** | every test gesture once, centred | predicted = true gesture | upper bound: the gesture fully in view |
| **slide** | every test gesture at 5 positions, from 30% cut off the left edge to 30% off the right | predicted = true gesture | does it still recognise a cut-off gesture? `exp_eval` splits it per position |
| **slide_bg** | slide, but the filler is held-out **real background** | predicted = true gesture | the most live-like accuracy: real motion around the gesture |
| **false reject** *(noise models)* | the slide_bg gesture windows | predicted **noise** (wrong) | how often a real gesture would be **swallowed** — no game event |
| **false accept** *(noise models)* | held-out background-only windows (no gesture), as many as the largest class has jitter windows | predicted **a gesture** (wrong) | how often idle / random motion would **fire** a game event |
| **per-class recall** | jitter windows (+ the noise windows for `noise`) | per class: predicted = that class | which gesture is weakest; `noise` recall = 1 − false accept |
| **confusion / worst pairs** | same as per-class | — | which class gets mistaken for which (e.g. `star->circle 2.9%`) |

How they fit together: a gesture window ends one of three ways — **right gesture** (counted by
slide_bg), **wrong gesture**, or **noise** (false reject); so slide_bg + wrong + false reject = 1. A
background window either says noise (good) or fires a gesture (false accept). A 7-class model has no
noise output, so its false accept is always 1.0 before any threshold — the reason for the noise class.

`exp_eval` adds the **threshold table** for any model: false accept vs false reject (gesture → no event)
when the comms rule also rejects a top probability below 0.0 / 0.5 / 0.7 / 0.9. Raising the threshold
trades fewer false accepts for more false rejects; pick the row that suits the game, then set
`CONF_THRESHOLD` in `AI/board/wk10_integration.py`.

The **visible-length sweep** in `exp_eval` (accuracy with only 0 / 10 / … / 100 gesture steps in the
window) is a leakage probe: at 0 visible steps a 7-class model should sit near chance (1/7 ≈ 0.14).

### Window filler: gesture chunks vs real background

`AugConfig.filler` picks what surrounds the gesture in a training window (`exp_new_vocab --filler`):

- `"gesture"` (the original): 5–24-step chunks of other gestures + `noise_frac` noise.
- `"background"` (**default**, tag `_bg`): one contiguous 150-step slice of **real background** —
  the **unlabelled** time of the reviewed continuous recordings, gesture and noise recordings alike
  (`SESSIONS_DIR` = `Hardware/Data Collection/sessions`, subfolders included, or `GESTURE_SESSIONS_DIR`),
  kept `bg_margin_us` (200 ms) clear of **every** labelled interval and split at sensor gaps. That includes
  `noise` intervals: in the noise recordings those mark random gestures of the 7 classes, so they are
  neither background nor a class. Unreviewed recordings are skipped (they may hide unmarked gestures).
  Encoded exactly like the gesture set (every 5th 500 Hz sample, int16 counts). `noise_frac` noise is
  still added on top; set it to 0 for pure recorded background.

### 8th class: `noise` (`exp_new_vocab --noise`, tag `_noise`)

`DataConfig(noise=True)` adds a no-gesture class so the model can say "nothing happened" instead of
forcing one of the 7 gestures:

- **Index 7, always last** (`names()` = sorted gestures + `noise`), so gesture indices 0–6 never move
  and the comms label map only gains `7: noise`.
- **What a noise window is:** real **unlabelled** background only, drawn fresh each time from that
  split's background blocks and augmented like a gesture (`augment.noise_window`). Native: one
  contiguous 150-step slice (time-warp varies its speed). Resampled: an 80–250-step span
  (`NOISE_SPAN`) sent through the gesture path, as the live `--resample` path would show it.
- **Labelled `noise` intervals are never used** — they are random gestures of the 7 classes, so they
  are excluded from background, filler and every class (also skipped if they appear in a dataset index).
- **How many:** `noise_ratio` (1.0) × the largest gesture class per split, each repeated `mult` times.
- **Needs `filler="background"`** (enforced): with gesture-chunk filler, "gesture chunks present" would
  separate the classes, which never holds live.
- **Metrics** (printed per seed + in `run_<tag>.json`): jitter / ceiling / slide / slide_bg stay
  gesture-only and comparable; plus **false reject** (slide_bg gesture windows predicted noise) and
  **false accept** (held-out background-only windows predicted a gesture). Per-class and the confusion
  matrix include `noise`.

`exp_eval` detects an 8-class model from its output size, and for **any** model prints a no-gesture
test plus a threshold table — false accepts vs false rejects at confidence 0 / 0.5 / 0.7 / 0.9 under the
comms rule (no event if top = noise or top p < threshold) — to pick `CONF_THRESHOLD`.

Board: an 8-class model needs `N_CLASSES = 8` + `label_map.json` with `7: noise` and a new bitstream;
the `.h5` path (`keras_stream`) reads the output size from the model, so it works as is.

The background is chopped into `bg_block` (6 s) blocks and split 70/15/15 with `split_seed`, so test
filler is never trained on. The first load caches it as `dataset/background_v2_m200.npz` (rebuilt when
the set of recordings changes).

`exp_eval` prints per-class recall, the confusion matrix, the offset-robustness sweep, and the
visible-length leakage probe (the `eval_offsets` / `eval_visible_lengths` sweeps from the old baseline).
Add `--smoke` to `exp_new_vocab` for a 1-seed, 3-epoch sanity run. Needs `tensorflow`, `tf-keras`,
`qkeras` (the env var for legacy Keras is set in `__init__.py`).

### Datasets: regenerating from the recordings

`export_dataset` rebuilds a dataset folder from the continuous recordings in `SESSIONS_DIR`
(`Hardware/Data Collection/sessions`, subfolders included). Two kinds of recording mix freely; the
rate comes from each `.meta.json` `raw_hz`:

- **500 Hz USB** (Hardware's `record.py`): every 5th sample → 100 Hz (`decimate-500to100-v1`).
- **100 Hz BLE** (`Comms/laptop/record_ble.py`, saved in `sessions/ble/`): every sample
  (`ble-100hz-v1`), the stream live inference sees. The background loader handles both the same way.


```bash
python -m gesture_pipeline.export_dataset --out new_gesture_100hz_v2
python -m gesture_pipeline.exp_new_vocab --noise --data new_gesture_100hz_v2 --tag new_lean_v2_noise
```

- Every labelled symbol of every **reviewed** recording → one CSV: cropped to its interval, every 5th
  500 Hz sample (100 Hz, variable length), int16 counts — the `decimate-500to100-v1` /
  `imu-counts-int16-v1` encoding of `new_gesture_100hz`. Re-exporting the original 38 recordings
  reproduces all 1089 `new_gesture_100hz` gestures **exactly** (counts and index fields).
- `ignore` and `noise` intervals are not exported (noise intervals are random gestures); unreviewed
  recordings and crops across a sensor gap go to `skipped.json`.
- The output folder must be new or empty, so new recordings always go to a new folder.

| dataset | gestures | from |
|---|---|---|
| `new_gesture_100hz` | 1089 (chop 115, circle 237, fireball 115, jabs 115, lightning 115, star 277, tank 115) | p1 day1–3 (circle, star) + p2 day1 (all 7) |
| `new_gesture_100hz_v2` | 1189 (chop **215**, others unchanged) | the above + `p2_day2_chop_air_front_cw_standing` (100 standing chops) |

The background (filler / noise class / slide_bg / false accept) is not part of a dataset folder: it is
read from the same recordings by `data.load_background` and cached as `dataset/background_v2_m200.npz`.

### Where the data is read from

Dataset paths come from **one constant**, `DATA_ROOT` in `config.py` — not a hardcoded location.
`DataConfig(dir=...)` resolves a bare name (e.g. `"new_gesture_100hz"`) under `DATA_ROOT`; an
absolute `dir` is used as-is. `DATA_ROOT` defaults to this package's `dataset/` folder, and the
`GESTURE_DATA_ROOT` env var overrides it without editing code:
```bash
GESTURE_DATA_ROOT=/path/to/datasets python -m gesture_pipeline.exp_new_vocab
```
So you can move this package anywhere and just point `DATA_ROOT` (or the env var) at the datasets;
results always write to `gesture_pipeline/results/`.

## Adding an experiment

Make `exp_<name>.py`, compose a `RunConfig`, call `train_eval`. Do not add training logic there —
if you need a new knob, add a field to the right dataclass in `config.py`.

## Notes

- `model.py` is a deliberate **copy** of the baseline architecture, kept standalone; if you change the
  architecture, change it here and in `../gesture_real_baseline_v3.py` (they must stay in sync).
- A trained `.h5` runs in the comms pipeline via `AI/board/keras_stream.py` (`--ai-h5`); see the
  ablation `README.md` "Testing the model in the comms pipeline" section.
