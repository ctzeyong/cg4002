# Full pipeline: Hardware -> Comms -> AI -> Visualiser

This is the runbook for the merged `mvp-full-pipeline` branch. It explains how a player's hand
movement becomes an action in the Vis game, and how to run that path in Configuration B.

It does not replace the detailed [Hardware-Comms](hardware-comms.md) and [AI-Comms](ai-comms.md)
contracts, or the [Vis drop-in README](../../Comms/phone/comms-visualiser/README.md). Those
describe each integration boundary; this page shows how they work together.

> **Current status (20 Oct 2026):** Every hop has run, but not yet all together.
> - The gloves' gesture detector is compiled and checked in a simulation, not yet flashed onto a
>   glove with an MPU6050.
> - The board half has run on the real FPGA: recorded gestures replayed as packets, with the
>   gesture flag, from two players through the laptop relay and the tunnel to `--gesture-flag`.
> - Both iPhones have received hand-made events on the board's broker and spawned units.
>
> The first run with real gloves and the phones together is still to come.
>
> **12 Nov 2026, on `packet-20b`:** the SENSOR packet shrinks from 26 to 20 bytes by dropping the
> three unused reserved fields. Compiled, not yet flashed. Every run above used 26 bytes.

## What is connected

```text
One MPU6050 per glove
  every reading, 500 Hz, through the chip's FIFO
       |
       | I2C
       v
FireBeetle firmware
  Hardware's gesture detector sees every reading
  every 10 ms: the newest reading, IMU status in reserved[0],
  gesture flag in reserved[1], sequence number and CRC
       |
       | encrypted BLE, 20-byte SENSOR packets
       v
Laptop Comms relay
  validates and decodes packets, identifies the player,
  adds the laptop receive time, and forwards framed JSON
       |
       | TCP through the reverse SSH tunnel
       v
Ultra96 Comms client (--gesture-flag)
  drops repeated, late and no-IMU samples; resets a player after a break;
  passes every other sample to AI, marking the end of each flagged gesture
       |
       | GestureClassifier.feed(IMUSample(...), player)
       v
AI/board/wk10_integration.py + FPGA (baseline_improved.xsa)
  per-player rolling 150-sample window; at the end of a gesture
  returns a gesture name and confidence
       |
       v
Ultra96 Comms client
  builds an AI_EVENT and publishes it at QoS 1;
  sends a VIBRATE back to that player's glove
       |
       | MQTT topic game/gesture/<player>, on the board's own broker
       v
Each iPhone (Vis app + Comms drop-in)
  SSH tunnel to the board's broker; the hook acts on its own player's
  topic as if the gesture had been drawn on the screen
```

This page uses **Configuration B**, where the Ultra96 publishes directly to its own loopback
broker. The laptop has no MQTT role in this configuration. Configuration A is still supported,
but it is not the path used for full-pipeline integration.

## Run the pipeline

Complete the one-time [Configuration B setup](../subsystem/comms/README.md#2-run-configuration-b)
first. In particular:

- prepare the laptop virtual environment and `.env` tunnel settings;
- prepare the Ultra96 virtual environment and SSH access; and
- run Mosquitto on the Ultra96. The board launcher warns if port `1883` is unavailable or is
  exposed beyond loopback.

### 1. Flash the gloves

Flash [`firebeetle.ino`](../../Comms/firebeetle/firebeetle.ino) from the Arduino IDE onto each
glove, with `#define PLAYER_ID` set to that glove's player. **The committed value is `1`**, so
change it to `2` for player 2's glove. A board with no IMU, or another board with the same
`PLAYER_ID`, should be switched off: the laptop connects to whichever answers first.

With the Serial Monitor open at 115200, you should see:

```text
[BLE] boot: B16 Player-1 peripheral
[MOTOR] ready on GPIO 2
[IMU] gesture model 342c4355152c26f4
[IMU] ready
[BLE] advertising as B16-Player1
```

`[IMU] not found` means the MPU6050 did not answer on I2C address `0x68`, SDA 21 / SCL 22. The
glove still streams, but every packet says the IMU is missing and no gestures are classified.

A glove that has never connected to this laptop pairs by itself on the first connection. Only a
board that has paired before and was then flashed with a different `PLAYER_ID` needs removing in
Windows Bluetooth settings first.

### 2. Start the laptop, then the board

Open two PowerShell terminals in `team/Comms`. Start the laptop side first, so the TCP server and
reverse tunnel are ready:

```powershell
# Terminal 1: gloves -> BLE -> laptop -> TCP tunnel (both players)
.\scripts\run_pipeline.ps1
```

For one player, add `--nodes 1`. Without it, the laptop keeps scanning for the missing glove,
which slows the connected glove's reconnects.

Then deploy the current Comms board code, AI's module and its bitstream, and start the board
client:

```powershell
# Terminal 2: TCP -> AI on the FPGA -> board-local MQTT broker
.\scripts\run_board.ps1 --sync --gesture-flag
```

`--sync` copies every `Comms/ultra96/*.py` file and AI's model files (`wk10_integration.py` and
`baseline_improved.xsa`) into `~/comms/` on the board, then restarts the client. Without `--sync`,
the launcher attaches to a board client that is already running, and that client keeps the
arguments it was started with. So use `--sync` whenever you change files or flags.

`--gesture-flag` turns classification on. Without it, the board makes events only from laptop
keypresses (below).

Useful board commands are:

```powershell
# Stop the detached Configuration B client
.\scripts\run_board.ps1 --stop

# Copy today's UTC board logs into Comms/logs/board/<date>/
.\scripts\run_board.ps1 --fetch-logs
```

### All three panes at once: run_all.ps1

`scripts/run_all.ps1` opens the relay, the board client, and a read-only status dashboard as
three panes in one Windows Terminal window, instead of the two terminals above:

```powershell
cd team\Comms
.\scripts\run_all.ps1 --sync --gesture-flag
```

The dashboard pane starts first and waits for the other two to log in before splitting its
window, so the ssh-agent isn't asked for two logins at once. Ctrl-C in the dashboard pane stops
all three, each as if Ctrl-C had been pressed in its own pane; closing the dashboard pane stops
only the dashboard. Each pane still runs the same command as the two-terminal setup above, so
any one of them can be closed and restarted on its own without touching the other two.

**Arguments typed after `run_all.ps1` go to the board pane only**, not the relay — the same
forwarding rule as `run_board.ps1` itself. To change the relay's own flags (for example
`--nodes 1` for a one-glove run), start `run_pipeline.ps1` directly in its own pane instead of
through `run_all.ps1`.

| Flag | Passed to | Effect |
|---|---|---|
| `--sync` | `run_board.py` | Re-copy `ultra96/*.py` and AI's model files to the board, then restart the client. Use on the first run after any change under `ultra96/` or to AI's module/bitstream. |
| `--stop` | `run_board.py` | Stop the Configuration B client on the Ultra96 and exit; does not start the panes. |
| `--fetch-logs [YYYY-MM-DD]` | `run_board.py` | Copy that UTC day's board logs into `Comms/logs/board/`, then exit (default: today). |
| `--port N` | `run_board.py` | Override `TUNNEL_PORT` for this run. |
| `--gesture-flag` | `tcp_client_mqtt.py` | Turn classification on: pass every SENSOR packet to AI and emit an AI event for each gesture the glove flags. Without it, the board only makes events from laptop keypresses. |
| `--event-every N` | `tcp_client_mqtt.py` | Publish a dummy AI event every `N` seconds, with no ECHO or gesture trigger. MQTT-only diagnostic, not a substitute for `--gesture-flag`. |
| `-v`, `--verbose` | `tcp_client_mqtt.py` | Log every packet (the per-packet DEBUG dump; see `notes/logging.md`). |
| `--host`, `--broker-host`, `--broker-port` | `tcp_client_mqtt.py` | Override the TCP tunnel endpoint or the board's broker address/port the client connects to. Defaults match the tunnel and the loopback broker; rarely needed. |

`--sync`, `--stop`, `--port`, and `--fetch-logs` are `run_board.py`'s own flags; everything else
is forwarded on to `tcp_client_mqtt.py` unparsed, so a flag `tcp_client_mqtt.py` adds later works
here too without an update to this table. `--ai-h5` (local Keras model testing before a bitstream
exists, see [`Comms/README.md`](../../Comms/README.md#testing-the-model-in-the-comms-pipeline-laptop-before-bitstream))
is Configuration A's `tcp_client.py` only and does not apply to `run_board.ps1` / `run_all.ps1`.

### 3. Connect the phones

Follow the drop-in's [bring-up steps](../../Comms/phone/comms-visualiser/README.md#4-bring-up-every-launch):
both iPhones must show `MQTT_CONNECTED` before anyone selects a player. In VsPlayer, **glove N's
player selects Player N** on their phone. Player 1's phone is the host, where the match runs.
Keep the crosshair on your own half: units need valid aim, the same as a drawn stroke.

### What a healthy run should show

Terminal 1, for each glove: `node N: subscribed`, then once a second
`node N: n=… rate=~100 Hz … missed=0`, and no lasting `IMU missing` warning.

Terminal 2 should first identify AI's model on the real FPGA. `StubAI` instead means the client
found no FPGA:

```text
classifier: AI
```

It also reports each glove's status, and publishes it to the phones on `status/gloves`. A glove
is `ready` once the board receives at least 50 packets a second from it with an IMU reading, and
`down` again only below 25 a second:

```text
gloves: player1=down player2=down
gloves: player1=ready player2=down
```

Each gesture the glove flags should then produce:

```text
trigger node=1 seq=... t_laptop_rx=... t_rx=...
AI event seq=... player=1 key=None gesture=warrior t_event=... conf=... t_infer_ms=... missing=... -> game/gesture/1
VIBRATE node=1 pattern=1 sent
published ultra96-relay:... to game/gesture/1 t=...
```

On the phone that selected that player, the game's feedback banner shows `glove -> Warrior`.
A red banner still means the event arrived; the game refused it, for mana or aim.

If a flagged gesture gets no answer from AI, the board says so instead:

```text
no AI event: node 1's flagged gesture of N samples gave no result
```

The usual cause is that AI's window was not yet full. It needs 150 samples (1.5 s) after the
board client connects, and after anything that resets the player (below).

If nothing at all appears on terminal 2 when you gesture, check the glove's serial output. Every
5 s it prints the detector's counts:

```text
[IMU] detector: readings=~2500 decisions=~100 max_score=... gestures started=... ended=... rejected=..., fifo overflows=0 stalls=0, detector gaps=0, clipped=...
```

`readings` near 2,500 means the FIFO works. `gestures started` and `ended` rising when you
gesture means the detector fires.

### Laptop keypresses

A letter or digit typed into terminal 1 travels to the glove as a CMD packet and back as an ECHO,
then on to the board, which publishes a made-up event without asking AI. `w m f r l h s` pick
warrior, mage, fireball, rogue, lightning, hero and shield. `1` or `2` goes to that player only;
any other key goes to every glove. Use it to check the phone half without gesturing.

## How a SENSOR packet becomes an event

1. [`imuService()`](../../Comms/firebeetle/imu.cpp) runs on every pass of `loop()`. It reads every
   500 Hz reading from the MPU6050's FIFO and gives each to Hardware's detector
   (`gesture_core.h`, `segments.h`, `model_data.h`, copied unchanged from
   `Hardware/Data Collection/esp32_continuous_pipeline_test/`).
2. Every 10 ms, [`firebeetle.ino`](../../Comms/firebeetle/firebeetle.ino) sends the newest reading
   as six raw counts, the [`ImuStatus`](../../Comms/firebeetle/imu.h) in `reserved[0]` and
   `gestureFlag()` in `reserved[1]`: `1` while the detector's segmenter is in a gesture, `2` once
   on the packet after a gesture the segmenter rejected, else `0`. Only status `IMU_OK` (`1`)
   represents a real sensor reading.
3. [`ble_client.py`](../../Comms/laptop/ble_client.py) checks the packet, obtains the player from
   the BLE connection, and adds `timestamp = time.perf_counter()` when the notification is
   received. This timestamp is a laptop receive time, not the time of the physical measurement.
4. [`pipeline_board_broker.py`](../../Comms/laptop/pipeline_board_broker.py) forwards the decoded
   message to the Ultra96 over TCP. It does not publish MQTT messages.
5. [`gesture.Windows`](../../Comms/ultra96/gesture.py) applies the rules below to each player,
   then passes every remaining sample to AI's `feed(IMUSample(...), player)`: `ax`..`gz`,
   `end_of_gesture` and `timestamp`. `end_of_gesture` is `True` on the first sample after the
   flag drops from 1 to 0. A drop to 2 means the glove rejected the gesture, so it never ends and
   AI never classifies it.
6. [`wk10_integration.py`](../../AI/board/wk10_integration.py) converts the counts to m/s² and
   rad/s and appends them to that player's 150-sample window. On `end_of_gesture` it quantises
   the window (900 inputs), runs the FPGA accelerator, and returns a gesture name and confidence.
   Its contract is [`README_gesture_stream_sim.md`](../../AI/board/README_gesture_stream_sim.md).
7. [`ai_events.make_event()`](../../Comms/ultra96/ai_events.py) places the name in
   `AI_EVENT.gesture`. [`tcp_client_mqtt.py`](../../Comms/ultra96/tcp_client_mqtt.py) wraps the
   event in the envelope and publishes it to `game/gesture/<player>` through the board's local
   broker. It then sends `{"type": "VIBRATE", "node": <player>, "pattern": 1}` down the tunnel,
   which the laptop turns into a CMD for that glove.
8. On each iPhone, Vis's `GloveGestureReceiver` subscribes to its own player's topic and runs
   the name through `GestureBindings`, the same path as a card. `MqttConnection` drops repeated
   `event_id`s first. The drop-in's `AiGestureHook` doesn't act on gestures. It only echoes each
   one on `viz/echo/<player>` and shows the gloves' status.

The published payload contains `type`, `player`, `seq`, `gesture` and `timestamp`. Confidence,
inference time and missing-packet count are log-only. `gesture` is one of AI's seven role names,
which the Vis game takes: `warrior`, `mage`, `fireball`, `rogue`, `lightning`, `hero`, `shield`.
Until AI's new model is on the board, the classifier still gives the old names (`arc`, `circle`,
...), which the game ignores, with nothing on screen. Keypress events already use the new names.

## Rules that protect the AI input

The Ultra96 applies these rules separately to each player:

- Each sample sent to AI has only `ax`, `ay`, `az`, `gx`, `gy` and `gz`, plus `end_of_gesture`
  and `timestamp`.
- Only packets with `reserved[0] == IMU_OK` reach AI. Missing, recovering, old-firmware,
  unknown, or absent status resets that player instead of joining readings across an outage.
- Malformed, repeated, and backwards SENSOR messages do not reach AI.
- Forward sequence gaps are recorded as missing packets but do not invent replacement samples.
- If more than `0.5 s` passes between a node's laptop receive timestamps, that player's state is
  cleared, and AI is told to forget its window, before the next sequence is checked. This covers
  a BLE reconnect, FireBeetle reboot, or long stream stall without changing the packet protocol.
- A TCP reconnection clears every player's window in AI. The already-loaded model is reused
  across those sessions.
- A DMA stall in AI logs a warning and skips that gesture, and packet reception keeps running.

At 100 Hz, a `0.5 s` break already represents about 50 absent samples, so the old window is no
longer a continuous record of the hand.

## Behaviours to know about

- **The flag ends after the gesture.** Hardware's detector raises the flag about 300 ms after
  the movement starts and drops it 150–200 ms after it ends. AI's 150-sample window ends at that
  drop, so it holds the gesture plus the moments before and after.
- **A gesture the glove rejects never reaches AI.** The detector rejects a candidate that runs
  over 6 s (about 5.7 s after the flag rises), or a short one just after an IMU restart. The glove
  then sends `2` instead of `0` and buzzes twice, and the client pane logs
  `no AI event: node N's glove rejected its flagged gesture of K samples`. Every other drop of the
  flag asks AI for an answer, and AI's confidence threshold is `0.7` (`CONF_THRESHOLD` in
  `wk10_integration.py`). Compare the glove's `ended` and `rejected` counts with the board's events.
- **The glove buzzes on its own as well as on a VIBRATE.** The motor on GPIO 2 gives one 200 ms
  pulse when a VIBRATE CMD arrives and prints `[MOTOR] VIBRATE seq=...` on serial. It also gives
  one 150 ms pulse when the flag rises, and two 150 ms pulses 500 ms apart on a `2`. A new pattern
  cuts off the one playing. The firmware still echoes a VIBRATE, which the laptop logs as
  `node N: VIBRATE seq=... echoed, not forwarded`. The start and reject buzzes are compiled but not
  yet flashed. GPIO 2 is an ESP32 strapping pin, so check the glove still boots and flashes with
  the motor driver attached.
- **Inference holds the board's event loop for about 23 ms**, mostly two fixed 10 ms sleeps in
  AI's DMA wait.

## Retired: `--sensor-trigger`

The older `--sensor-trigger` mode, which classified a fixed 100-sample window at random packets
through `ai_api.py`, was removed on `full-pipeline`. The board clients now refuse that flag.

## Contracts that must agree across teams

| Contract | Current code | Owner/action |
|---|---|---|
| Sensor layout | Six raw `int16` counts: `ax, ay, az, gx, gy, gz` | Hardware and Comms must preserve order and meaning |
| Sensor rate | IMU 500 Hz, all of it to the detector; 100 Hz packets; AI window 150 samples | Hardware, Comms, and AI coordinate any change |
| IMU validity | `reserved[0] == 1` means real data | Hardware defines status; Comms enforces it |
| Gesture flag | `reserved[1]` is `1` while Hardware's segmenter is in a gesture, and `2` once after a gesture it rejected. A Comms test checks the board's values against `firebeetle/imu.h` | Hardware owns the detector; Comms carries the flag |
| Gyroscope range | Firmware: +/-500 degrees/s; AI: `65.5` counts/(degree/s) | Currently aligned |
| Accelerometer range | Firmware: +/-4 g and `8192` counts/g; AI: `8192` counts/g | Aligned; a Comms test checks `wk10_integration.py` against `firebeetle/imu.h` |
| AI deployment | `gesture.AI_BITSTREAM` and `run_board.py` both name `baseline_improved.xsa` | AI and Comms update together |
| Model input | All of it is inside `wk10_integration.py` | AI owns implementation; interface changes require coordination |
| Per-sample API | `GestureClassifier.feed(IMUSample, player)` and `reset(player)`; Comms tests read `IMUSample`'s fields and both `player` arguments from the source | AI and Comms agree any change |
| Gesture names | AI's `LABEL_MAP`, lowercase; the hook maps them onto Vis's `GestureSymbol` enum | AI and Vis keep the seven names equal |
| Event route | QoS 1 MQTT on `game/gesture/<player>` | Comms owns transport; Game/Vis own consumption |

### Acceleration range

The firmware configures the MPU6050 for +/-4 g, where `1 g` is approximately `8192` counts. AI's
`wk10_integration.py` decodes at `8192` counts/g, so a stationary `az` near `8192` reaches the
model as about `9.81 m/s^2`. The retired `ai_api.py` once divided by `16384`, which gave the model
half the real acceleration (`4.90 m/s^2` at rest). `test_ais_model_decodes_counts_at_the_ranges_the_firmware_sets`
reads both ranges from `firebeetle/imu.h` and fails if `wk10_integration.py` disagrees with them.

Do not treat a live run as evidence of real gesture accuracy yet:
- The signed Q6.10 input clips acceleration above approximately `3.26 g`. AI decides whether
  that is acceptable for the model.
- The board's model, `baseline_improved.xsa`, was trained on gestures resampled to 100 steps.
  Live, the window holds raw 100 Hz samples, so a short gesture fills less of it and a gesture
  longer than 1.5 s loses its start. AI's newer model trains on raw 100 Hz gestures, which matches
  the live stream, and replaces it once AI makes its `.xsa`.

## What has and has not been verified

| Evidence | What it establishes | What it does not establish |
|---|---|---|
| 588 automated Comms tests passing on 20 Oct, including both classification paths, the VIBRATE return and the detector copy check | Message validation, per-player state, IMU-status filtering, gap reset, AI invocation, event construction, VIBRATE routing, A/B wiring | Physical IMU behaviour, FPGA deployment, or phone receipt |
| [16 Oct merged laptop log](../../Comms/logs/2026-10-16/pipeline_board_broker_125107Z.log) and [board log](../../Comms/logs/board/2026-10-16/tcp_client_mqtt_125116Z.log): two nodes at about 100 Hz each, no IMUs attached | Firmware, BLE, TCP, real classifier startup, IMU fault rejection, ECHO events, and MQTT publishing work together | Real IMU samples entering the FPGA or a classified gesture reaching the phones |
| 20 Oct simulation of the detector firmware (the real `imu.cpp` and Hardware's headers against a simulated MPU6050 with a FIFO) | FIFO handling, the flag matching a reference detector on every OK packet, restarts after faults, 100 Hz packets | The real chip, the detector's thresholds on our gloves, real BLE timing |
| 20 Oct board run with recorded gestures replayed as packets for two players, through the laptop relay and tunnel to `--gesture-flag` | AI's module and `baseline_improved.xsa` on the real FPGA: 60 of 60 gestures gave an event, 59 correct (training data), inference median 23 ms | Live gloves, or accuracy on new data |
| 20 Oct, both iPhones: hand-made events on `game/gesture/1` and `/2` each spawned one unit on the right side | The phone tunnel, the hook, and `PerformGesture` on both phones | Events from the classifier, and the other six gestures |

An MQTT `PUBACK` proves that the broker accepted a QoS 1 publication. It does not prove that a
phone subscribed, received the event, or acted on it. The game's feedback label on the phone is
that evidence.

## Remaining integration work

1. Flash both gloves and run the whole path with real gestures and both phones.
2. Measure live accuracy once AI's newer model is on the board (see "Acceleration range").
3. Decide whether a rejected candidate should still be classified, and set AI's confidence
   threshold.
4. Flash the motor code and check each player's glove buzzes after its own gestures only.
5. Vis imports the updated hook and rebuilds both iPhones (drop-in README, "Updating your
   existing copy").
6. Confirm recovery after a real DMA timeout; AI's module reports the error but does not reset the
   DMA or reload the overlay.

For changes inside a boundary, follow the change guides in
[Hardware-Comms](hardware-comms.md#change-guide) and [AI-Comms](ai-comms.md#change-guide).
