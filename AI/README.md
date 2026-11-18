# AI: the gesture classifier

AI turns a window of glove IMU samples (150 steps × 6 channels, 100 Hz) into one of 7 gestures or
`noise`, on the Ultra96's FPGA. This page is the whole pipeline in order: each stage has one
command here and a link to its details.

| Folder | What is in it |
|---|---|
| [`data_collection/`](data_collection/README.md) | Turns labelled recordings into a training dataset (`export_dataset.py`), holds the datasets, and zips one for sharing (`pack_dataset.py`) |
| [`train/`](train/README.md) | The model, augmentation, training, evaluation, and the test-window export for conversion |
| [`hls4ml/`](hls4ml/README.md) | Converts a trained `.h5` to a Vitis HLS project, in WSL, and checks it against training |
| [`ultra96/`](ultra96/README.md) | The classifier that runs on the board (`wk10_integration.py`), the deployed overlay (`gesture_model.xsa`), and the laptop Keras stand-in |
| [`../docs/results.md`](../docs/results.md) | Every model trained, and why the deployed one was chosen |

## The pipeline

Run every `python -m ...` command from this `AI/` folder, in the venv from [Setup](#setup).

**1. Record and label.** Record gestures with the glove over USB (500 Hz) or BLE (100 Hz), and mark
each attempt. Recordings land in `Hardware/Data Collection/sessions/`. See
[Hardware/Data Collection/README.md](../Hardware/Data%20Collection/README.md).

**2. Export a dataset.** Crop every labelled gesture of every reviewed recording into its own CSV:
```bash
python -m data_collection.export_dataset --out <dataset>
```
Details: [data_collection/README.md](data_collection/README.md).

**3. Train and evaluate.** Train 3 seeds with the 8th `noise` class, then score a saved model:
```bash
python -m train.exp_new_vocab --noise --data <dataset> --tag <tag>
python -m train.exp_eval --model train/results/models/<tag>_s1.h5 --data <dataset>
```
Details: [train/README.md](train/README.md).

**4. Export the test windows.** Save the exact windows the run was scored on, with the `.h5`'s
outputs, so the converted model can be checked window by window:
```bash
python -m train.export_test_windows --data <dataset> --noise \
    --model train/results/models/<tag>_s<seed>.h5 --out train/results/test_windows_<tag>.npz
```

**5. Convert to a bitstream (WSL).** Copy the `.h5` and the `.npz` into WSL, convert, synthesise
with Vitis HLS, and build the overlay in Vivado:
```bash
python convert.py --model <tag>_s<seed>.h5 --npz test_windows_<tag>.npz --out ~/<tag>
```
Details: [hls4ml/README.md](hls4ml/README.md).

**6. Deploy.** Copy the new `.xsa` to `ultra96/gesture_model.xsa`. Comms' `--sync` copies it and
`wk10_integration.py` to the board. Details: [ultra96/README.md](ultra96/README.md).

## The deployed model

`ultra96/gesture_model.xsa` is the `mage_noise_robust` run: 6-channel QCNN (8,744 parameters),
8 classes (`chop circle fireball jabs lightning star tank noise`, sent as
`warrior mage fireball rogue lightning hero shield` + no event), trained on 3,097 gestures from
8 participant IDs. Its numbers and how it compares are in [docs/results.md](../docs/results.md).
To reproduce its accuracy without the recordings, pack its dataset and background into one zip:
[data_collection/README.md](data_collection/README.md#sharing-a-dataset-reproducing-the-deployed-model-without-the-recordings).

## Other guides

- Testing an `.h5` live on the laptop before building a bitstream:
  [ultra96/README.md](ultra96/README.md#testing-a-model-on-the-laptop).
- The board's power and the end-to-end latency, both measured through Comms:
  [docs/power.md](../docs/power.md), [docs/latency.md](../docs/latency.md).

## Setup

Laptop (Windows, Python 3.12): everything under `data_collection/`, `train/` and the laptop side of
`ultra96/` (`keras_stream.py`, `wk10_integration.py --mock`) runs in one venv built from
[`requirements.txt`](requirements.txt), which pins the versions the deployed model was trained with.
Once, from this `AI/` folder:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Then either activate it (`.venv\Scripts\activate`) or run each command as
`.venv\Scripts\python -m ...`. `.venv/` is git-ignored. `train/__init__.py` sets
`TF_USE_LEGACY_KERAS=1` for QKeras. `pyparsing` is listed on its own because QKeras imports it
without declaring it.

Conversion (WSL Ubuntu 22.04): the `hls4ml-tutorial` conda environment, Vitis HLS and Vivado 2024.1.
