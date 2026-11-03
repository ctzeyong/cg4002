# Collect, label and prepare gesture data

**Start here:** use live manual labelling below to mark each symbol while recording. Video and later annotation are optional ways to check or repair boundaries. You do not need a trained ESP detector to collect data or turn manually labelled symbols into cloud training examples.

## Quick start: record a block, label afterwards

Record one gesture many times with a short still pause (about 1 s) between repeats. Leave out `--live-labels`; the symbol and variant are still saved to the meta file:

```bat
cd ..\..\Comms\laptop
python record_ble.py --node 1 --participant p2 --session day3 --symbol chop --drawing-mode air_front --direction cw --posture standing
```

Then open the recording in the block annotator. It needs `pip install pyqtgraph PyQt6`.

```bat
python annotate_blocks.py sessions/ble/p2_day3_chop_air_front_cw_standing.csv
```

The annotator proposes one interval per repeat, using the pauses in the motion-energy trace. Review the proposals:

1. Check the count in the status bar against the number of repeats you did. Too many or too few? Drag the red threshold line on the energy plot, or change **Merge gaps** / **Min length**. Detection re-runs until you edit an interval by hand.
2. Step through with **Left/Right**. **Tab** jumps to intervals outlined in red, which are much shorter or longer than the median.
3. Fix intervals: drag an edge to move a boundary. **Del** deletes the selected interval, **M** merges it with the next one, **I** marks it ignore, and **Shift+drag** draws a missed one.
4. **Ctrl+Shift+S** saves reviewed labels (**Ctrl+S** saves a draft). The tool writes the same `.labels.json` that `export_dataset` reads.

Mouse wheel zooms the time axis and dragging pans. The strip at the bottom shows the whole recording; drag its blue box to move around. **Ctrl+Z** undoes.

## Quick start: label while recording (Windows)

Complete setup in section 1, then run this in Anaconda Prompt or a Windows terminal, from this directory:

```bat
python record.py --port COM9 --out sessions/p1_day1_circle_table_cw.csv --participant p1 --session day1 --live-labels --symbol circle --drawing-mode table --direction cw --posture seated
```

1. Keep the recorder terminal focused. Wait until valid samples arrive. The symbol and variant remain selected for this entire file.
2. Position the drawing hand. With your **other hand**, tap and release **Space**. Wait for `START`, then draw exactly one symbol.
3. Tap **Space** again immediately after the final stroke, before lowering/repositioning the hand. Wait for `END`. The recorded interval includes key timing error; it is not automatic stroke trimming.
4. If an attempt went wrong, tap **D**. During an open attempt, this marks it ignore; **still press Space when that attempt finishes**. After an attempt ends, D marks the most recent attempt ignore. Repeated D presses do not discard earlier attempts.
5. Repeat. Between attempts, include ordinary arm raising/lowering, swings, walking and other unwanted motion. This unlabelled time becomes background. **Mark every actual symbol attempt or mark it ignore**; do not draw extra unmarked symbols.
6. Tap **Q** to finish. Any open attempt becomes ignore. Answer `y` only if you marked every attempt, discarded mistakes, and the boundaries are usable. This confirms the recording without requiring a video review. Otherwise press Enter to leave a draft and repair it with `python annotate.py sessions/p1_day1_circle_table_cw.csv`.
7. Add the CSV to the manifest in section 5, then run section 6 to produce the 100-step cloud CSV examples. Section 7 trains the separate ESP useful/background detector; it is **not required** for manual-label cloud export.

Controls are single key presses: **no Enter needed**, except for the final confirmation. Tap and release; holding a key may repeat. Ctrl+C stops collection but leaves labels as an unreviewed draft. After a serial error, saved data and labels also remain a draft. Gap-crossing open attempts automatically become ignore; close the attempt normally with Space before starting another.

The terminal shows a running count after Start/End/Discard, every 5,000 raw samples, and when recording ends. For example: `Labelled gestures: 24; ignored attempts: 2; in progress: none`. This counts completed symbol examples in the **current recording**, not individual sensor readings. An open attempt is not counted yet; discarding a completed gesture reduces the labelled count. Ignored attempts are listed separately. The count does not replace your final label confirmation.

| Saved file | Purpose |
|---|---|
| `...csv` | Every received valid raw sample at nominal 500 Hz, including background |
| `...meta.json` | Recording settings, participant/session and serial diagnostics |
| `...labels.json` | Start/end sensor timestamps, symbol, drawing mode, direction, posture, ignore intervals and confirmation status |

Labels are saved on controls and once per second; an open attempt is stored as unfinished/ignore until closed. An abrupt process/power failure can lose the latest buffered data or annotations: review the surviving recording before use. Nothing is stored persistently on the ESP. The raw CSV is not replaced by labels or resampled data.

Allowed symbols: `circle`, `arc`, `infinity`, `caret`, `z`, `triangle`, `star`. Drawing mode: `table` or `air_front`; direction: `cw`, `ccw` or `na`; posture: `seated` or `standing`. Defaults are table/na/seated, so set these explicitly for each recording. Use a consistent viewing direction to define clockwise. Start a new filename when changing symbol/variant. A seated air drawing is `--drawing-mode air_front --posture seated`; posture and drawing surface are separate attributes. Filenames alone are not parsed into labels.

Cloud `index.csv` and `index.json` retain these variant fields alongside the symbol and example filename. Each example CSV still contains only step/time and six sensor channels. The ESP trains on gesture versus background, not variant classes. Older labels without variants export blank variant fields. The existing plot annotator preserves attributes on untouched intervals; deleting/redrawing an interval does not automatically restore its variants.

**Timing trade-off:** controls use the latest received sensor timestamp (the next 2 ms boundary), not a synchronized physical button on the hand. USB buffering and key timing can shift boundaries; there is no USB-latency correction or automatic trimming. If the stream falls behind, do not confirm the recording as usable. Check a short practice recording in the plot first and occasionally spot-check later recordings; a helper can press keys for standing gestures. Keep gaps short between pressing Start and drawing. This is quicker than full video review, but less precise for training start/end detection. Live labelling does not fix corrupted serial data.

Keep all files from the same collection day/mounting in one manifest **group** and one split, even if there is a separate file for every symbol/variant. Repeat variants on independent days for validation/test.

| Stage | Input → output | Where it runs |
|---|---|---|
| Collection | Hand motion → full-rate raw CSV + optional live labels/video | ESP over USB to laptop |
| Annotation | Live confirmation, or raw CSV + optional video → reviewed symbol/ignore intervals | Recorder or laptop annotation tool |
| Cloud dataset preparation | Manually labelled intervals → 100-step CSV examples | Laptop, no detector required |
| ESP detector development | Reviewed recordings → useful/background model and reports | Laptop; exported model later runs on ESP |
| Gameplay (later) | Detected candidate → 100×6 array → teammate's Bluetooth transport | ESP → laptop/cloud |

**500 Hz is the raw recording rate (one reading every 2 ms). 100 steps is the number of output rows across one whole gesture; it is not a sampling rate.** No Bluetooth sending code is included.

### Updating from the 1,000 Hz recorder

Upload the updated recording sketch with `RECORD_MODE=true` before running the new recorder. The firmware now reports `READY,gesture-packet-v4,raw_hz=500,...` and uses `RAW500` rows. Record to a **new filename**, for example `sessions/practice2.csv`; keep `practice1.csv` unchanged. Older 1,000 Hz recordings/models are rejected by the new annotation/training/export pipeline. Collect new 500 Hz examples and retrain before gameplay.

The rate change halves the number of text rows sent over USB. It should reduce transfer load, but a live recording is still needed to check your cable/adapter/driver. Start with a short practice session and inspect gaps/malformed counts before collecting a large dataset. GPIO2 stays LOW during recording.

## 1. Set up the recorder and laptop

1. Mount one MPU6050 firmly on the **back of the hand**, with consistent orientation between sessions. On the DFR0478 FireBeetle ESP32: SDA=GPIO21, SCL=GPIO22, AD0 low/address `0x68`, 3.3 V supply.
2. Open `esp32_continuous/esp32_continuous.ino` in Arduino IDE. Select the FireBeetle ESP32 board and the connected serial port. Leave **`RECORD_MODE=true`**, then upload. This mode drives the motor PWM control on **GPIO2 LOW (0% duty)** for the whole recording and does not require a trained model.
3. Close Arduino Serial Monitor so the Python recorder can use the port.
4. Open an Anaconda Prompt or terminal with Python available. Change into **this directory**, wherever the project currently lives. For the current location:

```bat
cd /d "C:\Users\tzeyo\Documents\TzeYong\NUS\Y4S1\CG4002_ComputerEngineeringCapstone\HW\Continuous Gestures"
python -m pip install -r requirements.txt
```

The `cd /d` example is for Command Prompt/Anaconda Prompt; in PowerShell use `Set-Location "path"`. Use a Python installation with an interactive Matplotlib backend (standard desktop Python/Anaconda). OpenCV is needed only for video viewing. A terminal reporting that `python` is unavailable needs its Python environment activated first.

## 2. Alternative: record now and label afterward

Skip sections 2–4 if your live labels are complete and confirmed. Use these sections for optional video checks or fixing mistakes.

Run, replacing COM9 with your board's port:

```bat
python record.py --port COM9 --out sessions/p1_day1.csv --participant p1 --session day1 --notes "table and standing air drawings"
```

1. Start a separate camera/phone video showing the hand clearly. Save/copy it as `sessions/p1_day1.mp4`. Camera capture is **not** built into this software.
2. While both recordings are running, make **three distinct hand movements separated by short pauses**. These should be clearly visible in the video and sensor plot. They are synchronization markers, not symbols.
3. Draw **circle, arc, infinity, caret, Z, triangle and star**, one at a time. Briefly slow/stop between symbols; complete stillness is unnecessary. Vary speed, size, table/air drawing and comfortable hand positions.
4. Include normal background: raising/lowering the hand, arm swings, walking, squatting/standing, front-to-back circles and abandoned strokes. Do not filter these out while recording. Ordinary unwanted movement is valuable negative training data.
5. Repeat the synchronization movements near the end, then stop the video and press **Ctrl+C** in the recorder terminal.

Use several sessions/days and background-only recordings. Initially aim for 50–100 examples per symbol across sessions, then improve coverage based on validation failures; this is not an accuracy guarantee. Record short manageable sessions initially so you can check video visibility and sensor clipping before collecting more.

The laptop stores:

| File | Contents |
|---|---|
| `sessions/p1_day1.csv` | All six channels at nominal 500 Hz, sequence, ESP timestamp and laptop arrival time |
| `sessions/p1_day1.meta.json` | Participant/session, sampling settings, notes and gap counts |
| `sessions/p1_day1.mp4` | Your separately recorded/copied video |

CSV units are acceleration in g and rotation in degrees/second; USB sends integer counts at 921600 baud and the recorder converts them. Raw data remains unchanged during annotation/export. Use a new filename/session after each ESP reboot. Investigate reported gaps/clipping before continuing; do not silently change sensor settings. No persistent raw recording is written to ESP storage.

Damaged/unrecognized text is counted as malformed and skipped, with up to ten examples saved in the metadata. Missing rows produce a gap warning. A repeated/backwards counter stops the recording with **possible reboot or serial corruption**, the previous values and the offending row; it does not prove a reboot. A new startup banner after data is reported separately as a likely restart. Inconsistent forward sequence/timestamp changes also stop recording unless the ESP explicitly reported a sensor gap. Saved rows and the stop reason remain on disk. Text transfer has no checksum, so these checks cannot detect every corrupted sensor value.

## 3. Align the video and sensor plot

```bat
python annotate.py sessions/p1_day1.csv --video sessions/p1_day1.mp4
```

The window shows acceleration/gyro plots, a video panel, and separate **Sensor s** / **Video s** sliders. Sensor seconds start at the first saved reading. Without `--video`, the same tool works as a plot-only annotator.

1. Leave **Link timelines unchecked** initially. Find an identifiable synchronization movement near the beginning in the video. Use its slider and **Frame < / Frame >** for fine adjustment.
2. Click the matching event in the sensor plot, or move the sensor slider. Zoom using the plot toolbar if needed; turn off zoom/pan afterward to click/label. A click positions the cursor; dragging creates an annotation.
3. Click **Add sync point**. This matches the current video frame to the current sensor cursor. One point estimates a constant offset between clocks.
4. Preferably repeat for a matching event near the end while still unlinked. A second point estimates a linear timing drift as well as offset. It must be later in both timelines. Use **Reset sync** to replace incorrect points.
5. Check **Link timelines**, then seek/play and check several movements across the recording. The video and sensor cursor now move together. Save the alignment using **Save draft** or **Save reviewed labels**.

Synchronization is **manual**, not automatic movement recognition. The mapping is `video seconds = offset + rate × sensor seconds`; two points correct steady drift, not dropped/irregular video frames. Frame seeking assumes a constant frame rate. If a phone variable-frame-rate video will not align consistently, convert it to constant frame rate with a video editor and re-align, or mark uncertain intervals ignore. A 30 fps video only gives about 33 ms visual timing resolution, regardless of the 2 ms sensor sampling interval. Audio is not played. If the video is moved/renamed, load the new path and re-establish alignment.

## 4. Label actual drawing boundaries

Watch the aligned video rather than guessing intent from raw peaks. **Start** is when tracing the symbol begins, after positioning the hand. **End** is after its final stroke, before lowering/repositioning. Do not include the 0.3 s pretrigger buffer in manual labels.

1. Pause at the symbol start and choose its name in the left panel.
2. Click **Mark start**.
3. Seek/frame-step to its end and click **Mark end**. Alternatively, drag its interval directly on either plot with toolbar zoom/pan off.
4. Repeat through the entire recording. Labels use an inclusive start and exclusive end. **Undo last** removes the last added interval. **Delete at cursor** removes the interval containing the sensor cursor so you can redraw it.
5. Use **Save draft** while working. After reviewing the **whole recording**, click **Save reviewed labels**. Training/export reject unreviewed drafts.

| Annotation | Meaning | ESP training | Cloud symbol export |
|---|---|---|---|
| Seven symbol names | Actual completed drawings | Useful motion (all seven combined) | Preserve the individual symbol name |
| Unlabelled time | Known ordinary/background movement | Not useful motion | No manual symbol example |
| `ignore` | Uncertain intent/boundaries, obscured video or unusable recording | Excluded, including feature windows touching it | Excluded |

**Do not mark ordinary arm swings ignore**: leave them as background so the ESP learns to reject them. Intervals cannot overlap. For a wholly uncertain attempt, mark the entire attempt ignore. Symbol labels cannot cross sensor gaps; ignore intervals may cover gaps. A background-only recording is valid after reviewing/saving it with zero symbol intervals.

Annotations and synchronization are saved to `sessions/p1_day1.labels.json`; the raw CSV is never rewritten. Closing with unsaved changes writes a separate `p1_day1.labels.autosave.json` draft. To recover it, preserve the current labels file, copy the autosave to `p1_day1.labels.json`, reopen and review. Autosaves are not silently used as training labels.

## 5. Create a manifest to keep datasets separate

Copy `manifest.example.json` to `manifest.json`, then replace its session filenames with your recordings:

```json
{"sessions": [
  {"csv": "sessions/p1_day1.csv", "group": "p1_day1", "split": "train"},
  {"csv": "sessions/p1_day2.csv", "group": "p1_day2", "split": "validation"},
  {"csv": "sessions/p1_day3.csv", "group": "p1_day3", "split": "test"}
]}
```

Paths are relative to the manifest. Keep recordings from one session/day in one split; for unseen-player testing, group by participant. Never distribute clips from the same recording across splits. Training learns from `train`, `validation` selects settings, and `test` provides the final untouched check. You can start manual CSV export with just one reviewed training session; training the ESP later requires all seven symbols and background in the training and validation sets. Old 200 Hz and 1,000 Hz recordings/models are incompatible with the current `gesture-packet-v4`, 500 Hz, DLPF=3 pipeline.

## 6. Export manually labelled symbols for cloud training

**This is the raw-to-100-step conversion stage. No ESP model is needed.**

```bat
python pipeline.py prepare --manifest manifest.json --source labels --split train --steps 100 --out artifacts/cloud_manual_train
```

Repeat for `--split validation` and `--split test`, using separate output folders. Outputs must be new/empty so stale examples cannot be mixed in.

Each labelled symbol is cropped to its actual available readings and resampled into a CSV with **100 data rows**, plus a header:

```text
step,time_from_start_s,ax_counts,ay_counts,az_counts,gx_counts,gy_counts,gz_counts
```

The six sensor columns are signed **int16 counts**, forming the model's **100×6 input**. CSV stores integer text; `--format npz` stores an actual int16 `frames` array. Step/time columns are metadata, not model inputs. Step is zero-based; time is relative to the first retained sample. `index.csv` contains each example's symbol, participant/session/group/split, source CSV, duration, boundaries and preprocessing ID. `index.json` repeats the metadata for scripts; `skipped.json` explains unusable crops. Open the CSVs in Excel; a native `.xlsx` workbook is not necessary. Optional `--format npz` retains array export for Python users.

Manual examples contain only the labelled stroke, with **no added pretrigger context**. They are useful initial cloud training data but cleaner than gameplay. Later use detector-generated crops to train/check tolerance to actual context and imperfect boundaries.

### How 500 Hz data becomes 100 steps

Apply a **Hann-windowed sinc low-pass filter**, then evaluate six channels at 100 evenly spaced times from the first retained sample to the last. Filtering limits aliasing before reducing sample count. For downsampling, the cutoff is 90% of the output Nyquist frequency; edges replicate endpoint readings. The shared Python/ESP processing ID is `hann-sinc4-v1`.

For 1,000 input readings at 500 Hz, the first-to-last span is 1.998 s, so 100 rows are about **20.18 ms apart**. This is **lossy temporal resampling**: keep the full-rate CSV for future reprocessing. After resampling, acceleration is multiplied by 8192 counts/g and gyro by 65.5 counts/(degree/s), using float64 for scaling. Values are rounded to nearest with ties away from zero, clamped to [-32768, 32767], then cast to int16. This encoding is `imu-counts-int16-v1`; it does not amplitude-normalize the gesture. The index records encoding, scales, rounding and the number of saturated values (`clipped_values`). These are resampled counts, not untouched raw samples. Cloud training must use the same decoding/normalization as inference. The existing ESP capture still produces floats internally; the transport integration must apply this quantization before sending binary int16 (see STREAMING_SPEC.md). Original duration, raw rate and schema are stored in the index so speed and acquisition settings remain available. `--steps` supports 32–512; requesting more steps than input readings interpolates rather than creating new measurements. The MPU low-pass filter remains approximately 44 Hz for acceleration and 42 Hz for rotation; the sample rate alone does not set the motion bandwidth.

## 7. Train and check the ESP useful-motion detector

This is separate from cloud symbol classification. The ESP learns **gesture-like versus background**, not which of the seven symbols was drawn.

```bat
python pipeline.py train --manifest manifest.json
python pipeline.py select --manifest manifest.json
python pipeline.py evaluate --manifest manifest.json --model artifacts/selected/model.json --config artifacts/selected/gate.json --out artifacts/test_report.json
python pipeline.py export --model artifacts/selected/model.json --config artifacts/selected/gate.json --out esp32_continuous/model_data.h
```

1. `train` builds a depth-four tree and an eight-tree depth-four forest in `artifacts/candidates/` using training data only.
2. `select` checks models/thresholds on validation recordings, choosing a qualifying model with less background retention (fewer trees breaks ties). Outputs go to `artifacts/selected/`. If neither qualifies, collect/improve data; do not assume exporting an unqualified model fixes it.
3. `evaluate` produces held-out test results. Inspect missed symbols, incorrect boundaries and retained background before deployment. Do not repeatedly tune using test sessions.
4. `export` writes the selected C++ model for the ESP. This does not train the cloud classifier or flash hardware.

Defaults require ≥98% gesture-sample retention; ≥95% of symbols retaining ≥95% of their samples, also per class; ≤60% background retained; candidate precision/recall ≥90% at interval IoU ≥0.5; and no detected multi-symbol merges. These are requirements, **not accuracy claims**. Ignored samples do not train the model; after an ignored span, the 300 ms feature history must refill. Evaluation replays continuously but excludes candidates touching ignore intervals and affected reference gestures; exclusion counts appear in reports.

## 8. Export realistic detector crops later

After training, replay recordings using the detector to get cloud examples with gameplay-like boundaries and up to 0.3 s pretrigger context:

```bat
python pipeline.py prepare --manifest manifest.json --source detector --model artifacts/selected/model.json --config artifacts/selected/gate.json --split validation --steps 100 --out artifacts/cloud_detector_validation
```

This also writes per-candidate CSVs and `index.csv`. It requires a trained model; manual export in step 6 does not. Candidates touching ignore intervals are skipped. Automatic labels use annotation overlap and need review: `background` and `ambiguous` candidates require cloud reject handling. Match preprocessing/step count between cloud training and deployment, and assess both clean manual crops and realistic detector crops.

## 9. Gameplay handoff and detector rationale

After successful validation/export, use `RECORD_MODE=false` and upload. The ESP captures 500 Hz data in a 4,096-reading RAM ring (8.192 seconds). Groups of five readings are averaged to 100 Hz internally. Every 50 ms it computes 42 motion features from the last 30 averages (300 ms), including three 100 ms section means, then applies the small learned classifier. Motion energy uses the last five averages (50 ms). Default start score is 0.4, with up to 0.3 s / 150 raw readings of pretrigger context. Capture ends after a relative energy drop to 20% of the recent peak sustained for 150 ms, or score below 0.2 for 200 ms. The confirmation tail is trimmed. No complete stillness is required; prolonged within-symbol pauses can split gestures, and unclear boundaries can merge them. Captured intervals must be 0.25–6 seconds including context; gaps/unfinished intervals/timeouts do not become partial packets.

The resulting `capture->output.frames` array is available in RAM; the demonstration sketch prints a diagnostic and releases it. Your teammate must copy/use it before release. A 100×6 float32 array is 2,400 payload bytes plus metadata, sent after completion rather than at a fixed 100 Hz. See [STREAMING_SPEC.md](STREAMING_SPEC.md). **Bluetooth transmission and integrated camera recording are not implemented.** The old `gateway.py` is unused.

A hand IMU cannot directly enforce “in front of the torso”; finger-only drawing with a stationary hand may be invisible. Dynamic calibration is deferred: a moving average during arbitrary motion could erase genuine slow gestures. Any later bounded gyro-bias correction should update only during confidently near-still moments and match training preprocessing.

## Verification

Run `python -m unittest discover -s tests -v`. Tests cover capture/segmentation, Python/C++ feature/energy parity, serial error reporting, ignored data, training/export, CSV content and video alignment/annotation controls using a generated video. Automated video checks do not establish synchronization accuracy on your camera footage. No real gesture accuracy is claimed and no board is flashed by these tools. Sensor timing, conversion latency and available RAM with Bluetooth active still require hardware testing. The firmware capture object uses about 60 KiB heap in gameplay mode (48 KiB raw ring plus up to 12 KiB output), in addition to static data and runtime/Bluetooth needs; allocation failure is reported explicitly.

### Windows label-save errors

If Windows blocks replacement of the labels file, the recorder preserves the latest draft in `.labels.json.tmp`, warns, and retries on the next save without sleeping in the capture loop. Recovery is printed. If it is still blocked at exit, preserve both files: the temporary file may be newer. Close programs holding the file, back up both copies, recover the temporary JSON as the main labels file, and review before training. Failed replacement never confirms labels as reviewed. Failure to write the temporary file stops recording instead.

Use `--out sessions/p1_day1_infinity_table_ccw_stand.csv`, rather than a directory followed by `/.csv`. The latter creates a file literally named `.csv`; it is confusing but does not prove the cause of an access denial. Use a new filename for the next recording.
