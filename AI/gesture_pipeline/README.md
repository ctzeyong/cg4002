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
| `evaluate.py` | accuracy, per-class recall, confusion matrix |
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

## Run (from `baseline/`)

```bash
python -m gesture_pipeline.exp_new_vocab              # new vocab, 6-ch lean, native length
python -m gesture_pipeline.exp_new_vocab --resample   # same, resampled to 100 steps (= new_lean)
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

`exp_final` / `exp_ablation` pin `resample=True` so they still reproduce the old-vocab results.
Evaluate a model with the mode it was trained in (`exp_eval --resample` for resampled models).

### Test sets

| test | windows | why |
|---|---|---|
| **jitter** (`build_test`) | each test gesture ×4 at a random start offset −40…40 | the original metric, unchanged, so new runs compare with old ones |
| **ceiling** (`build_test`) | each gesture centred | upper bound |
| **slide** (`build_slide_test`) | each gesture at 5 evenly spaced offsets, from `slide_max_out` cut off the left edge to the same off the right | robustness to cut-off / partial gestures; same rule for every model |

`train_eval` prints all three per seed (slide is also in `run_<tag>.json`; `results.csv` keeps its
old columns). `exp_eval` prints slide accuracy per position (head cut → tail cut).

`exp_eval` prints per-class recall, the confusion matrix, the offset-robustness sweep, and the
visible-length leakage probe (the `eval_offsets` / `eval_visible_lengths` sweeps from the old baseline).
Add `--smoke` to `exp_new_vocab` for a 1-seed, 3-epoch sanity run. Needs `tensorflow`, `tf-keras`,
`qkeras` (the env var for legacy Keras is set in `__init__.py`).

### Where the data is read from

Dataset paths come from **one constant**, `DATA_ROOT` in `config.py` — not a hardcoded location.
`DataConfig(dir=...)` resolves a bare name (e.g. `"new_gesture_100hz"`) under `DATA_ROOT`; an
absolute `dir` is used as-is. `DATA_ROOT` defaults to the `Model/` folder two levels up, and the
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
