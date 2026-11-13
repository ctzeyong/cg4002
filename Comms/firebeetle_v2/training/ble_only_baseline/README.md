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
python pipeline.py --sessions ../../../sessions/ble
python pipeline.py --sessions ../../../sessions/ble --verify-only
```

Use `--compiler` to supply a compiler path. On the development machine, Python
was available at `C:/Users/tzeyo/anaconda3/python.exe` and g++ at
`C:/MinGW/bin/g++.exe`. All output is confined to this directory. Session files
are read-only inputs. A training run replaces `model_data.h` and its reports;
the original model is preserved at `training/model_data.previous.h` for reference
(it requires the old float/averaged frontend and is **not** a drop-in rollback).

Only reviewed BLE recordings with compatible settings are accepted. The loader
reconstructs counts as `round(acceleration * 8192)` and
`round(gyroscope * 65.5)`, verifies reconstruction within 0.001 count and int16
bounds, and rejects incompatible/nonmonotonic data. Recorded `gesture_flag`
values are never used as labels or features.

Useful labels: circle, star, lightning, fireball, tank, chop, jabs. Noise and
unlabelled samples are negative. Ignore intervals reset replay and feature
history and are excluded from metrics. No feature window spans an ignore
interval, an invalid reading or a missing packet. Unreviewed/empty recordings
are excluded and recorded in `training/manifest.json`.

The participant split is explicit in `pipeline.py`:

| Split | Participants |
| --- | --- |
| Training | Jermaine, Brandon, Yuwen |
| Validation / threshold selection | Juven |
| Test, never fitted or used to select | TY |

New participants require an explicit split assignment. Every split must contain
all seven useful gestures and background. Source CSV, metadata and annotation
hashes are recorded. Dedicated noise recordings are available only from Yuwen;
held-out background consists of unlabelled motion in gesture recordings.
Another participant's dedicated non-gesture-motion session would strengthen
validation of false positives.

Candidates are a depth-6 tree and an eight-tree depth-6 forest, with balanced
classes, minimum 20 samples per leaf and seed 16. Threshold selection measures
the complete state machine's outgoing flags. It prioritizes useful-sample
recall: among candidates within one percentage point of the best recall,
retain gesture detection within one point of the best, then minimize active
background. This explicitly favours uncertain useful motion. Thresholds are
selected using validation only. Reports retain all candidate results.

## Reports and limitations

The current export is one 113-node depth-6 decision tree, with start threshold
0.30 and stop threshold 0.20. It was trained and checked on 33 reviewed recordings
(657,147 rows total), with the participant split above.

| Outgoing-flag metric | Validation (Juven) | Test (TY) |
| --- | ---: | ---: |
| Useful samples flagged active | 64.0% | 78.9% |
| Gestures with at least one active useful sample | 99.5% | 99.7% (349/350) |
| Background samples flagged active | 30.8% | 44.1% |

These figures do **not** mean complete gesture retention: 21.1% of useful test
samples remain unflagged, and validation retention is lower. Only 172/350 test
gestures had at least 80% of their useful samples flagged. Fireball, tank, chop
and jabs had weaker sample coverage than the longer shapes. The test 95th
percentile onset delay among detected gestures was 398 ms. The existing
segmentation locks and warmup can suppress useful samples despite a permissive
classifier. Improvements to complete gesture retention may require a separate
review of those segmentation rules or protocol buffering.

The C++ replay matched Python flags and segment events on all 657,147 rows and
127,911 decisions, with zero mismatches. This includes invalid readings and
excluded annotations as resets. Synthetic checks cover constant and alternating
int16 extremes, missing samples, invalid status, streaming pauses and clock wrap.

- `training/model.json`: selected trees, thresholds and training provenance.
- `training/selection_report.json`: validation results for every candidate.
- `training/test_report.json`: held-out results, including per-gesture recall.
- `training/cpp_verification.json`: Python/C++ feature, energy, prediction,
  event and outgoing-flag comparison over all included recordings.
- `training/unchanged_files.json`: pre-change hashes of the sketch, public IMU
  interface and segmentation code.
- `training/environment.json`: versions used for training and compilation.
- `training/firmware_build.json`: successful FireBeetle-ESP32 build receipt.

The complete firmware compiled successfully with Arduino CLI 1.5.1, ESP32 core
3.3.12 and NimBLE-Arduino 2.5.2, targeting `esp32:esp32:firebeetle32`: 633,875
bytes of program storage (48%) and 38,400 bytes of global RAM (11%). Arduino
requires the sketch directory to match the main `.ino` name, so the build used
an identical source copy in `training/build_sketch/firebeetle`. Every copied
firmware source was checked against the current files. The board was not flashed.

Useful-sample recall measures how much annotated useful motion is actually
flagged active. Gesture detection means **at least one** useful sample was
flagged; it does not imply complete coverage. Coverage of 80% of a gesture is
reported separately. Onset delay is measured only for detected gestures; missed
gestures remain in detection/recall denominators. Background starts are starts
outside useful annotations, and may include annotation-boundary uncertainty.
BLE keyboard annotations have uncorrected latency, so exact boundaries are
approximate. Evaluation never credits retroactive flagging or a prebuffer: the
current protocol cannot change already-transmitted flags.

Offline verification does not establish ESP32 execution latency, BLE scheduling,
motor interference or live recognition quality. Validate these on the device
using its existing timing/statistics logs after flashing.

## Older 500 Hz recordings

They are deliberately not included in this training run. A future loader can
select every fifth raw sample (not average), preserve the selected timestamps
and annotations, and reconstruct counts using matching sensor settings. All
derived variants must share the original recording's split. This approximates
nominal BLE selection but cannot recreate its actual scheduling or losses.
