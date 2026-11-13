# BLE-count useful-motion detector

The MPU6050 still acquires at 500 Hz (DLPF 3, ±4 g, ±500 degrees/s). The
unchanged sketch sends its latest raw reading every nominal 10 ms. All packets
are sent regardless of classification. `gestureFlag()` now feeds that **same**
selected `int16_t[6]` reading into the detector; it no longer classifies a separate
five-sample average. `firebeetle.ino`, `imu.h`, and `segments.h` are unchanged.

`gesture_core.h` keeps 30 selected readings and computes 42 count-unit features
every five samples (20 decisions/s). Each axis contributes mean, standard
deviation, range, mean absolute difference, and three 10-sample block means.
Integer history is retained as int16; double accumulators avoid overflow and
cancellation, and the final features and tree thresholds are float32. No input
is converted to g or degrees/s. Segment energy uses equivalent count-domain
scale constants to retain the existing segmenter's dimensionless thresholds.

`stream_detector.h` owns streaming history and the existing segmenter. Invalid
IMU readings or a gap of at least 20 ms between packet calls reset all history.
Unsigned elapsed time handles `millis()` wrap. Ordinary 10 ms scheduling jitter
does not change the nominal model grid. The first decision after a reset needs
30 valid readings. Sensor recovery also resets the detector. No detection runs
between outgoing packets. A disconnect or file transfer lasting at least 20 ms
is detected on the next packet; no BLE callbacks or motor code were changed.

The existing segmenter's end, rearm and timeout rules remain in force, including
its legacy 2 ms endpoint offset. `RAW_*` constants still describe that legacy
sensor/capture grid; `SAMPLE_*` describe classification. The unused `Capture`
helper in `segments.h` is not involved in streaming or model inference.

## Train and reproduce

From this directory, with Python, NumPy, scikit-learn and a C++11 compiler:

```powershell
python -m pip install -r requirements-training.txt
python pipeline.py --sessions ../../../sessions/ble --run-name ble_comparison
python pipeline.py --sessions ../../../sessions/ble --legacy-sessions ../../../sessions --run-name combined_500hz
python training/test_resampling.py
python training/compare_runs.py
```

A training command exports its candidate to the root `model_data.h`. Named runs
also preserve their own header and reports under `training/<run-name>`.
**Running the combined command above replaces the active header with the combined
candidate.** To restore the recommended BLE model after comparing:

```powershell
Copy-Item training/ble_comparison/model_data.h model_data.h
```

`--verify-only --run-name <name>` verifies that run against the **currently active**
header, so install the matching header before running it. It does not select or
train a model. `--compiler` accepts a C++ compiler path. The development machine
used `C:/Users/tzeyo/anaconda3/python.exe` and `C:/MinGW/bin/g++.exe`.

All changes and output stay inside this directory. Original recordings are never
modified. `training/ble_only_baseline` preserves the original run from before the
500 Hz experiment (TY test holdout); do not compare its test percentages directly
with the newer Jermaine holdout. `training/model_data.previous.h` preserves the
original pre-retraining model; it requires the old float/averaged frontend and
is not a drop-in rollback.

## Input handling and 500 Hz conversion

Only reviewed recordings with DLPF 3, ±4 g and ±500 degrees/s are accepted.
The loader reconstructs counts as `round(acceleration * 8192)` and
`round(gyroscope * 65.5)`, verifies reconstruction within 0.001 count and int16
bounds, and rejects incompatible/nonmonotonic data. Recorded `gesture_flag`
values are never used as labels or features.

Useful labels: circle, star, lightning, fireball, tank, chop, jabs. Noise and
unlabelled samples are negative. Ignore intervals reset replay and feature
history and are excluded from metrics. No feature window spans an ignore
interval, an invalid reading or a missing packet. Unreviewed/empty recordings
are excluded and listed in each run's manifest.

For 500 Hz input, select the fifth sample of each five-sample group on a fixed
sequence grid. **Do not average or interpolate.** This preserves original raw
counts at nominal 100 Hz. A missing raw reading invalidates the next selected
group even if the missing reading would not have been selected. Ignore intervals
between selected readings also invalidate feature history. Partial trailing
groups are discarded. Original timestamp/label alignment is retained; only the
selected sequence index is expressed on the 100 Hz grid. This approximates BLE
selection, but cannot reconstruct real BLE scheduling jitter or packet losses.

The combined run included 45 reviewed 500 Hz recordings, selecting 644,898 rows
from 3,224,578 raw rows with 78 source gaps, plus the same 33 reviewed BLE files
used by the comparison run. All 500 Hz recordings were assigned to training.

## Fixed comparison split

| Split | Recorded participant IDs |
| --- | --- |
| BLE training | TY, Brandon, Yuwen |
| Additional 500 Hz training | p1, p2, John |
| Validation / threshold selection | Juven |
| Test, never fitted or used to select | Jermaine |

These holdouts follow the user's requested choice of Juven, Brandon or Jermaine.
Legacy IDs p1/p2 were retained as recorded; their real-name mapping was not
supplied. Splits are separate by recorded ID, not independently verified identity.
Every split contains all seven useful gestures and background. CSV, metadata and
annotation hashes are recorded; the comparison script verifies identical
validation/test sources and labels between runs. Both runs use the same seed,
model sizes and threshold candidates. No tuning uses test results.

Candidates are a depth-6 tree and an eight-tree depth-6 forest, with balanced
classes, minimum 20 samples per leaf and seed 16. Threshold selection measures
the complete state machine's outgoing flags. Among candidates within one
percentage point of the best useful-sample recall, retain gesture detection
within one point of the best, then minimize active background. Start and stop
thresholds must be assessed together because the segmenter's locks also affect
retention.

## Results and active model

**The new BLE-only comparison model remains active**, because its validation
useful-sample recall is higher. The combined model is saved as an evaluated
candidate at `training/combined_500hz/model_data.h`.

| Metric | BLE only | BLE + selected 500 Hz |
| --- | ---: | ---: |
| Validation useful samples flagged active | 66.3% | 62.4% |
| Validation background flagged active | 36.0% | 26.7% |
| Test useful samples flagged active | 68.1% | 56.6% |
| Test background flagged active | 22.9% | 3.8% |
| Test gestures with at least one active useful sample | 357/357 | 352/357 |
| Test 95th percentile onset delay, detected gestures | 600 ms | 732.5 ms |
| Tree nodes | 119 | 127 |
| Start / stop thresholds | 0.20 / 0.10 | 0.60 / 0.40 |

Adding the older data reduced false activations, but sacrificed useful samples.
That trade-off is contrary to the requested priority of avoiding missed useful
motion. This result does not establish that 500 Hz data are intrinsically less
useful: dataset composition, movement/participant differences, fixed tree
capacity and segmentation can all contribute; no causal diagnosis was made.

Detecting some part of a gesture is not complete retention. The active model
still leaves 31.9% of useful test samples unflagged. Exact annotations have
uncorrected capture/keyboard latency. The current segmenter's warmup and locks
can also suppress useful samples. No retroactive flags or prebuffer are credited
in evaluation: the existing protocol cannot change already-transmitted flags.

Python and C++ had zero flag/event mismatches for the combined run across all
78 recordings, 1,302,045 selected samples and 253,080 decisions. The BLE-only
comparison also passed across 657,147 samples and 127,911 decisions. Five
conversion checks cover selection phase, partial groups, missing selected and
unselected readings, and ignore intervals between selections. Existing C++
checks cover int16 extremes, status/gap resets and clock wrap.

## Artifacts and firmware checks

- `training/combined_500hz/`: combined candidate, manifest, selection/test reports,
  exported header, pipeline snapshot and C++ verification report.
- `training/ble_comparison/`: matching BLE-only run on the same holdouts.
- `training/ble_only_baseline/`: original run, with the earlier participant split.
- `training/comparison.json`: side-by-side metrics and holdout identity check.
- `training/active_run.json`: active model decision and its validation basis.
- Top-level `training/model.json`, `manifest.json`, `selection_report.json`,
  `test_report.json`, `cpp_verification.json`: copies for the active model.
- `training/unchanged_files.json` and `preservation_check.json`: source-preservation
  checks for the sketch, public interface and segmentation code.
- `training/environment.json`: training/build tool versions.
- `training/firmware_build.json`: latest full firmware build receipt.

Arduino requires the sketch directory to match the main `.ino` name, so builds
use identical firmware copies under `training/build_sketch/firebeetle`. The
FireBeetle-ESP32 target is `esp32:esp32:firebeetle32`. The sketch, BLE protocol,
motor handling, sensor configuration and segmentation implementation were not
changed for this experiment. No board was flashed. Offline checks do not measure
actual ESP32 latency, BLE scheduling or motor interference; verify those on the
device using the existing timing logs.