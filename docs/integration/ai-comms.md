# AI–Comms integration

This document explains how the AI gesture classifier is connected to the Comms pipeline, the
Python API shared by both teams, and what must change when either side changes its code.

> **Retired (28 Oct 2026):** on `full-pipeline`, `--sensor-trigger`, the fixed window and the
> `ai_api.py` path described below are removed. The board classifies with `--gesture-flag` and
> AI's `wk10_integration.py` instead: see [Full pipeline](full-pipeline.md). This page stays as
> the record of the 15 Oct integration.
>
> **Current status (15 Oct 2026):** FPGA classification has been exercised in the live
> Configuration B pipeline on branch `ai-comms`. The FireBeetle firmware still sends dummy IMU
> values, and `--sensor-trigger` is only a test trigger. The run validates integration and timing,
> not gesture accuracy or production gesture detection.

## Architecture overview

```text
FireBeetle
  SENSOR packet: node, seq, raw ax/ay/az/gx/gy/gz counts
       |
       | BLE
       v
Laptop Comms relay
       |
       | framed JSON over TCP
       v
Ultra96 Comms client
  keeps validated rolling history for each player
       |
       | selected sensor window
       v
AI's local Python API
  GestureClassifier.classify(samples, timeout=0.05)
       |
       | (label, confidence)
       v
Comms builds an AI_EVENT
       |
       +-- Configuration A: TCP to laptop -> laptop MQTT broker -> phones
       |
       `-- Configuration B: board-local MQTT broker -> phones
```

The AI API is an **in-process Python API**. Comms imports AI's module and
calls it inside the Ultra96 process. BLE, TCP, MQTT, and phone delivery remain Comms concerns.

## Responsibilities and shared API

`AI/board/ai_api.py` was the boundary between the two subsystems.

| Owner | Files | Purpose |
|---|---|---|
| AI | `AI/board/ai_api.py`, `gesture_min_q.xsa` | Define the model input requirements; convert and classify a window; own FPGA/DMA/model details, labels, and confidence |
| Comms | [`Comms/ultra96/gesture.py`](../../Comms/ultra96/gesture.py) | Validate SENSOR messages, track sequence gaps, and keep separate rolling sample history for each player |
| Comms | [`Comms/ultra96/tcp_client.py`](../../Comms/ultra96/tcp_client.py) | Receive samples, invoke the shared API, and provide Configuration A's TCP event sink |
| Comms | [`Comms/ultra96/tcp_client_mqtt.py`](../../Comms/ultra96/tcp_client_mqtt.py) | Reuse the same receiver/API path and provide Configuration B's MQTT event sink |
| Comms | [`Comms/ultra96/ai_events.py`](../../Comms/ultra96/ai_events.py) | Convert the classifier result into the common `AI_EVENT` payload |
| Comms | [`Comms/scripts/run_board.py`](../../Comms/scripts/run_board.py) | Deploy the Comms board files, AI API, and bitstream to `~/comms/` and start the client |

Comms owns the validated rolling sample history, while AI owns the model's input requirements.
Ownership of final gesture segmentation is not decided yet.

### What Comms provides to AI

Comms calls `classify(samples, timeout)` with:

- one player's validated raw IMU samples, oldest first;
- the fields `ax`, `ay`, `az`, `gx`, `gy`, and `gz` as signed integer counts;
- exactly the number of samples declared by AI's `WINDOW`; and
- a timeout limiting how long inference may block Comms' packet-processing loop.

Before this call, Comms has already decoded the BLE packet, rejected malformed, repeated, or
backwards packets, separated the players, and maintained their rolling histories. AI does not
need to know how the samples arrived or how the result will be transported.

### What AI exposes to Comms

AI exposes these symbols from `ai_api.py`:

| Symbol | What Comms uses it for |
|---|---|
| `WINDOW`, `CHANNELS` | Determine how much history to retain and which fields are required |
| `LABELS` | Describe the model's possible output labels |
| `BITSTREAM` | Identify the matching XSA that must be deployed with the API |
| `GestureClassifier` | Load/setup the accelerator and provide `classify()` |
| `ClassifyError` | Report a recoverable failure of one inference |

The call used by Comms is:

```python
from ai_api import GestureClassifier

model = GestureClassifier()  # loads the default XSA and performs one warm-up inference
label, confidence = model.classify(samples, timeout=0.05)
```

The instance is created once and reused across TCP reconnections.

### Input contract

`samples` currently contains exactly 100 entries from one player, ordered oldest to newest. Each
entry has six raw integer counts:

```python
{
    "ax": 120,
    "ay": -4,
    "az": 16380,
    "gx": 7,
    "gy": -2,
    "gz": 1,
}
```

<details>
<summary>Current model-specific preprocessing</summary>

- The layout is time-major: `t0(ax, ay, az, gx, gy, gz)`, then `t1(...)`, and so on.
- Accelerometer counts assume ±2 g (`16384` counts/g) and are converted to m/s².
- Gyroscope counts assume ±500 °/s (`65.5` counts/(°/s)) and are converted to rad/s.
- The resulting 600 values are converted to signed Q6.10 `int16`; out-of-range values are clipped.

These conversions belong to AI, but their sensor-range assumptions must remain consistent with
the real sensor configuration and model training.

</details>

### Output, timing, and failure contract

`classify()` returns `(label: str, confidence: float)`. The label is currently one of `"0"` to
`"6"`; confidence is the selected class's softmax probability. Comms treats the label as an
opaque string and does not interpret the class number.

Because `classify()` runs synchronously in the receiver loop:

1. It must return or raise promptly within the supplied timeout and must not sleep.
2. A recoverable per-window failure must raise `ClassifyError`; it must not exit, print, or
   silently return a dummy prediction.
3. Importing `ai_api.py` must not load the FPGA or require PYNQ. Board-only imports and hardware
   setup happen when `GestureClassifier` is constructed. Any new dependency must be available in
   both the Ultra96 environment and the Comms laptop test environment.
4. Constructor/setup failures may stop startup and should remain clear exceptions.

When `ClassifyError` is raised, Comms logs it, skips that `AI_EVENT`, and continues receiving
packets. Recovery after a real DMA timeout is still unconfirmed: the current API does not reset
the DMA or reload the bitstream before the next inference.

## Current window and trigger behaviour

1. `gesture.Windows` keeps the latest `WINDOW` valid samples separately for each player and ignores
   malformed, repeated, or backwards SENSOR messages.
   **Only real IMU readings enter a window.** A real reading is a SENSOR message whose
   `reserved[0]` IMU status is `1`. Any other status empties that player's window: `2` or `3`
   (the zeros sent while the IMU is missing or recovering), `0` (old firmware's dummy ramp), an
   unknown value, or no status at all. The window then refills with the next `WINDOW` readings.
   So a window never mixes readings from before and after an outage, and an unplugged IMU's
   zeros are never classified. Until the window is full again, a trigger logs `no AI event: node
   N has K of 100 samples; the last M had no IMU reading`. The model is given only the six
   channels in `CHANNELS`.
2. `--sensor-trigger N` selects a packet after roughly `N/2` to `1.5N` packets from that player.
   This is a test stand-in for a gesture boundary, not production segmentation.
3. A selected player is classified only after its history is full. The returned label becomes
   `AI_EVENT.gesture`; confidence, inference time, and missing-packet count remain log-only.
4. Configurations A and B create the same event and differ only in how they deliver it.

ECHO keypress events and `--event-every` events continue to use dummy gestures. Only a selected
SENSOR packet calls the classifier.

## Event sent to the rest of the system

`ai_events.make_event()` creates the inner `AI_EVENT`. Configuration A sends this event to the
laptop over TCP. Configuration B wraps it in the standard MQTT envelope and publishes it at QoS 1
to `game/gesture/<player>`:

```json
{
  "event_id": "ultra96-relay:999b7d1f:0",
  "source": "ultra96-relay",
  "seq": 0,
  "ts": 1792077322.8335,
  "payload": {
    "type": "AI_EVENT",
    "player": 1,
    "seq": 0,
    "gesture": "4",
    "mana_cost": 23,
    "timestamp": 1792077322.833211
  }
}
```

`mana_cost` is still randomly generated dummy data. `confidence`, `t_infer_ms`, and the window's
missing-packet count currently appear only in the board log.

## Change guide

AI and Comms may change their internal implementations independently while the public API contract
and event contract remain unchanged. The following changes require coordination:

| Change | Required coordination |
|---|---|
| Bitstream filename | AI updates `ai_api.BITSTREAM`; Comms updates `run_board.py::BITSTREAM`; the filename-agreement test must pass |
| Window length, channel names/order, or API input shape | AI, Comms, and possibly Sensors update the API, rolling history, packet contract, and tests together |
| IMU range, scale, units, or calibration | Sensors and AI align hardware configuration, training/preprocessing assumptions, and `ai_api.py` scales |
| Gesture class names or number of classes | AI updates `LABELS` and the model output; Game/Vis agree on strings carried in `AI_EVENT.gesture` |
| Return type, exception, timeout, or classifier lifecycle | AI and Comms update `ai_api.py`, `gesture.py`, event/log handling, and tests together |
| MQTT topic or `AI_EVENT` fields | Comms updates both A/B paths, tests, and phone/game consumers; the classifier API need not change |

After any AI deployment change, run `run_board.ps1 --sync`; otherwise the board may still use an
older API or XSA even when the repository is current.

## Run the integrated path

The Ultra96 can load only one bitstream at a time, so coordinate before running another FPGA
experiment. From `Comms/`, follow the [Configuration B setup](../subsystem/comms/README.md#2-run-configuration-b),
then use two terminals:

```powershell
# Terminal 1: laptop BLE/TCP relay and reverse tunnel
.\scripts\run_pipeline.ps1

# Terminal 2: deploy Comms + AI files, then start classification on the board
.\scripts\run_board.ps1 --sync --sensor-trigger 100
```

`--sync` copies every `Comms/ultra96/*.py` file plus `AI/board/ai_api.py` and
`AI/board/gesture_min_q.xsa` to `~/comms/`.

Expected board log lines:

```text
classifier: GestureClassifier
trigger node=1 seq=104 ...
AI event seq=0 player=1 ... gesture=0 ... conf=0.903 t_infer_ms=5.239 missing=0
published ultra96-relay:...:0 to game/gesture/1 ...
```

`StubClassifier` is for laptops/tests where `/sys/class/fpga_manager/fpga0` is absent. A real
Ultra96 run should log `GestureClassifier`; missing PYNQ or an incompatible bitstream should fail
visibly rather than silently use the stub.

AI can check the API directly on the board with:

```bash
sudo env XILINX_XRT=/usr ~/comms-venv/bin/python ai_api.py <csv> [bitstream]
```

This compares the first 10 CSV rows in the same format as `test_accuracy.py`, then times 100
calls to `classify()`.

## Verification and current evidence

The code-level contract is covered by:

- [`test_gesture.py`](../../Comms/laptop/tests/test_gesture.py): history order, sequence handling,
  raw-count conversion, time-major DMA layout, Q6.10 conversion, return value, errors, warm-up,
  and real/stub classifier selection.
- `test_sensor_trigger.py` (now `test_gesture_flag.py`): trigger selection,
  full-window requirement, failure isolation, and equal classifier output in Configurations A/B.
- [`test_run_board.py`](../../Comms/laptop/tests/test_run_board.py): deployment of `ai_api.py` and
  the XSA, including the bitstream filename agreement.

The 15 Oct Configuration B hardware log records:

| Check | Result |
|---|---:|
| Duration | about 10 minutes |
| SENSOR rate | about 200 messages/s across two nodes |
| Triggers / classified events / MQTT PUBACKs | 1,214 / 1,214 / 1,214 |
| Classifier failures / stale or unserialisable MQTT drops | 0 / 0 |
| FPGA inference time | 5.00 ms median, 5.39 ms p95, 5.99 ms max |
| Board receives trigger → event created | 5.84 ms median |

This proves no observed loss through the board's MQTT broker acknowledgement for that run. The
captured report has no phone timestamps, so it does **not** prove that all 1,214 events reached or
were rendered by a phone. A separate manual comparison recorded 10/10 matching rows against AI's
`test_accuracy.py`, but that check was not reproduced by the software tests.

## Undecided for now

- **AI:** map labels `"0"`–`"6"` to final gesture/action names.
- **AI:** confirm recovery after a real DMA timeout and whether reset or overlay reload is needed.
- **AI + Sensors:** confirm the sampling rate, channel order, IMU ranges, and calibration.
- **Team:** decide whether final segmentation is AI-based or comes from an explicit sensor boundary,
  then replace `--sensor-trigger` accordingly.
- **Team:** decide whether `confidence` and missing-packet information belong in the event payload.
- **Game:** replace random `mana_cost` once the game engine owns mana calculation.
