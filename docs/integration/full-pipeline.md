# Full pipeline: Hardware -> Comms -> AI

This is the runbook for the merged `mvp-full-pipeline` branch. It explains how one player's
physical motion becomes an AI event and how to run that path in Configuration B.

It does not replace the detailed [Hardware-Comms](hardware-comms.md) or
[AI-Comms](ai-comms.md) contracts. Those documents describe each integration boundary; this
page shows how the two boundaries work together.

> **Current status (16 Oct 2026):** Hardware, Comms, and AI are merged. Two FireBeetles have
> streamed through the merged Configuration B pipeline, and the Ultra96 correctly refused to
> classify their missing-IMU zeros. FPGA classification and MQTT publishing were previously
> exercised with synthetic sensor values. A real-IMU-to-FPGA classification run is still
> blocked by the accelerometer scaling mismatch described below. The Visualiser is not part of
> this integration yet, so the current endpoint is the Ultra96's MQTT broker.

## What is connected

```text
One MPU6050 per player
  raw ax, ay, az, gx, gy, gz
       |
       | I2C, read at a target 100 Hz
       v
FireBeetle firmware
  adds IMU status, sequence number, and CRC
       |
       | encrypted BLE, 26-byte SENSOR packets
       v
Laptop Comms relay
  validates and decodes packets, identifies the player,
  adds the laptop receive time, and forwards framed JSON
       |
       | TCP through the reverse SSH tunnel
       v
Ultra96 Comms client
  keeps one validated 100-sample window per player
       |
       | GestureClassifier.classify(samples, timeout=0.05)
       v
FPGA classifier
  returns a label and confidence
       |
       v
Ultra96 Comms client
  builds an AI_EVENT and publishes it at QoS 1
       |
       | MQTT topic game/gesture/<player>
       v
Ultra96 Mosquitto broker
  current end of the demonstrated merged path
       |
       `-- Visualiser/phones: to be integrated later
```

This page uses **Configuration B**, where the Ultra96 publishes directly to its own loopback
broker. The laptop has no MQTT role in this configuration. Configuration A is still supported,
but it is not the path currently used for full-pipeline integration.

## Run the current pipeline

Complete the one-time [Configuration B setup](../subsystem/comms/README.md#2-run-configuration-b)
first. In particular:

- flash the merged FireBeetle firmware with the correct `PLAYER_ID` on each board;
- prepare the laptop virtual environment and `.env` tunnel settings;
- prepare the Ultra96 virtual environment and SSH access; and
- run Mosquitto on the Ultra96. The board launcher warns if port `1883` is unavailable or is
  exposed beyond loopback.

Open two PowerShell terminals in `team/Comms`. Start the laptop side first so the TCP server and
reverse tunnel are ready:

```powershell
# Terminal 1: FireBeetles -> BLE -> laptop -> TCP tunnel
.\scripts\run_pipeline.ps1
```

Then deploy the current Comms board code, AI API, and bitstream, and start the board client:

```powershell
# Terminal 2: TCP -> window -> FPGA -> board-local MQTT broker
.\scripts\run_board.ps1 --sync --sensor-trigger 100
```

`--sync` copies every `Comms/ultra96/*.py` file, `AI/board/ai_api.py`, and
`AI/board/gesture_min_q.xsa` into `~/comms/` before restarting the client. Use it after changing
any of those files. Without `--sync`, the launcher attaches to an existing board client if one
is already running; new command-line arguments do not replace that process's arguments.

`--sensor-trigger 100` enables classification and chooses a test boundary after a random 50 to
150 SENSOR packets from each player. It is deliberately opt-in: this is a repeatable integration
trigger, not the final gesture-segmentation design. Omitting the flag leaves sensor
classification off.

Useful board commands are:

```powershell
# Stop the detached Configuration B client
.\scripts\run_board.ps1 --stop

# Copy today's UTC board logs into Comms/logs/board/<date>/
.\scripts\run_board.ps1 --fetch-logs
```

### What a healthy run should show

The board log should first identify the real FPGA classifier:

```text
classifier: GestureClassifier
```

Once a player has 100 valid readings, a selected SENSOR packet should produce three related
lines:

```text
trigger node=1 seq=... t_laptop_rx=... t_rx=...
AI event seq=... player=1 ... gesture=... conf=... t_infer_ms=... missing=...
published ultra96-relay:... to game/gesture/1 ...
```

If the IMU is absent or recovering, the expected result is instead:

```text
no AI event: node 1 has 0 of 100 samples; the last ... had no IMU reading
```

That line is a safety check working correctly: zero-filled fault packets are reaching the
Ultra96, but they are not reaching the classifier.

## How a SENSOR packet becomes an event

1. [`imuRead()`](../../Comms/firebeetle/imu.cpp) returns six signed raw counts and an
   [`ImuStatus`](../../Comms/firebeetle/imu.h). Only status `IMU_OK` (`1`) represents a real
   sensor reading.
2. [`firebeetle.ino`](../../Comms/firebeetle/firebeetle.ino) puts the six values first, places
   the status in `reserved[0]`, and sends a sequenced, CRC-protected packet every 10 ms while BLE
   is ready.
3. [`ble_client.py`](../../Comms/laptop/ble_client.py) checks the packet, obtains the player from
   the BLE connection, and adds `timestamp = time.perf_counter()` when the notification is
   received. This timestamp is a laptop receive time, not the time of the physical measurement.
4. [`pipeline_board_broker.py`](../../Comms/laptop/pipeline_board_broker.py) forwards the decoded
   message to the Ultra96 over TCP. It does not publish MQTT messages.
5. [`gesture.Windows`](../../Comms/ultra96/gesture.py) keeps independent sequence state and
   sample history for each player. It passes exactly the channels and window size declared by
   AI's API to the classifier.
6. [`GestureClassifier`](../../AI/board/ai_api.py) converts the counts to model units, quantises
   the 600 inputs, runs the FPGA accelerator, and returns `(label, confidence)`.
7. [`ai_events.make_event()`](../../Comms/ultra96/ai_events.py) places the returned label in
   `AI_EVENT.gesture`. [`tcp_client_mqtt.py`](../../Comms/ultra96/tcp_client_mqtt.py) wraps the
   event and publishes it to `game/gesture/<player>` through the board's local broker.

The published payload currently contains `type`, `player`, `seq`, `gesture`, `mana_cost`, and
`timestamp`. Confidence, inference time, and missing-packet count are log-only. `gesture` is
currently a numeric string from `"0"` to `"6"`, and `mana_cost` is still random dummy data.

## Rules that protect the classifier input

The Ultra96 applies these rules separately to each player:

- A window contains exactly 100 samples in oldest-to-newest order. Each sample has only `ax`,
  `ay`, `az`, `gx`, `gy`, and `gz`.
- Only packets with `reserved[0] == IMU_OK` enter the window. Missing, recovering, old-firmware,
  unknown, or absent status clears the window instead of joining readings across an outage.
- Malformed, repeated, and backwards SENSOR messages do not enter the window.
- Forward sequence gaps are recorded as missing packets but do not invent replacement samples.
- If more than `0.5 s` passes between a node's laptop receive timestamps, that node's window,
  sequence state, no-IMU count, and receive-time state are cleared before the next sequence is
  checked. This covers a BLE reconnect, FireBeetle reboot, or long stream stall without changing
  the packet protocol.
- A TCP reconnection creates fresh windows for all players. The already-loaded classifier is
  reused across those sessions.
- A recoverable `ClassifyError` skips one event and leaves packet reception running.

At 100 Hz, a `0.5 s` break already represents about 50 absent samples, so the old 100-sample
window is no longer a useful continuous gesture window.

## The gesture flag path (in progress)

`--gesture-flag` is the replacement for `--sensor-trigger`'s fixed window. It is built and tested
on the laptop, but no glove has a detector yet. Use one of the two flags, not both.

- **Hardware:** `gestureFlag()` in [`imu.h`](../../Comms/firebeetle/imu.h) is your detector. It gets
  every sample and returns `1` while the hand may be making a gesture, otherwise `0`. The version in
  `imu.cpp` is a placeholder that always returns `0`. `buildPacket()` sends the flag in
  `reserved[1]`, and the serial timing line now also prints the slowest `gestureFlag()` call.
- **Comms:** the board applies the rules above, then passes every remaining sample to AI's
  `feed(IMUSample(...), player)`: `ax`..`gz`, `end_of_gesture` and `timestamp` (the laptop's
  receive time). `end_of_gesture` is `True` on the first sample after the flag drops from 1 to 0.
  It calls `reset(player)` after an IMU outage or a gap of more than `0.5 s`, `reset()` for every
  player at the start of each TCP session, and publishes whatever AI returns. A DMA stall in AI
  logs a warning and skips that gesture rather than ending the session.
- **AI:** [`wk10_integration.py`](../../AI/board/wk10_integration.py) owns the buffering and
  inference, on `baseline_improved.xsa`. Its contract is
  [`README_gesture_stream_sim.md`](../../AI/board/README_gesture_stream_sim.md). Each player has
  their own 150-step ring buffer, and `feed()` classifies it when `end_of_gesture` is `True`.
  `ai_api.py` stays for `--sensor-trigger` only.

## Contracts that must agree across teams

| Contract | Current code | Owner/action |
|---|---|---|
| Sensor layout | Six raw `int16` counts: `ax, ay, az, gx, gy, gz` | Hardware and Comms must preserve order and meaning |
| Sensor rate | FireBeetle target: 100 Hz; AI window: 100 samples | Hardware, Comms, and AI coordinate any change |
| IMU validity | `reserved[0] == 1` means real data | Hardware defines status; Comms enforces it |
| Gyroscope range | Firmware: +/-500 degrees/s; AI: `65.5` counts/(degree/s) | Currently aligned |
| Accelerometer range | Firmware: +/-4 g and `8192` counts/g; AI: `8192` counts/g | Aligned; a Comms test checks `ai_api.py` against `firebeetle/imu.h` |
| AI deployment | `--sensor-trigger`: `ai_api.BITSTREAM` and `run_board.py` both name `gesture_min_q.xsa`. `--gesture-flag`: `gesture.AI_BITSTREAM` and `run_board.py` both name `baseline_improved.xsa` | AI and Comms update together |
| Model input | `--sensor-trigger`: `WINDOW`, `CHANNELS`, scaling, and quantisation come from `ai_api.py`. `--gesture-flag`: all of it is inside `wk10_integration.py` | AI owns implementation; interface changes require coordination |
| Per-sample API | `GestureClassifier.feed(IMUSample, player)` and `reset(player)`; Comms tests read `IMUSample`'s fields and both `player` arguments from the source | AI and Comms agree any change |
| Event route | QoS 1 MQTT on `game/gesture/<player>` | Comms owns transport; Game/Vis own consumption |

### Acceleration range

The firmware configures the MPU6050 for +/-4 g, where `1 g` is approximately `8192` counts.
`ai_api.py` decodes at `8192` counts/g as well, so a stationary `az` near `8192` reaches the model
as about `9.81 m/s^2`. It used to divide by `16384`, which gave the model half the real
acceleration (`4.90 m/s^2` at rest). `test_ai_api_decodes_counts_at_the_ranges_the_firmware_sets`
reads both ranges from `firebeetle/imu.h` and fails if `ai_api.py` disagrees with them.

Do not use the current merged path as evidence of real gesture accuracy yet:
- The signed Q6.10 input clips acceleration above approximately `3.26 g`. AI decides whether
  that is acceptable for the model.
- The model on the board (`gesture_min_q.xsa`) was trained before Hardware's dataset existed,
  on a different dataset. Meaningful predictions need a model trained on Hardware's data, with
  live data prepared the same way: the same IMU settings and mounting, and the same gesture
  segmentation and resampling.

## What has and has not been verified

| Evidence | What it establishes | What it does not establish |
|---|---|---|
| 505 automated Comms tests passing on 16 Oct, including windowing and both sensor-trigger paths | Message validation, per-player histories, IMU-status filtering, gap reset, classifier invocation, event construction, and A/B wiring | Physical IMU behaviour, FPGA deployment, or phone receipt |
| 15 Oct AI-Comms Configuration B run: 1,214 triggers, events, and broker acknowledgements | Live FPGA inference and board-side MQTT publishing with the then-current synthetic sensor stream | Real gesture accuracy or phone rendering |
| [16 Oct merged laptop log](../../Comms/logs/2026-10-16/pipeline_board_broker_125107Z.log) and [board log](../../Comms/logs/board/2026-10-16/tcp_client_mqtt_125116Z.log): two nodes at about 100 Hz each, no IMUs attached | Merged firmware, BLE, TCP, real classifier startup, IMU fault rejection, ECHO events, and MQTT publishing work together | Real IMU samples entering the FPGA or a classified gesture reaching the Visualiser |

An MQTT `PUBACK` proves that the broker accepted a QoS 1 publication. It does not prove that a
phone subscribed, received the event, or rendered it. That boundary needs separate Visualiser
evidence when the phone is added.

## Remaining integration work

1. Align the +/-4 g firmware with AI preprocessing and confirm the model's usable input range.
2. Run both players with real MPU6050 readings through the FPGA and compare predictions with a
   trusted input/result set.
3. Replace `--sensor-trigger` with `--gesture-flag` once a glove has a real detector and
   `wk10_integration.py` has run on the board.
4. Map labels `"0"` to `"6"` to the final gesture/action names.
5. Replace random `mana_cost` when the game engine owns that value.
6. Confirm recovery after a real DMA timeout; the current API reports the error but does not
   reset the DMA or reload the overlay.
7. Integrate the Visualiser and verify phone-side receipt and rendering, not only broker
   acknowledgement.

For changes inside either boundary, follow the change guides in
[Hardware-Comms](hardware-comms.md#change-guide) and [AI-Comms](ai-comms.md#change-guide).
