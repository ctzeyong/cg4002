# Recording and labelling gesture data

Gesture data is recorded with the glove in one of two ways, and labelled either while recording or
afterwards. Both write the same three files into `sessions/`, which AI's exporter turns into a
training dataset: see [AI/data_collection](../../AI/data_collection/README.md). The same 500 Hz USB
recordings also train the on-glove detector: see [The on-glove detector](#the-on-glove-detector).

| | USB (`record.py`) | BLE (`Comms/laptop/record_ble.py`) |
|---|---|---|
| Rate | 500 Hz, every reading | 100 Hz, the stream live inference sees |
| Firmware | `esp32_continuous/esp32_continuous.ino`, `RECORD_MODE=true` | The game firmware, `Comms/firebeetle/` |
| Link | USB cable, 921,600 baud | BLE, the same code as the game's relay |
| Saved to | `sessions/` (or `--out`) | `sessions/ble/`, auto-named, never overwritten (`_2`, `_3`) |
| Use for | AI training and the on-glove detector | AI training only |

Every recording is three files with the same name:

| File | Holds |
|---|---|
| `<name>.csv` | Every valid sample, background included: `sequence, esp_time_us, ax_g … gz_dps, host_rx_unix_ns` (BLE adds `imu_status, gesture_flag`). Units are g and °/s |
| `<name>.meta.json` | Participant, session, the variant, `raw_hz`, and diagnostics: gaps, malformed or late packets |
| `<name>.labels.json` | The labelled intervals (symbol or `ignore`, start/end in sensor µs, variant) and whether they were **reviewed**. The exporter skips unreviewed recordings |

## Wiring

| Part | Connection |
|---|---|
| MPU6050 | SDA → GPIO21, SCL → GPIO22, AD0 low (I2C address `0x68`), 3.3 V, GND. Mounted firmly on the back of the hand, in the same orientation every session |
| Vibration motor driver | PWM on GPIO2. GPIO2 is also a boot strapping pin: if the board won't flash or boot, disconnect the driver while flashing |

The IMU samples at 500 Hz with its digital low-pass filter at setting 3 (about 44 Hz accel, 42 Hz
gyro). Readings are int16 counts: 8,192 per g and 65.5 per °/s.

## Which firmware to flash

| Purpose | Sketch |
|---|---|
| Playing the game | `Comms/firebeetle/firebeetle.ino`: IMU, the trained detector, BLE and the motor. Set `PLAYER_ID` per glove. See the [team README](../../README.md#gloves-firebeetle-esp32) |
| Recording data over USB (500 Hz) | `esp32_continuous/esp32_continuous.ino` with `RECORD_MODE=true` |
| Recording data over BLE (100 Hz) | The game firmware above, with `Comms/laptop/record_ble.py` |

Both sketches carry the same detector code (`gesture_core.h`, `segments.h`). The trained model
(`model_data.h`) is only in `Comms/firebeetle/`; `esp32_continuous/` has an untrained placeholder,
which recording mode doesn't use.

## Setup

From this folder: `python -m pip install -r requirements.txt` (numpy, matplotlib, pyserial,
scikit-learn, opencv-python, pyqtgraph, PyQt6). OpenCV is only for video review and
pyqtgraph / PyQt6 only for the block annotator.

**For USB:** wire the glove as in [Wiring](#wiring). In the Arduino IDE,
open `esp32_continuous/esp32_continuous.ino`, check `constexpr bool RECORD_MODE=true;`, pick the
FireBeetle ESP32 board and its COM port, and upload. Then close the Serial Monitor, which would
otherwise hold the port. Recording mode keeps the motor (GPIO2) off and needs no trained model.

**For BLE:** flash the game firmware (`Comms/firebeetle/`) as in the
[team README](../../README.md#gloves-firebeetle-esp32), and set up the Comms venv. `record_ble.py`
also needs numpy: from `Comms/`, `.\.venv\Scripts\pip install numpy`. Run it **instead of** the game
pipeline, since a glove takes one BLE connection at a time.

## Record with live labels

Both recorders take the same arguments. Use one symbol and one variant per file:

```bat
:: USB, from Hardware/Data Collection
python record.py --port COM9 --participant p2 --session day3 --live-labels --symbol chop --drawing-mode air_front --direction cw --posture standing

:: BLE, from Comms/laptop
..\.venv\Scripts\python record_ble.py --node 1 --participant p2 --session day3 --live-labels --symbol chop --drawing-mode air_front --direction cw --posture standing
```

| Argument | Values |
|---|---|
| `--port` (USB) / `--node` (BLE) | The ESP32's COM port, from the Arduino IDE or Device Manager / the glove's `PLAYER_ID` |
| `--participant`, `--session` | IDs agreed with the team, such as `p2`, `day3` |
| `--symbol` | The game's `chop circle fireball jabs lightning star tank`, the old shapes `arc caret infinity triangle z`, or `noise` |
| `--drawing-mode` / `--direction` / `--posture` | `table` or `air_front` / `cw`, `ccw` or `na` / `seated` or `standing`. The defaults are table / na / seated, so set them every time |
| `--out` | Optional. Without it, the file is named from the arguments: `<participant>_<session>[_<symbol>_<mode>_<direction>_<posture>].csv` |
| `--notes` | Free text, saved in the meta file |

Keep the terminal focused; each control is one key tap, no Enter:

| Key | Action |
|---|---|
| **Space** | Start an attempt. Tap again right after the last stroke, before lowering the hand, to end it |
| **D** | Mark the open, or the last finished, attempt `ignore`. Still end an open attempt with Space |
| **Q** | Finish. Then answer `y` only if every attempt was marked, mistakes discarded and boundaries usable; Enter keeps the labels as a draft to repair |

For each attempt: position the hand, tap Space with the other hand, wait for `START`, make one
gesture, tap Space, wait for `END`. Between attempts, move normally: raising and lowering the arm,
swings, walking. That unlabelled time is the **background** that trains AI's `noise` class and the
detector's rejection, so leave it unlabelled, and never make an extra gesture without marking it.

The terminal keeps a running count (`Labelled gestures: 24; ignored attempts: 2`). Labels are saved
on every key and once a second; Ctrl+C stops and leaves them as a draft.

**Noise recordings.** To give the `noise` class harder examples, record random gestures with
`--symbol noise`: those intervals are treated as neither background nor a class, and only the time
between them is used.

## Record first, label afterwards

Leave out `--live-labels`. The symbol and variant still go into the meta file.

**Block annotator** (best for many repeats of one gesture with a ~1 s pause between). Record the
block, then:
```bat
python annotate_blocks.py sessions/ble/p2_day3_chop_air_front_cw_standing.csv
```
It proposes one interval per repeat from the pauses in the motion energy. Check the count in the
status bar against how many you did; if it's off, drag the red threshold line or change
**Merge gaps** / **Min length**. Step through with Left/Right; Tab jumps to intervals much shorter
or longer than the median (outlined red). Drag an edge to move a boundary, **Del** deletes, **M**
merges with the next, **I** marks ignore, **Shift+drag** draws a missed one, **Ctrl+Z** undoes.
**Ctrl+S** saves a draft and **Ctrl+Shift+S** saves reviewed labels.

**Plot and video annotator** (any recording, and for repairing live labels):
```bat
python annotate.py sessions/p2_day1_circle_table_cw.csv
python annotate.py sessions/p2_day1_circle_table_cw.csv --video sessions/p2_day1_circle_table_cw.mp4
```
Pick the symbol (or `ignore`) on the left, then **Mark start** / **Mark end** at the cursor, or drag
across the plot. Start is when the stroke begins, after positioning the hand; end is just after the
last stroke. **Delete at cursor** and **Undo last** fix mistakes. Use **Save draft** while working
and **Save reviewed labels** after checking the whole recording. Intervals can't overlap, and a
symbol interval can't cross a sensor gap (mark that area `ignore`). A redrawn interval keeps its
symbol but not the variant fields. Closing with unsaved changes writes a separate
`.labels.autosave.json`, never used for training until you copy it over and review it.

To label from video, record the hand on a phone at the same time and make three distinct
synchronisation movements near the start and the end. In the annotator, with **Link timelines**
off, find the same movement in the video and the plot and **Add sync point**; add a second one near
the end to correct drift; then turn **Link timelines** on and check the alignment. Sync is manual,
assumes a constant frame rate, and a 30 fps video only resolves about 33 ms.

## Checks before handing data over

- Each recording has its `.csv`, `.meta.json` and a `.labels.json` that says `"reviewed": true`.
- Every real gesture is labelled or `ignore`; normal movement is left unlabelled.
- A short practice recording showed no ongoing gaps, malformed rows, restarts or clipping. If it
  did, fix the cable, port, wiring or mounting first.
- Filenames and IDs are unique, and a new file was started after any ESP32 reset or reconnect.
- No recording was edited by hand.

## Troubleshooting

- **USB recording stops with "possible reboot or serial corruption".** The sequence counter went
  backwards or repeated. Saved rows and the reason stay on disk; start a new file. Text over USB has
  no checksum, so these checks can't catch every corrupted value.
- **BLE gaps.** A lost packet or bad IMU reading leaves a gap (timing comes from the firmware's
  sequence count, 10 ms per sample), and the exporter never crops a gesture across one.
- **Windows won't replace the labels file.** The recorder keeps the latest draft in
  `.labels.json.tmp` and retries. If it is still blocked at exit, keep both files, close whatever
  holds the file, and copy the temporary one over before reviewing.
- **A file literally named `.csv`.** Give `--out` a full filename, not a folder followed by `/.csv`.

The detailed USB walkthrough written for the first collection days is in
[esp32_continuous/README.md](esp32_continuous/README.md).

## The on-glove detector

The ESP32 doesn't classify gestures. It decides **gesture-like motion vs background**, to mark each
gesture's start and end (the end-of-gesture flag that tells the Ultra96 to classify).

**How it works.** The firmware keeps 500 Hz readings in a 4,096-reading RAM ring (8.192 s) and
averages groups of five to 100 Hz. Every 50 ms it computes 42 motion features from the last 30
averages (300 ms), including three 100 ms section means, and runs a small decision-tree forest on
them. A gesture starts when the score passes the start threshold, with up to 0.3 s of pre-trigger
context. It ends after the motion energy (last 50 ms) drops to 20% of its recent peak for 150 ms,
or the score stays below the stop threshold for 200 ms. Captures must be 0.25–6 s; gaps,
unfinished captures and timeouts don't produce a gesture. No stillness is required, but a long
pause inside a gesture can split it and unclear boundaries can merge two.

**The current model** (`Comms/firebeetle/model_data.h`, id `342c4355152c26f4`) is provisional: an
eight-tree depth-four forest, trained on days 1 and 2 and validated on day 3, start threshold 0.5,
stop threshold 0.2, stop hold 200 ms. It retained 87.72% of gesture samples and 29.08% of
background, with 69.12% packet precision and 67.14% packet recall, short of the validation targets
below. Its `model.json`, `gate.json`, validation report and notes are in
[`../ESP model training result/`](../ESP%20model%20training%20result/PROVISIONAL_MODEL.md), and the
recordings it was trained on (p1, days 1–3) in `../Dataset for ESP model/`.

### Training the detector

From this folder, after [Setup](#setup). It uses labelled 500 Hz USB recordings, the same kind AI
uses. Labelled symbols are "useful motion" (all classes combined), unlabelled time is background,
and `ignore` intervals are left out.

**1. Write a manifest** that puts each recording in one split. Copy `manifest.example.json` to
`manifest.json` (`manifest.day1.json` is a real one):
```json
{"sessions": [
  {"csv": "../Dataset for ESP model/p1_day1_arc_air_cw.csv", "group": "p1_day1", "split": "train"},
  {"csv": "../Dataset for ESP model/p1_day2_star_air_cw.csv", "group": "p1_day2", "split": "validation"},
  {"csv": "../Dataset for ESP model/p1_day3_arc_air_cw.csv", "group": "p1_day3", "split": "test"}
]}
```
Paths are relative to the manifest. Keep one session or day in one split, and group by participant
to test on unseen players. Training needs symbols and background in both train and validation.

**2. Train, select, evaluate and export:**
```bat
python pipeline.py train --manifest manifest.json
python pipeline.py select --manifest manifest.json
python pipeline.py evaluate --manifest manifest.json --model artifacts/selected/model.json --config artifacts/selected/gate.json --out artifacts/test_report.json
python pipeline.py export --model artifacts/selected/model.json --config artifacts/selected/gate.json --out ../../Comms/firebeetle/model_data.h
```

1. `train` builds a depth-four tree and an eight-tree depth-four forest in `artifacts/candidates/`,
   from the training split only.
2. `select` checks the candidates and thresholds on the validation split and keeps the qualifying
   one with the least background retained (fewer trees breaks ties), in `artifacts/selected/`. If
   none qualifies, collect more data rather than exporting an unqualified model.
3. `evaluate` reports on the held-out test split: missed symbols, wrong boundaries, background kept.
   Don't tune on the test split.
4. `export` writes the model as C++ for the ESP32, straight into the game firmware.

The validation targets (`select`'s defaults): at least 98% of gesture samples retained; at least
95% of symbols keep at least 95% of their samples, per class too; at most 60% of background
retained; candidate precision and recall at least 90% at interval IoU ≥ 0.5; no merged symbols.

**3. Deploy.** Flash the game firmware. The serial monitor prints `[IMU] gesture model <id>` at
boot. Copy `artifacts/selected/model.json`, `gate.json` and the reports into
`../ESP model training result/` so the record matches the firmware.

**Detector crops (optional).** `pipeline.py prepare --source detector --model ... --config ...`
replays recordings through the trained detector and writes each candidate as a 100-step CSV with
its real boundaries and pre-trigger context, to check how a classifier copes with gameplay-like
crops. AI's training data comes from `AI/data_collection` instead.

The Python detector (`core.py`, `segments.py`) mirrors the C++ one (`gesture_core.h`, `segments.h`):
the features, energy and thresholds must stay identical for a model trained here to behave the
same on the glove, so change both together.
