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
false-accept tests, and cached here as `background_v2_m200_<hash>.npz` (one per set of recordings;
the hash is of the `--sessions` paths relative to the repo, so the same on every clone). Without the
recordings, training uses the cache as is. So training takes its own `--sessions`, which can differ
from the export's: see [train/README.md](../train/README.md).

## Reproducing the deployed model from the release zip

The recordings are git-ignored, so a fresh clone can't rebuild `mage_noise_robust` or its
background. `AI_release.zip` (37 MB) holds everything the deployed model's results
depend on, with paths relative to `AI/`:

| In the zip | What it is |
|---|---|
| `data_collection/datasets/mage_noise_robust/` | The exported gestures and `index.csv` |
| `data_collection/datasets/background_v2_m200_4a064b05.npz` | The background cache for the default `--sessions`, which the noise class and `slide_bg` are cut from |
| `train/results/models/mage_noise_robust_s2.h5` | The trained model |
| `train/results/run_mage_noise_robust.json` | Its accuracy as recorded at training time |
| `train/results/test_windows_mage_noise_robust.npz` | The windows the HLS check in [hls4ml/README.md](../hls4ml/README.md) used |
| `ultra96/gesture_model.xsa` | The deployed overlay (the same file as in git) |
| `REPRODUCE.txt` | The commands below |

**1. Import it.** Clone the repo, then extract the zip into its `AI/` folder. From the repo root,
in PowerShell (`-Force` because `gesture_model.xsa` is already there):
```powershell
Expand-Archive AI_release.zip -DestinationPath AI -Force
```
or in Git Bash / WSL: `unzip -o AI_release.zip -d AI`. Nothing else needs moving:
training looks for datasets and the background in `data_collection/datasets/`, and the cache name
is the same on every clone, so it is used without the recordings.

**2. Build the venv** as in [Setup](../README.md#setup).

**3. Check the accuracy.** From `AI/`:
```powershell
.venv\Scripts\python -m train.export_test_windows --data mage_noise_robust --noise --model train/results/models/mage_noise_robust_s2.h5 --out train/results/test_windows_check.npz
```
"acc (as trained)" equals `run_mage_noise_robust.json`: jitter 0.9430, slide_bg 0.9673, noise
0.9635 (1 − `false_accept`).

**4. Check the converted model** (what `gesture_model.xsa` runs), in WSL as in
[hls4ml/README.md](../hls4ml/README.md), on `test_windows_check.npz` from step 3 or the zip's
`test_windows_mage_noise_robust.npz`. Output (3) gives the Keras and HLS accuracy per test set.
With step 3's windows they are equal on every set (0.9473 / 0.9673 / 0.9635 on the board's input
grid), and the generated `firmware/` is identical to the project the overlay was built from.

**Retraining** from the zip also works
(`python -m train.exp_new_vocab --noise --data mage_noise_robust --tag <new tag>`); expect figures
close to, not necessarily identical with, step 3.

**Making a zip for another model:** zip the same kinds of files with paths relative to `AI/`. Take
the background cache named by `train.data.background_cache_path(DataConfig(dir=<dataset>,
sessions_dir=<the training run's --sessions>))`. The `index.csv` files name the participants, so
check with them before publishing a zip.

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
