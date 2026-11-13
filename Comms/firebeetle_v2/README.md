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

## Active training setup

The active run is `training/ble_juven_val_ty_test`. Open `firebeetle_v2.ino` and
use the root `model_data.h` with the other firmware files in this directory.

| Split | Participants |
| --- | --- |
| Training | Jermaine, Brandon, Yuwen |
| Validation and threshold selection | Juven |
| Test only | TY |

Only reviewed 100 Hz BLE recordings are used. The 500 Hz recordings are excluded.
The noise recording `yuwen_day4_noise_air_front_cw_standing_2.csv` in the sibling
`noise` folder is identical to its copy under `sessions/ble`, including metadata
and labels. It is included **once**, through the sessions copy: 8,550 samples,
1,705 negative training windows. There is no noise-specific weighting or duplicate
sampling. The unchanged balanced-class weighting applies to the entire dataset.
The verification is saved in the run's `noise_source_check.json`.

Useful classes are circle, star, lightning, fireball, tank, chop and jabs. Noise
and unlabelled time are negative. Ignore intervals are excluded and reset feature
history; no feature window spans an ignore interval, invalid reading or missing
packet. Unreviewed/empty files are excluded with reasons in the manifest.

The loader reconstructs counts using acceleration × 8192 and gyroscope × 65.5,
checks reconstruction within 0.001 count and int16 bounds, and requires DLPF 3,
±4 g and ±500 degrees/s settings. Recorded gesture flags are never training
inputs or labels. Source CSV, metadata and annotation hashes are recorded.

## Reproduce

From this directory, with Python, NumPy, scikit-learn and a C++11 compiler:

```powershell
python -m pip install -r requirements-training.txt
python pipeline.py --sessions ../../../sessions/ble --run-name ble_juven_val_ty_test
python pipeline.py --sessions ../../../sessions/ble --run-name ble_juven_val_ty_test --verify-only
```

The development machine uses `C:/Users/tzeyo/anaconda3/python.exe` and
`C:/MinGW/bin/g++.exe`. Supply `--compiler` if needed. Training exports the
selected model into the root `model_data.h`, and saves the header, pipeline
snapshot, manifest and reports under the named run. Verification checks the
currently active header against that run. Original datasets are never changed.

Candidates are a depth-6 tree and an eight-tree depth-6 forest, seed 16, minimum
20 samples per leaf, with balanced classes. Selection uses validation only:
among candidates within one percentage point of the best useful-sample recall,
retain gesture detection within one point of the best, then minimize active
background. Start and stop thresholds are evaluated together with the existing
segmenter's rearm/timeout behaviour. Test results never tune the model.

## Current results

The selected model is a 113-node decision tree, with start threshold 0.30 and
stop threshold 0.20. Its tree and thresholds reproduce the original BLE-only
run that also used Juven for validation and TY for testing.

| Metric | Validation: Juven | Test: TY |
| --- | ---: | ---: |
| Useful samples flagged active | 64.0% | 78.9% |
| Background samples flagged active | 30.8% | 44.1% |
| Gestures detected at least partly | 99.5% | 349/350 |

Test onset delay at the 95th percentile among detected gestures is 398 ms.
Detection of some part of a gesture is not complete retention: 21.1% of useful
test samples remain unflagged, and 172/350 gestures have at least 80% coverage.
The current segmenter's warmup/locks and approximate annotation boundaries
limit full retention. No retrospective flags or prebuffer are credited.

Python and C++ agree on every outgoing flag and segment event across all 33
included recordings: 657,147 samples and 127,911 decisions. Existing C++ checks
also cover int16 extremes, packet gaps, invalid status and clock wrap.

## Artifacts and prior experiments

### Dedicated-noise weighting comparison

The comparison in `training/noise_weight_comparison` tested 1×, 1.5×, 2× and 3×
sample weights for the 1,705 windows from the dedicated noise recording only.
Class balancing, source data, participant splits, seed, model sizes and threshold
grid stayed fixed. Both the tree and forest were evaluated at each weight.
The 1× run exactly reproduced the active model's trees, thresholds and validation
results. Higher weights were not evaluated on TY; only the validation-selected
winner received the final test evaluation.

| Noise weight | Juven useful-sample recall | Juven background flagged active |
| --- | ---: | ---: |
| 1× (retained) | 63.98% | 30.77% |
| 1.5× | 64.18% | 30.90% |
| 2× | 63.52% | 30.49% |
| 3× | 62.64% | 30.74% |

Acceptance required at least a 2-percentage-point background reduction, no more
than a 1-point overall recall/detection-rate loss, and no more than a 2-point
sample-recall loss for any individual gesture, relative to 1×. No higher weight
met these requirements. The active model/header remains unchanged, with the same
TY test results above. This is an empirical comparison on these recordings,
not evidence that noise weighting can never help with different data.

Reproduce with `python compare_noise_weights.py`. The comparison leaves the
active header unchanged and saves all candidate models, validation trials,
acceptance checks and the selected test report in its own output directory.
The existing C++ verification report was reused for the exactly reproduced 1×
winner; no firmware rebuild was needed because firmware contents did not change.

- `training/ble_juven_val_ty_test/`: current run and noise-source verification.
- Top-level `training/model.json`, `manifest.json`, `selection_report.json`,
  `test_report.json`, `cpp_verification.json`: copies of the active run's reports.
- `training/active_run.json`: active run selection and requested split.
- `training/firmware_build.json`: current full ESP32 build receipt.
- `training/unchanged_files.json` and `preservation_check.json`: source checks.
- `training/ble_comparison/` and `combined_500hz/`: historical comparison using
  Juven for validation and **Jermaine** for testing. Their results and commands
  are documented in `training/comparison_README.md`; they are not the active
  model. Do not compare those test percentages directly with the TY holdout.
- `training/ble_only_baseline/`: preserved original BLE run.
- `training/model_data.previous.h`: pre-retraining model requiring the old
  float/averaged frontend; it is not a drop-in rollback.

The optional 500 Hz loader still exists for future experiments: it selects the
fifth sample on a fixed five-sample grid, without averaging, while preserving
raw gaps and ignored intervals. It is disabled unless `--legacy-sessions` is
supplied. The current training run does not use it.

The sketch, public IMU interface and segmenter remain unchanged, including BLE
protocol and motor handling. Arduino builds use identical copies under
`training/build_sketch/firebeetle` to satisfy the sketch-name requirement, with
target `esp32:esp32:firebeetle32`. No board has been flashed. Offline checks do
not measure on-device execution time, BLE timing or motor interference; check
those using the existing timing logs after uploading.
