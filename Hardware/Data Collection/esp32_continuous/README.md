# ESP32 USB gesture data collection and labelling

This guide is for collecting continuous-gesture IMU data from the ESP32 over USB and labelling it. Stop after the labels have been reviewed. You do **not** need to create a manifest, export 100-step examples, train a model, or run the gameplay pipeline.

## Use this firmware

Open and upload:

```text
Hardware/Data Collection/esp32_continuous/esp32_continuous.ino
```

Do **not** use `esp32_continuous_pipeline_test`. That folder contains later inference/pipeline-test firmware rather than the USB data-collection firmware.

Before uploading, confirm this line is unchanged:

```cpp
constexpr bool RECORD_MODE=true;
```

In recording mode, the ESP32 streams all IMU readings to the laptop over USB at 500 Hz. No trained model or Bluetooth connection is required. The motor output on GPIO2 is held low.

## 1. Hardware and software setup

### Hardware

- Mount one MPU6050 firmly on the back of the hand. Keep its position and orientation consistent throughout a collection session.
- For the DFR0478 FireBeetle ESP32, connect SDA to GPIO21, SCL to GPIO22, AD0 low for address `0x68`, 3.3 V, and GND.
- Connect the ESP32 to the laptop with a USB data cable.

### Laptop

1. Open the sketch above in Arduino IDE.
2. Select the FireBeetle ESP32 board and its COM port, then upload the sketch.
3. Close Arduino Serial Monitor after uploading. The Python recorder cannot use the COM port while Serial Monitor has it open.
4. Open Anaconda Prompt, Command Prompt, or PowerShell at the repository root and run:

```bat
cd "Hardware\Data Collection"
python -m pip install -r requirements.txt
```

Find the ESP32's COM port in Arduino IDE or Windows Device Manager. The examples below use `COM9`; replace it with the actual port.

## 2. Do a short practice recording first

The recommended workflow labels each attempt while it is being recorded. Use one symbol and one variant per file so the labels retain the correct drawing conditions.

From `Hardware/Data Collection`, run a command like:

```bat
python record.py --port COM9 --out sessions/p2_day1_circle_table_cw.csv --participant p2 --session day1 --live-labels --symbol circle --drawing-mode table --direction cw --posture seated
```

Change all of the following as needed:

- `COM9`: the ESP32's COM port.
- `p2`: the participant ID agreed with the team.
- `day1`: the collection session/day ID.
- `circle`: `circle`, `arc`, `infinity`, `caret`, `z`, `triangle`, or `star`.
- `table`: use `table` or `air_front`.
- `cw`: use `cw`, `ccw`, or `na`. Use `na` when clockwise/counter-clockwise does not apply.
- `seated`: use `seated` or `standing`.

Use a new, descriptive filename whenever the symbol or variant changes. Existing recordings are never overwritten. Also start a new file after an ESP32 reset or reconnect.

### Controls during recording

Keep the recorder terminal focused. The controls are single key taps; do not press Enter.

| Key | Action |
|---|---|
| Space | Start an attempt; tap Space again immediately after the final stroke to end it |
| D | Mark the current or most recently completed attempt as `ignore` |
| Q | Finish the recording and proceed to confirmation |

For each attempt:

1. Position the drawing hand.
2. With the other hand, tap and release **Space**. Wait for `START`.
3. Draw exactly one symbol.
4. Tap **Space** immediately after the last stroke, before lowering or repositioning the drawing hand. Wait for `END`.
5. If the attempt was wrong, tap **D**. If it is still open, finish it with **Space** as well.
6. Repeat, varying speed and size naturally.

Leave ordinary movements between attempts in the recording. Raising and lowering the hand, repositioning, arm swings, walking, and similar non-symbol motion become useful background data. Do not deliberately draw an extra symbol without labelling or ignoring it.

When finished, tap **Q**. Answer `y` only if every symbol attempt was marked, mistakes were discarded, and the start/end boundaries were usable. Otherwise press Enter to keep the labels as a draft and review them in the next section.

The practice run should show valid samples arriving and should not report continuing gaps, malformed rows, a restart, or heavy clipping. If it does, fix the cable, port, wiring, or sensor mounting before collecting the full dataset.

### Alternative: record first and label afterward

If pressing keys during the gestures is impractical, omit the live-label arguments:

```bat
python record.py --port COM9 --out sessions/p2_day1_circle_table_cw.csv --participant p2 --session day1 --notes "circle, table, clockwise, seated"
```

Record a phone video at the same time, include the synchronization movements described below, and press **Ctrl+C** to stop. Then label the whole recording in the annotator. Keep a written or visible gesture order because the annotator does not play video audio. This method does not automatically store the drawing mode, direction, or posture in each interval, so use descriptive filenames/notes and tell the teammate who will process the dataset later.

## 3. Review or repair the labels

Run the annotator on the recorded CSV:

```bat
python annotate.py sessions/p2_day1_circle_table_cw.csv
```

The window shows acceleration and gyro plots. Existing live labels appear as shaded intervals.

- Select the required symbol or `ignore` on the left.
- To add an interval, click the plot at the true start and choose **Mark start**. Move to the true end and choose **Mark end**. You can also drag across an interval on a plot.
- Start means the point where the symbol stroke begins, after positioning the hand.
- End means just after the final stroke, before lowering or repositioning the hand.
- Use **Delete at cursor** and redraw an incorrect interval, or use **Undo last** for the most recently added interval.
- A redrawn interval keeps its symbol label but does not automatically regain the live label's drawing mode, direction, and posture fields. Note any redrawn examples for the teammate who will process the dataset later.
- Leave ordinary background movement unlabelled.
- Mark mistakes, uncertain boundaries, or unusable sections as `ignore`.
- Symbol intervals must not cross a reported sensor gap. Mark an unusable gap area as `ignore` instead.
- Use **Save draft** while working. After checking the **entire recording**, click **Save reviewed labels**.

For more accurate after-the-fact review, record a separate phone video of the hand and save it beside the CSV, for example `sessions/p2_day1_circle_table_cw.mp4`. Then open it with:

```bat
python annotate.py sessions/p2_day1_circle_table_cw.csv --video sessions/p2_day1_circle_table_cw.mp4
```

Make three clear synchronization movements near the start and again near the end of the recording. In the annotator, leave **Link timelines** unchecked, locate the same movement in the plot and video, and choose **Add sync point**. Add a second point near the end to account for timing drift, then enable **Link timelines** and verify the alignment before labelling. Audio is not played, so keep a visible or written record of the gesture order.

## 4. What to hand over

For every recording, keep these files together:

| File | Purpose |
|---|---|
| `name.csv` | Full 500 Hz raw sensor recording, including background |
| `name.meta.json` | Participant/session details and recording diagnostics |
| `name.labels.json` | Gesture and ignore intervals; must say `"reviewed": true` |
| `name.mp4` | Optional but recommended review video |

Before handing the data over, check that:

- the correct non-pipeline sketch was used with `RECORD_MODE=true`;
- the raw CSV, metadata, and reviewed labels file all exist;
- every real symbol attempt is labelled or marked `ignore`;
- normal background movement is left unlabelled;
- filenames and participant/session IDs are unique and consistent; and
- no collection file was edited or replaced manually.

That is the end of this workflow. Dataset manifests, resampling/export, model training, and pipeline testing are handled separately.
