# The classifier on the Ultra96

| File | Job |
|---|---|
| `wk10_integration.py` | The board classifier: loads the overlay, decodes each IMU sample, keeps a ring buffer per player, runs the FPGA on end-of-gesture, and applies the no-event rule. No networking: Comms owns the sockets |
| `gesture_model.xsa` | The deployed overlay: the `mage_noise_robust` model, 8 classes ([docs/results.md](../../docs/results.md)) |
| `keras_stream.py` | The same API with a Keras `.h5` instead of the FPGA, for testing a model on the laptop |

Comms imports `wk10_integration` from `Comms/ultra96/gesture.py`. `run_board --sync` copies it and
the overlay named by `--bitstream` to `~/comms/` on the board, and `--gesture-flag` turns
classification on. The run commands are in the [team README](../../README.md#2-running-the-game).

## Deploying a new overlay

1. Build the `.xsa` with [hls4ml/](../hls4ml/README.md).
2. Copy it over `AI/ultra96/gesture_model.xsa`, so no launcher or doc needs a new name.
3. If the class order changed, update `NEW_LABEL_MAP` in `wk10_integration.py` and
   `keras_stream.py`. The class ids must equal the training label order (the sorted gesture names,
   then `noise`).
4. Run with `--sync`, which copies the new overlay to the board.

## The contract with Comms

**Input:** one `IMUSample` per received packet: `ax ay az gx gy gz` as **raw int16 counts**,
`end_of_gesture` (True on the glove's last flagged sample) and an optional `timestamp`. The module
does the only preprocessing, the same as training: accel × 9.80665/8192 → m/s², gyro ×
(π/180)/65.5 → rad/s, no normalisation.

**Output:** `feed()` returns a `GestureResult` (`class_id`, `gesture`, `confidence` = top softmax
probability, `logits`, `timestamp`) only on an end-of-gesture sample, and only when there is a
gesture to send. Otherwise it returns `None`, and `last_reject` says why:

| `last_reject` | Meaning |
|---|---|
| `buffer not full: n/150 samples` | Fewer than 150 samples since this player's first one |
| `noise p=…` | The top class is `noise` (in `REJECT_LABELS`) |
| `low confidence <gesture> p=… < 0.70` | The top probability is below `CONF_THRESHOLD` (0.7) |

**Label map** (`--vocab new`, the default; `--vocab old` is the retired 7-shape model):

| id | Trained as | Sent as | | id | Trained as | Sent as |
|---|---|---|---|---|---|---|
| 0 | chop | `warrior` | | 4 | lightning | `lightning` |
| 1 | circle | `mage` | | 5 | star | `hero` |
| 2 | fireball | `fireball` | | 6 | tank | `shield` |
| 3 | jabs | `rogue` | | 7 | noise | no event |

## API

```python
from wk10_integration import GestureClassifier, IMUSample, ai_event_from_result

clf = GestureClassifier(bitstream="gesture_model.xsa")      # once: loads the overlay, allocates DMA buffers
res = clf.feed(IMUSample(ax, ay, az, gx, gy, gz, end_of_gesture=eog, timestamp=t), player=1)
if res is not None:
    event = ai_event_from_result(res, player=1, seq=seq)   # the AI_EVENT dict
```

- `GestureClassifier(bitstream, config=ModelConfig(), mock=False)`: build once. `mock=True` skips
  pynq and returns a uniform softmax, for wiring tests off the board. `vocab_config("new")` builds
  the `ModelConfig` for a vocabulary.
- `feed(sample, player)`: the one call per packet. Each player has their own 150-sample ring buffer,
  which rolls continuously with no reset between gestures. One overlay serves both players.
- `reset(player=None)`: clear one player's buffer, or all of them. Only for joining two unrelated
  stretches of samples, such as after a reconnect.
- `infer(player)` / `infer_window(samples)`: classify a player's buffer now, or a caller-owned
  `(150, 6)` block of decoded units.
- `last_result`, `last_reject`: the latest result, and why the latest inference gave none.

**On the board:** the backend writes the 900 inputs as `round(x × 1024)` int16 into a DMA buffer,
pulses `ap_start` on `cnn_axi_0`, streams through `axi_dma_0`, and reads one int16 logit per class
(`/ 1024`). A `label_map.json` next to the overlay, if present, is checked against the label map at
start-up.

**As a script:** `python3 wk10_integration.py --mode server` loads the overlay and idles (a smoke
test; add `--mock` off the board). `--mode test` is the old accuracy harness for the retired
7-shape model's decoded CSV (`gestures_real_train.csv`, not in the repo), so it doesn't apply to
`gesture_model.xsa`. Score models with `train.exp_eval` and `hls4ml/convert.py` instead.

## Testing a model on the laptop

`keras_stream.py` runs a trained `.h5` through the real glove → BLE → laptop → TCP path, with
inference on the laptop instead of the FPGA. Use it to check a model on live data before building
a bitstream. Only the backend differs (Keras instead of pynq); the ring buffer, decode,
end-of-gesture logic and no-event rule are inherited from `wk10_integration`, and the class count
is read from the model.

Once, from `Comms/`: `.\.venv\Scripts\pip install tensorflow tf-keras qkeras`. Then two Windows
Terminal panes:
```powershell
# Pane 1 (Comms/): the laptop relay, with no SSH tunnel, for one glove
.\scripts\run_pipeline.ps1 --no-tunnel --nodes 1
```
```powershell
# Pane 2 (Comms/ultra96/): the board client on the laptop, with the Keras model
..\.venv\Scripts\python tcp_client.py --host 127.0.0.1 --port 8888 --ai-h5 <path-to-model.h5>
```
Draw a gesture. Pane 2 logs each classified gesture and its confidence, or why there was none:
```text
no AI event: node 1's glove rejected its flagged gesture of 570 samples
no AI event: node 1's flagged gesture of 84 samples gave no result (noise p=0.94)
no AI event: node 1's flagged gesture of 61 samples gave no result (low confidence fireball p=0.55 < 0.70)
no AI event: node 1's flagged gesture of 12 samples gave no result (buffer not full: 12/150 samples)
```
The glove buzzes as the gesture starts but not after it is classified, since this client's events
go to the laptop, which passes only VIBRATE messages on. Pane 1 warns about that once, as expected.

## Power and latency

Both are measured through Comms, with this classifier in the loop:

- [docs/power.md](../../docs/power.md): the board's power while idle and playing, and a sweep of CPU
  and FPGA clock settings (`Comms/ultra96/power_sweep.py`).
- [docs/latency.md](../../docs/latency.md): the end-to-end latency, including the classifier call
  on its own (`t_infer_ms`).
