# Testing a model on the laptop, and recording training data over BLE

Two tools for developing the AI model with the Comms code. Set up the laptop first, as in the
[team README](../README.md#laptop-windows).

## Testing a model on the laptop, before a bitstream

This runs a trained Keras `.h5` model through the real glove → BLE → laptop → TCP path, with
inference on the laptop instead of the FPGA. Use it to check a model on live data before it is
converted to a bitstream. There is no broker and no phone. The result is printed in the client
pane.

The board client, `Comms/ultra96/tcp_client.py`, runs on the laptop with `--ai-h5`. It then uses AI's
Keras classifier (`AI/board/keras_stream.py`) instead of the FPGA one. Without `--ai-h5`, nothing
changes.

Once, install the ML packages into the Comms venv. From `Comms/`:
```powershell
.\.venv\Scripts\pip install tensorflow tf-keras qkeras
```

Then use two Windows Terminal panes:
```powershell
# Pane 1 (Comms/): the laptop relay, with no SSH tunnel, for one glove
.\scripts\run_pipeline.ps1 --no-tunnel --nodes 1
```
```powershell
# Pane 2 (Comms/ultra96/): the board client on the laptop, with the Keras model
..\.venv\Scripts\python tcp_client.py --host 127.0.0.1 --port 8888 --ai-h5 <path-to-model.h5>
```

Draw a gesture. Pane 2 logs each classified gesture and its confidence. The glove buzzes as the
gesture starts, but not after it is classified: this client sends its events to the laptop,
which passes only VIBRATE messages on to the gloves. Pane 1 warns about that once, which is
expected here.
A 7-class or an 8-class model both work, since the output size is read from the model.

When a gesture gives no event, Pane 2 says why:
```text
no AI event: node 1's glove rejected its flagged gesture of 570 samples
no AI event: node 1's flagged gesture of 84 samples gave no result (noise p=0.94)
no AI event: node 1's flagged gesture of 61 samples gave no result (low confidence fireball p=0.55 < 0.70)
no AI event: node 1's flagged gesture of 12 samples gave no result (buffer not full: 12/150 samples)
```
In order: the glove's detector rejected it, the model's top class was
`noise`, the top probability was below `CONF_THRESHOLD` in `AI/board/wk10_integration.py`, or
the buffer was not full yet.

## Recording training data over BLE

`Comms/laptop/record_ble.py` records labelled gesture data from the normal glove firmware, over the
same BLE code the pipeline uses. The data is the same 100 Hz stream that live inference sees.
Hardware's USB `record.py` is still used for the on-glove detector, which needs 500 Hz.

Run it **instead of** the pipeline, since a glove takes one BLE connection at a time. Use a
normal Windows terminal, which the label keys need. It also needs `numpy`, which the Comms venv
doesn't install: run `.\.venv\Scripts\pip install numpy` from `Comms/`. Then, from `Comms/laptop/`:
```powershell
..\.venv\Scripts\python record_ble.py --node 1 --participant p2 --session day3 --live-labels --symbol chop --drawing-mode air_front --direction cw --posture standing
```
`--node` is the glove's `PLAYER_ID`. The keys are the same as `record.py`'s: **Space** starts or
ends an attempt, **D** discards it, **Q** finishes, then **y** marks the recording reviewed.
Recordings that are not reviewed are not exported. Unlabelled time is background, which trains
the `noise` class.

Files are saved under `Hardware/Data Collection/sessions/ble/`, named automatically and never
overwritten (`_2`, `_3`):

| File | Holds |
|---|---|
| `<name>.csv` | `sequence, esp_time_us, ax_g..gz_dps, host_rx_unix_ns, imu_status, gesture_flag` |
| `<name>.labels.json` | The labelled intervals, as `record.py` writes them |
| `<name>.meta.json` | `raw_hz: 100`, `transport: ble`, node, participant, session, counts of gaps and late packets |

Timing comes from the firmware. The 8-bit SENSOR `seq` is unwrapped into a running count, and
`esp_time_us = count x 10 ms`. So a lost packet or a bad IMU reading leaves a gap rather than
squeezed samples, and the exporter never crops a gesture across a gap. Then, from `AI/`:
```powershell
python -m gesture_pipeline.export_dataset --out new_gesture_100hz_v3
```
This picks up the BLE recordings along with the USB ones. It reads `raw_hz` from each
`.meta.json`.
