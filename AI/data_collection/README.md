# Exporting a dataset

`export_dataset.py` turns labelled recordings into a training dataset: one CSV per gesture. Making
the recordings (USB or BLE) and labelling them is in
[Hardware/Data Collection/README.md](../../Hardware/Data%20Collection/README.md).

Run from `AI/`:
```bash
python -m data_collection.export_dataset --out new_gestures_all --sessions "../Hardware/Data Collection/sessions/ble/*.csv"
```

| Flag | Meaning |
|---|---|
| `--out NAME` | Output folder. A bare name goes under `datasets/`; an absolute path is used as is. Must be new or empty, so nothing stale mixes in. |
| `--sessions A [B ...]` | Which recordings to read (default `Hardware/Data Collection/sessions`, subfolders included). Each entry is a folder (with subfolders), one recording (`.csv` or `.labels.json`), or a wildcard pattern. `".../ble/*.csv"` takes only that folder's top level. A file named twice counts once. |

Quote paths with spaces. Python expands `*` itself, so quoted patterns work in PowerShell too.

## What it does

For every **reviewed** recording, each labelled gesture interval is cropped and written as one CSV:

- **Rate.** Brought to 100 Hz, the rate live inference sees. The rate comes from each recording's
  `.meta.json` `raw_hz`:
  - 500 Hz USB (`record.py`): every 5th sample, processing id `decimate-500to100-v1`.
  - 100 Hz BLE (`Comms/laptop/record_ble.py`): every sample, processing id `ble-100hz-v1`.
- **Encoding** (`imu-counts-int16-v1`): accel × 8192 counts/g, gyro × 65.5 counts/(°/s), rounded half
  away from zero, clamped to int16. Variable length: no resampling.
- **Not exported:** `ignore` intervals, and `noise` intervals (in the noise recordings those mark
  random gestures of the 7 classes, so they are neither background nor a class). Unreviewed
  recordings and crops across a sensor gap go to `skipped.json`.

Each dataset folder holds the gesture CSVs plus `index.csv` / `index.json` (symbol, participant,
session, source recording, boundaries, encoding) and `skipped.json`.

The **background** is not part of a dataset. It is the unlabelled time of the same recordings, read
by `train.data.load_background` for the window filler, the `noise` class and the `slide_bg` /
false-accept tests, and cached here as `background_v2_m200_<hash>.npz` (one per set of recordings).
So training takes its own `--sessions`, which can differ from the export's: see
[train/README.md](../train/README.md).

## Where the datasets live

`datasets/`, which git ignores, except `cloud_manual_day1_raw100hz_int16` (the old vocabulary, kept
so `train.exp_ablation` and `train.exp_final` run from a fresh clone). Training finds a dataset by
its bare name under `DATA_ROOT` (`train/config.py`); the `GESTURE_DATA_ROOT` environment variable
points it elsewhere without editing code.

| Dataset | Gestures | Classes (count) | Participants |
|---|---|---|---|
| `cloud_manual_day1_raw100hz_int16` | 1,466 | arc 212, caret 212, circle 212, infinity 195, star 212, triangle 212, z 211 | p1 (USB, day 1) |
| `new_gesture_100hz` | 1,089 | chop 115, circle 237, fireball 115, jabs 115, lightning 115, star 277, tank 115 | p1, p2 |
| `new_gesture_100hz_v2` | 1,189 | as above, chop 215 | p1, p2 |
| `shapes_yuwen` | 809 | arc, caret, circle, infinity, lightning, triangle, z 100 each; star 109 | yuwen (BLE) |
| `shapes_combined` | 2,425 | arc 322, caret 322, circle 322, infinity 305, lightning 100, star 371, triangle 362, z 321 | p1, yuwen |
| `new_gestures_w_john` | 1,414 | chop 266, circle 288, fireball 115, jabs 166, lightning 115, star 298, tank 166 | p1, p2, john |
| `new_gestures_all` | 2,887 | chop 469, circle 540, fireball 268, jabs 355, lightning 368, star 565, tank 322 | 8 IDs |
| `new_gestures_all_v2` | 2,947 | as above, star 575, tank 372 | 8 IDs |
| `new_gestures_all_jabs` | 2,997 | as above, jabs 405 | 8 IDs |
| `mage_noise_robust` | 3,097 | as above, circle 640 | 8 IDs (the deployed model) |

The 8 participant IDs are p1, p2, brandon, jermaine, john, juven, ty and yuwen. Only
`cloud_manual_day1_raw100hz_int16` is in git; the others are rebuilt from the recordings with the
command above.
