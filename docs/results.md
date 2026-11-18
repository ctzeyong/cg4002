# Gesture model results

Every model trained for the gesture classifier, in order, and why the deployed one was chosen. How
to train and what each metric means are in [AI/train/README.md](../AI/train/README.md); the datasets
are listed in [AI/data_collection/README.md](../AI/data_collection/README.md).

All models share one architecture (`AI/train/model.py`), a quantised 1-D CNN sized for the FPGA:

| | Value |
|---|---|
| Input | 150 steps × 6 channels (accel m/s², gyro rad/s), 100 Hz; 10 channels for the feature model |
| Conv blocks | 3 × [QConv1D (k=5, valid, no bias) → BatchNorm → ReLU → SpatialDropout1D(0.1) → MaxPool1D(2)], filters 16 / 32 / 32 |
| Head | Global max pool → QDense(classes), logits |
| Quantisers | Weights `quantized_bits(8,2)`; input, output and activations 16-bit with 6 integer bits (`ap_fixed<16,6>`) |
| Parameters | 8,711 (7 classes), 8,744 (8 classes), 9,031 (10-channel feature model) |
| Training | Adam 1e-3, batch 64, ≤120 epochs, early stopping on val_loss (patience 15), 12 augmented windows per gesture, seeds 1–3 averaged |

## 1. Choosing the augmentation (old vocabulary)

The first vocabulary was 7 drawn shapes (arc, caret, circle, infinity, star, triangle, z): 1,466
gestures, one person (p1), day 1 (`cloud_manual_day1_raw100hz_int16`). Each gesture was resampled to
100 steps and laid into the 150-step window at a random offset, with chunks of other gestures as
filler. Reproduce with `python -m train.exp_ablation` and `python -m train.exp_final`.

**Leave-one-out** (remove each augmentation from the full set; 1 seed, noise about ±1%):

| Removed | Jitter | Δ vs all | Verdict |
|---|---|---|---|
| none (all) | 0.9696 | | |
| time-warp | 0.9448 | −2.5% | load-bearing |
| mag-warp | 0.9493 | −2.0% | load-bearing |
| rotate | 0.9516 | −1.8% | load-bearing |
| amp | 0.9572 | −1.2% | helps |
| bias / jitter / boundary | 0.964 / 0.966 / 0.966 | −0.6% / −0.3% / −0.3% | within noise, dropped |

Time-warp diversifies the temporal profile, rotate covers how the hand is held, and mag-warp / amp
broaden the amplitude. Jitter, bias and boundary trimming were redundant with the window offset and
filler. The kept four are the **lean** set (`AugConfig.lean()`), used by every model since.

**Strength sweep** (1 seed): the defaults are the sweet spot. Time-warp σ=0.20 (0.10 → 0.957,
0.35 → 0.953), mag-warp σ=0.12 (0.20 → 0.958), rotate ±12° (6° → 0.958, 20° → 0.957 with ceiling
down: 20° starts turning oriented shapes into their neighbours).

| Augmentation | What it does | Default | p |
|---|---|---|---|
| amp | One gain for the accel triple, another for the gyro triple | ×U(0.85, 1.15) | 0.9 |
| time-warp | Smooth non-uniform speed profile (5 knots) | σ=0.20 | always |
| rotate | One small 3-D rotation applied to accel and gyro alike | ±12° per axis | 0.8 |
| mag-warp | Smooth time-varying gain per channel (4 knots) | σ=0.12 | 0.7 |

**Final models** (3 seeds):

| Model | Input | Jitter | Ceiling | Notes |
|---|---|---|---|---|
| none | 6-ch, window augmentation only | 0.9478 | 0.9655 | The anchor |
| lean | 6-ch, lean set | 0.9632 | 0.9745 | +1.5% over none; worst lean seed (0.9606) beats the best none seed (0.9516) |
| feat | 10-ch (+ accel and gyro magnitude, \|Δaccel\|, \|Δgyro\|), lean set | 0.9658 | 0.9850 | Best on ceiling |

Three seeds matter: a single-seed `none` scored 0.9673, which would have said augmentation is
useless. Per class (lean): star and z 0.99, infinity 0.98, triangle and arc 0.96, circle 0.94,
caret 0.91. The worst confusions were triangle → z (3.9%) and the curved family arc ↔ circle ↔
infinity, which overlap geometrically. That overlap is why the vocabulary was redesigned.

## 2. The game vocabulary

The confusable 2-D shapes (arc, caret, infinity, triangle) were dropped for moves that each stress
a different signal: **chop** (warrior), **circle** (mage), **fireball** (forward punch), **jabs**
(rogue), **lightning** (zig-zag, was z), **star** (hero) and **tank** (shield raise). On the same
15-per-class leave-one-out separability probe, the old set scored 75% and the new set 100%.

First models on `new_gesture_100hz` (1,089 gestures, p1 for circle and star, p2 for the rest; 3
seeds; the runs in `AI/train/results/`):

| Run | Change | Jitter | Ceiling |
|---|---|---|---|
| `new_lean` | resampled to 100 steps, gesture filler | 0.9901 | 0.9940 |
| `new_lean_native` | native length, slid across the window | 0.9931 | 1.0000 |

The 10-channel feature model scored 0.9896 here: once the classes are separable, the extra channels
stop helping, so every later model is 6-channel. Native length became the default because it is
what the live ring buffer sees.

## 3. Adding `noise`, real background and more people

A 7-class model has to name a gesture for every window, so idle motion fires game events. From
`new_lean_v2_noise` on, every model has the 8th class `noise` and real recorded background as the
window filler, and is scored on `slide_bg` (the most live-like accuracy), false reject (a real
gesture swallowed) and false accept (idle motion firing an event). All runs are 6-channel, lean
augmentation, native length:

| Run | Dataset | Seeds | Jitter | Ceiling | slide_bg | False reject | False accept |
|---|---|---|---|---|---|---|---|
| `new_lean_v2_noise` | `new_gesture_100hz_v2` | 3 | 0.9426 | 0.9891 | 0.9778 | 0.0153 | 0.0349 |
| `shapes_yuwen_noise` | `shapes_yuwen` | 3 | 0.9502 | 0.9844 | 0.9794 | 0.0081 | 0.0735 |
| `shapes_combined_noise` | `shapes_combined` | 3 | 0.8750 | 0.9574 | 0.9275 | 0.0078 | 0.0119 |
| `new_gestures_w_john_noise` | `new_gestures_w_john` | 3 | 0.9308 | 0.9812 | 0.9703 | 0.0182 | 0.0259 |
| `new_gestures_all` | `new_gestures_all` | 3 | 0.9126 | 0.9793 | 0.9753 | 0.0129 | 0.0206 |
| `new_gestures_updated_noise` | not recorded | 3 | 0.9149 | 0.9812 | 0.9821 | 0.0101 | 0.0122 |
| `new_gestures_all_v2_blebg` | `new_gestures_all_v2` | 3 | 0.9443 | 0.9857 | 0.9754 | 0.0140 | 0.0239 |
| `new_gestures_all_jabs` | `new_gestures_all_jabs` | 3 | 0.9250 | 0.9837 | 0.9732 | 0.0145 | 0.0259 |
| `new_gestures_all_jabs_roll` | `new_gestures_all_jabs` + wrist roll | 1 | 0.9311 | 0.9822 | 0.9738 | 0.0178 | 0.0287 |
| **`mage_noise_robust`** | `mage_noise_robust` | 1 (seed 2) | 0.9430 | 0.9849 | 0.9673 | 0.0185 | 0.0365 |

Jitter dropped from about 0.99 to 0.91–0.95 once the noise class and more people came in, while
slide_bg stayed at 0.97–0.98. Two likely reasons: the test set now holds up to 8 participant IDs
instead of one per class, and jitter always uses gesture-chunk filler, which the background-trained
models never see in training. `shapes_*` are the old shapes recorded over BLE, kept for comparison.

`new_gestures_all_jabs_roll` adds a wrist roll about the forearm axis (y) to circle, star and
`noise` only (`train.exp_roll_aug`); one seed, within noise of the run without it.

## 4. The deployed model: `mage_noise_robust`

`AI/ultra96/gesture_model.xsa` is `mage_noise_robust_s2.h5`. Its dataset is `new_gestures_all_jabs`
plus 100 more circle (mage) gestures from yuwen: 3,097 gestures from 8 participant IDs.

Per-class recall (jitter windows; `noise` on the background-only windows):

| chop | circle | fireball | jabs | lightning | star | tank | noise |
|---|---|---|---|---|---|---|---|
| 0.908 | 0.930 | 0.919 | 0.979 | 0.973 | 0.963 | 0.929 | 0.964 |

**After conversion** (`AI/hls4ml/convert.py`, on the exported test windows: 1,860 jitter, 2,325
slide_bg and 384 noise):

| Set | Keras | HLS C model |
|---|---|---|
| jitter | 0.9473 | 0.9473 |
| slide_bg | 0.9738 | 0.9742 |
| noise | 0.9766 | 0.9766 |

The HLS model's argmax agrees with Keras on 4,568 of 4,569 windows, and its event / no-event decision
(threshold 0.7) on 99.8%. Over all 4,569 windows, the HLS model's largest errors are tank → noise
(21), chop → jabs (17) and circle → jabs (14).

The board's power and inference time with this classifier are in [power.md](power.md); the
end-to-end latency is in [latency.md](latency.md).

## Caveats

- **Few people, few days.** Every dataset is a handful of people, so accuracy is in-distribution;
  a new player is untested beyond the 8 participant IDs.
- **One seed for the deployed run.** `mage_noise_robust` and the roll run trained one seed, so their
  numbers carry about ±1% seed noise. The run file does not record the command or the background
  recordings used.
- **Board saturation.** amp ×1.15 can push a sample to about ±42 m/s², inside the training
  quantiser (±64) but over the board's ±32 input range. Capping `amp_hi` at about 1.05 removes it and
  barely changed accuracy.
