# Gesture augmentation ablation + models

This folder holds the on-the-fly augmentation experiments for the gesture QCNN: an
ablation that chose the augmentation set, a feature-engineering variant, and a retrain
on the new gesture vocabulary. All training reuses the architecture in
`../gesture_real_baseline_v3.py` (`build_qcnn`) — only the augmentation, input channels,
and dataset change. The last section walks through running a trained `.h5` live in the
comms pipeline on the laptop, before converting it to an FPGA bitstream.

## Setup

- **Architecture:** the baseline QCNN, unchanged (see shared hyperparameters below).
- **Data (old vocab):** `../../Model/cloud_manual_day1_raw100hz_int16` — 1466 gestures, **p1 / day 1 only**, decoded to SI (m/s², rad/s) with the board constants, no normalization.
- **Pipeline:** `augment_stream.py` augments each gesture on the fly, then lays it into a WIN=150 window with the baseline's offset/crop + filler + noise (always on).
- **Test protocol:** every run is scored on ONE cached test set (`test_fixed.npz`, 888 jitter + 222 ceiling windows), so results are comparable. **Jitter** = random-offset windows (realistic streaming), the primary metric; **ceiling** = centred gesture (upper bound).

> **The experiments here are now a clean standalone package at [`../gesture_pipeline/`](../gesture_pipeline/README.md).**
> Use it to run/retrain (`python -m gesture_pipeline.exp_new_vocab`, `exp_final`, `exp_ablation`, `exp_eval`).
> The original scattered scripts are archived in `legacy/`; this README remains the results write-up.

## Datasets

Two gesture vocabularies, same encoding pipeline (500 Hz raw → decimate every 5th sample →
100 Hz, variable length, int16 counts; no resampling).

### Old vocabulary — `../cloud_manual_day1_raw100hz_int16`
- 7 classes: **arc, caret, circle, infinity, star, triangle, z**
- 1466 gestures, **p1 / day 1 only**
- Built by decimating `raw_int16` (already int16) to 100 Hz within each labelled span.

### New vocabulary — `../new_gesture_100hz`
- 7 classes, mapped to game roles:

| gesture label | role | samples | source |
|---|---|---|---|
| lightning | lightning (spell) | 115 | p2 |
| fireball | fireball (spell) | 115 | p2 |
| chop | warrior | 115 | p2 |
| jabs | rogue | 115 | p2 |
| tank | shield | 115 | p2 |
| circle | mage | 237 | 222 p1 + 15 p2 |
| star | hero | 277 | 262 p1 + 15 p2 |

- 1089 gestures total (p1 484, p2 605).
- Built by **decimate-the-float-then-quantize** from the combined `sessions` recordings (float g/dps):
  crop each labelled span → take every 5th float sample → quantize to int16 (accel ×8192, gyro ×65.5,
  round ties-away, clamp). Proven byte-identical to the old decimate-int16 method.

### What changed
- **Vocabulary redesign (the point):** dropped **arc, caret, infinity, triangle** (the confusable
  "draw a 2-D shape" ones); kept **circle, star**; **z → lightning** (same zig-zag motion, renamed);
  added **fireball** (forward punch), **tank** (shield raise), **warrior** (chop), **rogue** (jabs).
  The new gestures are **motion-distinct** — each dominates a different signal (corners / forward-impulse /
  rotation / vertical / oscillation / multi-stroke) — instead of all being planar symbols.
- **Separability jumped:** same 15/class leave-one-out probe (`separability*.py`), **old 75% → new 100%**.
  The old curved family (arc/circle/infinity) and triangle↔z overlapped intrinsically; the new set has none.
- **People:** old = p1 only; new = p2 for the five new gestures, with p1 circle/star reused →
  **single-person-per-class** (see caveats).
- **Unchanged:** 500 Hz source, 100 Hz decimation, int16 encoding (`imu-counts-int16-v1`),
  variable length, processing id `decimate-500to100-v1`.

## Models trained

| model | input | augmentation | params | files | jitter (3-seed) | ceiling | notes |
|---|---|---|---|---|---|---|---|
| **none** | 6-ch | window only (no signal aug) | 8,711 | — (not saved; `results.csv` tag `none3`) | 0.9478 | 0.9655 | baseline anchor |
| **lean** | 6-ch | amp + time-warp + rotate + mag-warp | 8,711 | `models/lean_s{1,2,3}.h5` | 0.9632 | 0.9745 | best 6-ch; best file `lean_s1` (0.9662) |
| **feat** | 10-ch | same lean augmentation | 9,031 | `models/feat_s{1,2,3}.h5` | 0.9658 | 0.9850 | **best overall**; best file `feat_s2` (0.9707 / 0.9865) |
| **new_lean** | 6-ch | same lean augmentation | 8,711 | `models/new_lean_s{1,2,3}.h5` | **0.9926** | 0.994 | **best on new vocab**; drop-in 6-ch, no feature computation |
| new_feat | 10-ch | same lean augmentation | 9,031 | `models/new_feat_s{1,2,3}.h5` | 0.9896 | 0.992 | new vocab; features don't help once classes are separable |

**6-ch vs 10-ch (feat):** the 10-ch model adds 4 orientation-invariant channels to the raw 6 — accel magnitude, gyro magnitude, `|Δaccel|` (jerk), `|Δgyro|` (corner signal). It wins clearly on ceiling and ties/slightly-beats on jitter, at the cost of computing those 4 channels on the board + a larger DMA (WIN×10 vs WIN×6).

## Shared hyperparameters

Architecture (`build_qcnn` / `build_feat_model`, from `gesture_real_baseline_v3.py`):

| | value |
|---|---|
| window / gesture length | WIN=150, GEST=100 |
| input channels | 6 (lean/none) or 10 (feat) |
| conv blocks | 3 × [QConv1D(filt, k=5, valid, no bias) → BatchNorm → QActivation(relu) → SpatialDropout1D → MaxPool1D(2)] |
| filters | F1=16, F2=32, F3=32 (F4=0, no 4th block) |
| kernel / dropout / pool | KSIZE=5, DROPOUT=0.1, global **max** pool |
| head | QDense(7) → logits |
| quantizers | weights/bias `quantized_bits(8,2)`; input+output `quantized_bits(16,6)` = `ap_fixed<16,6>`; conv act `quantized_relu(16,6)` |
| classes | 7 |

Training (set in `run_final.py` / `run_features.py` / `train_new.py` for the saved models):

| | value |
|---|---|
| optimizer / lr | Adam, 1e-3 |
| loss / metric | SparseCategoricalCrossentropy(from_logits=True) / accuracy |
| batch size | 64 |
| epochs / early stop | ≤120, EarlyStopping(val_loss, patience=15, restore_best_weights) |
| augmented windows per gesture (`mult`) | 12 |
| seeds | 1, 2, 3 (averaged) |

(Screening runs — leave-one-out and strength sweep — used `mult=8`, `≤100` epochs, `patience=12`, 1 seed, and were not saved.)

## Augmentation reference

All augmentations run on the fly in `augment_stream.py`, in SI units, on each gesture
*before* windowing. `ch_std` = per-channel std of the clean training gestures. Each has an
on/off, probability `p`, and strength (in `AugConfig`). Order:
boundary → rotate → amp → mag-warp → time-warp + resample to 100 → bias → jitter.

| aug | what it does | range (default) | p | in lean |
|---|---|---|---|---|
| amp | one gain for the accel triple, a separate one for the gyro triple (draw size / vigor) | ×U(0.85, 1.15) each | 0.9 | yes |
| time-warp | non-uniform time remap: smooth speed profile `exp(curve(5 knots, σ))` → monotonic grid, sampled to 100 (local speed variation) | σ=0.20, 5 knots | always | yes |
| rotate | one small 3-D rotation (Rz·Ry·Rx), applied to **both** accel and gyro with the same R (hold angle) | ±12° per axis | 0.8 | yes |
| mag-warp | per-channel smooth time-varying gain × (1 + `curve(4 knots, σ)`) (varying effort) | σ=0.12, 4 knots | 0.7 | yes |
| jitter | per-sample Gaussian noise per channel, after resample | N(0, 0.05·ch_std) | 0.9 | no |
| bias | one constant per-channel offset, after resample | N(0, 0.05·ch_std) | 0.5 | no |
| boundary | trim a random fraction off each end before resampling | U(0, 0.10) per end | 0.6 | no |

`curve(k, σ)` = k control points ~N(0, σ), linearly interpolated. Window-level augs (always
on, not ablated): **offset/crop** (gesture start random in [−40, +90] of the window, may hang
off an edge), **filler** (rest of window = random 5–24-step slices of random gestures), **filler
noise** N(0, 0.05·ch_std).

## How the lean set was chosen (ablation, old vocab)

**Leave-one-out** (remove each aug from the full set; bigger drop = more load-bearing; 1 seed, noise ≈ ±1%):

| removed | jitter | Δ vs all | verdict |
|---|---|---|---|
| — (all) | 0.9696 | — | |
| time-warp | 0.9448 | −2.5% | load-bearing |
| mag-warp | 0.9493 | −2.0% | load-bearing |
| rotate | 0.9516 | −1.8% | load-bearing |
| amp | 0.9572 | −1.2% | helps |
| bias / jitter / boundary | 0.964 / 0.966 / 0.966 | −0.6% / −0.3% / −0.3% | within noise → dropped |

Why: **time-warp** diversifies the temporal profile (durations vary a lot but all squash to 100);
**rotate** covers hold-angle; **mag-warp/amp** broaden the amplitude envelope. **jitter/bias/boundary**
are redundant with the window filler/offset, so they were dropped → the **lean** set.

**Strength sweep** (1 seed): the defaults are the sweet spot — both weaker and stronger hurt.
time-warp σ=0.20 (vs 0.10→0.957, 0.35→0.953), mag-warp σ=0.12 (0.20→0.958), rotate 12°
(6°→0.958, 20°→0.957 with ceiling ↓). 20° rotation starts turning oriented symbols into neighbours.

**Why 3 seeds:** single-seed `none` scored 0.9673 (a ~2% lucky draw that would have said
"augmentation is useless"); the 3-seed `none` is 0.9478. Averaging seeds flipped the verdict to a
clear, consistent win — so the headline uses 3-seed means.

## Results (old vocab)

- **lean beats none by +1.5% jitter**, seed ranges non-overlapping (worst lean 0.9606 > best none 0.9516) → real, not noise.
- **feat beats lean on ceiling** clearly (+1.0%, non-overlapping) and ties on jitter.
- Per-class (lean): star/z 0.99, infinity 0.98, triangle/arc 0.96, circle 0.94, **caret 0.91** (weakest).

**Confusion (lean, jitter, 3 seeds pooled; `confusion.py` → `confusion_jitter.csv`):** worst pairs are
**triangle → z** (3.9%, sharp-corner pair), the **curved family** arc ↔ circle ↔ infinity (geometric
overlap), and **caret** failing diffusely. This overlap is what motivated the new, motion-distinct vocabulary.

## Results (new vocabulary)

Both models on `new_gesture_100hz` (3 seeds, own fixed test set: 672 jitter + 168 ceiling windows):

| model | input | jitter (3-seed) | ceiling | params |
|---|---|---|---|---|
| **new_lean** | 6-ch | **0.9926** (all 3 seeds 0.9926) | 0.9940 | 8,711 |
| new_feat | 10-ch | 0.9896 (0.988–0.991) | 0.9920 | 9,031 |

**On the new vocabulary the plain 6-channel model wins** — simpler, fewer params, no on-board feature
computation, marginally higher. The engineered channels helped on the OLD *overlapping* set (they fixed the
curved family) but add nothing once the classes are already well-separated. Rule of thumb: **feature-engineer
when classes overlap; drop it when they don't.** → use **`new_lean`** for this vocab; it drops straight into
the board's 150×6 path with zero extra preprocessing.

Per-class jitter (new_lean, 3-seed): warrior/chop **1.000**, rogue/jabs **1.000**, lightning **1.000**,
shield/tank **1.000**, mage/circle 0.998, fireball 0.991, hero/star 0.977. Only real residual: **star → circle
2.3%** (the two reused p1 curved/looping gestures). The design worry-pairs are gone — shield↔warrior and
fireball↔rogue ≈ 0.

Matches the separability probe (new 100% vs old 75%): the motion-distinct vocabulary trains to ~99% where the
old overlapping set plateaued ~96.6%. **Caveat:** single-person-per-class (circle/star p1, rest p2), so this
is in-distribution, not a cross-person number.

## Caveats

- **Single person / day.** Old-vocab data is all p1/day1, so accuracy is near-ceiling and gains are
  modest; cross-person generalisation is untested. The new-vocab dataset is also single-person-per-class
  (circle/star = p1, rest = p2), so `new_feat` numbers are in-distribution, not cross-person.
- **Board saturation:** amp×1.15 can push a sample to ~±42 m/s² — inside the `|x|<64` training quantizer
  (clean data already hits ±39) but over the board's `to_fixed` ±32 input range. Cap `amp_hi`≈1.05 for
  strict parity; it barely changed accuracy here.

## Scripts & files

Training/analysis files in this folder:

| file | purpose |
|---|---|
| `run_ablation.py` | ablation harness: leave-one-out + strength phases, builds split + cached test set |
| `run_final.py` | trains the final `lean` and `none` models (3 seeds) |
| `run_features.py` | the 10-channel `feat` model (`build_feat_model`, `add_features`) |
| `train_new.py` | retrains `feat` on the new vocabulary (`new_gesture_100hz`) |
| `train_new_lean.py` | retrains the 6-ch `lean` model on the new vocabulary |
| `eval_perclass.py` | per-class accuracy for saved models |
| `confusion.py` / `confusion_compare.py` | confusion matrix; lean-vs-feat comparison |
| `results.csv` / `new_results.csv` | per-run metrics (old / new vocab) |
| `run_*.json` | per-run config + per-class metrics |
| `test_fixed.npz` / `test_fixed_new.npz` | cached test windows (old / new vocab) |
| `logs/` | per-run training logs (the `model.fit` output) |
| `models/` | saved `.h5` models |

The comms-side files for the laptop test — `keras_stream.py` and the `--ai-h5` flag in
`gesture.py` / `tcp_client.py` — live in the `cg4002` repo under `AI/board/` and `Comms/ultra96/`, not here.
