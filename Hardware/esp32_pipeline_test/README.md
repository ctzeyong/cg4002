# ESP32 hardware-to-comms integration guide

This guide describes the current [esp32_pipeline_test.ino](esp32_pipeline_test.ino) and the changes needed to connect it to the laptop relay and Ultra96 AI. The BLE contract below is implemented by this sketch; it is a **pipeline-test contract, not a finalized team BLE specification**. Agree on any changes on both ends before replacing it.

The simplest integration is to flash this sketch as the ESP firmware and adapt the existing communications software to its packets. It already combines MPU6050 acquisition, an embedded decision-tree forest, gesture segmentation, BLE, and nonblocking motor vibration. Do not flash the dummy Comms sketch as well, or place both sketches in one Arduino folder.

## 1. What already works in the code, and what remains

```text
MPU6050 FIFO at 500 Hz
  -> ESP decision-tree detector + segmenter -> local start/end vibration
  -> every fifth raw sample -> BLE SENSOR at 100 Hz
  -> laptop -> TCP -> Ultra96 rolling AI window
  -> EOG sample triggers symbol classification
  -> result over TCP -> laptop's player-specific BLE CMD queue
  -> ESP validates gesture ID -> good/invalid vibration -> ECHO acknowledgement

Ultra96 -> MQTT -> phone remains a separate output for the game event.
```

| Responsibility | Current implementation / work needed |
|---|---|
| Read one MPU, detect gesture boundaries, drive motor | Implemented in this sketch |
| BLE service, sensor packets, result command validation | Implemented in this sketch |
| Laptop BLE discovery, pairing, notifications, CMD writer | Reuse [ble_client.py](../../Comms/laptop/ble_client.py) |
| Decode EOG, ESP timestamp and gesture ID | Add to [ble_packet.py](../../Comms/laptop/ble_packet.py) |
| Feed real AI instead of dummy events | Integrate [wk10_integration.py](../../AI/board/wk10_integration.py) into the board receive path |
| Return AI results to the ESP | Add board TCP result messages and replace Configuration B's downlink discard path |
| Loss, reconnect, result retry handling | Complete across the relay and AI; ESP handling alone is insufficient |

**Source-of-truth corrections:** the sketch's executable default is `PLAYER_ID=1`, although its introductory comment says 2. Its actual `SHORT_DUTY=180` and `LONG_DUTY=170`, although that comment says 150/135. This README follows the constants. The separate [data-collection pipeline sketch](../Data%20Collection/esp32_continuous_pipeline_test/README.md) is a different integration path; its captured `100 x 6` tensor is not the wire format used here.

## 2. Flashing and hardware setup

1. Open `esp32_pipeline_test.ino` inside this matching Arduino folder. Keep one sketch entry point, with one `setup()` and one `loop()`.
2. Install/select the appropriate ESP32 board support and NimBLE-Arduino **2.x**. The sketch includes PWM branches for ESP32 Arduino core 2.x and 3.x. Confirm compilation with the team's selected versions and board.
3. Set `PLAYER_ID` to 1 or 2 before flashing each device; do not use the same player ID for both.
4. Connect MPU6050 SDA to GPIO21, SCL to GPIO22, with address `0x68`, suitable power, and common ground. Motor driver input is GPIO2. Use the existing driver circuit; do not connect the motor directly to the GPIO.
5. Open Serial Monitor at **115200**. Expect `READY,B16-Player1,...` (or Player2). `ERROR,MPU_NOT_FOUND`, `ERROR,IMU_CONFIG`, or `ERROR,MOTOR_PWM` must be resolved before BLE integration.

The code uses `Wire` directly: no separate MPU6050 Arduino library is required. It configures 400 kHz I2C, a 500 Hz FIFO stream, accelerometer range +/-4 g, gyro range +/-500 degrees/s, and six channels ordered `ax, ay, az, gx, gy, gz`. FIFO values are decoded from the MPU's big-endian bytes and serialized as little-endian BLE values.

## 3. Preserve the hardware pipeline when merging firmware

If the comms firmware must be the final base, port these components together rather than copying only the BLE send call:

| Sketch component | Preserve / integration point |
|---|---|
| `MODEL` snapshot, `TreeNode`, `TREES`, thresholds | Eight-tree forest, model ID `342c4355152c26f4` |
| `gesture::Frontend`, `Detector`, `Segmenter` | Feature extraction, timing, reset logic and segment state |
| `Firmware::begin`, `resetFifo`, `acquire` | MPU configuration, FIFO draining and sample clock |
| `Firmware::processSample` | Detector input, start/end events, raw sample decimation and packet metadata |
| `Motor` and `localPulse` | Queued PWM patterns serviced by `motor.update()` |
| `Protocol`, `processCommand`, result slots | CRC, result validation, duplicate suppression and acknowledgements |
| BLE callbacks, `updateConnection`, `enqueue`, `transmit` | Session tracking, encrypted/subscribed readiness, bounded transport queues |

Keep acquisition and motor updates running independently of successful BLE transmission. BLE callbacks should validate/copy a command into the FreeRTOS queue; the main loop handles it. Do not perform inference, long delays, or motor pulse loops in a BLE callback. Do not add a second `Wire.begin`, PWM owner, or BLE server without reconciling the existing ownership.

### Detector timing is different from transmitted data

- Acquisition and `detector.push()` run at **500 Hz**, with a 2,000 us sample period. Changing only the BLE rate or loop delay does not safely change the model's input rate.
- For detection only, counts become g (`/8192`) and degrees/s (`/65.5`). The frontend averages groups of five, uses a 30-step window at 100 Hz, and computes 42 features every five steps (50 ms) after warm-up. Preserve its additional feature scaling and feature order.
- The forest estimates gesture activity; it does **not** classify circle, star, etc. The Ultra96 performs symbol classification.
- `Segmenter::step()` starts at score >= 0.5. Completion requires a motion-energy dip held for 150 ms or score < 0.2 held for 200 ms. It also handles minimum/maximum duration, rearming and timeout rejection. Use its `SEG_*` events, not the legacy detector `Gate` actions, for symbol boundaries.
- A start increments `gestureId` and queues one short pulse. Completion queues two short pulses and sets EOG on one outgoing sample. Rejection/timeout queues two short pulses but **does not set EOG**.
- BLE sends every fifth **raw** sample, continuously, including background and the end-confirmation tail. It does not send the averaged detector samples, normalized features, or a resampled gesture tensor.

The embedded forest is provisional and did not meet validation targets. Preserve it for initial integration, then validate/replace the model separately. Also test whether motor vibration contaminates the sensor signal.

## 4. BLE connection contract

| Setting | Player 1 | Player 2 |
|---|---|---|
| Device name | `B16-Player1` | `B16-Player2` |
| Service | `b1160001-0000-4000-8000-000000000001` | `b1160001-0000-4000-8000-000000000002` |
| SENSOR characteristic | `b1160002-0000-4000-8000-000000000001` | `b1160002-0000-4000-8000-000000000002` |
| CMD characteristic | `b1160003-0000-4000-8000-000000000001` | `b1160003-0000-4000-8000-000000000002` |

The laptop is the BLE central; the ESP is the peripheral. SENSOR supports read/notify with encrypted reads; CMD supports encrypted writes. The sketch requests bonding and Secure Connections with no input/output and no MITM requirement. Streaming starts only when the connection is encrypted, notifications are subscribed, and negotiated MTU is **at least 29** (26-byte packet plus 3). Preferred MTU is 128. After encryption, the ESP requests a 15 ms connection interval; verify actual throughput on the laptop.

Both SENSOR and ECHO notifications arrive on the SENSOR characteristic. Dispatch by packet type. Use the existing CMD writer with `response=True`; a successful GATT write is not proof that the result was accepted by the application. Inspect ECHO status too.

## 5. SENSOR packet: ESP to laptop

Exactly **26 bytes**. All multibyte fields are little-endian.

| Byte offsets | Type | Meaning |
|---|---|---|
| 0 | uint8 | `0x01` SENSOR |
| 1 | uint8 | Sensor sequence, wraps 255 -> 0 |
| 2..13 | six int16 | Raw `ax, ay, az, gx, gy, gz` counts |
| 14..15 | uint16 | `end_of_gesture`: 1 on the completion sample, otherwise 0 |
| 16..19 | uint32 | ESP sample timestamp in milliseconds since boot |
| 20..23 | uint32 | Gesture ID; increments at start, initially 0 |
| 24..25 | uint16 | CRC-16/CCITT-FALSE over bytes 0..23 |

CRC parameters are polynomial `0x1021`, initial value `0xffff`, no reflection, no final XOR. Existing `ble_packet.decode()` validates packet length and CRC. Its returned payload excludes the two-byte header **but still includes the CRC**.

Add metadata decoding to `packet_to_message()` for SENSOR packets only. With the current signed `unpack_fields()` result:

```python
fields = unpack_fields(packet.payload)
eog = fields[6] & 0xffff
if eog not in (0, 1):
    raise ValueError("invalid EOG")
esp_timestamp_ms = (fields[7] & 0xffff) | ((fields[8] & 0xffff) << 16)
gesture_id = (fields[9] & 0xffff) | ((fields[10] & 0xffff) << 16)
message.update(end_of_gesture=bool(eog),
               esp_timestamp_ms=esp_timestamp_ms,
               gesture_id=gesture_id)
```

Keep the existing relay arrival `timestamp` separately. Do not interpret the ESP timestamp as Unix time, or overwrite the arrival timestamp used for latency measurements. It wraps after approximately 49.7 days. Preserve raw integer IMU counts through TCP. `node` comes from the BLE connection, not the packet body.

Gesture ID is not an active/inactive flag: it remains the same during background after completion until the next start. Only EOG triggers inference. The packet does not carry exact segment start/end timestamps or an abort flag; EOG timestamp is the completion decision time, including the confirmation tail.

## 6. Laptop and Ultra96 changes

Apply these in order:

1. **Decode metadata** in [ble_packet.py](../../Comms/laptop/ble_packet.py), preserving its existing fields for compatibility. Keep SENSOR sequence tracking separate from ECHO, which reuses the command sequence.
2. **Disable dummy input/output paths.** Remove/disable `keystroke_reader()` in [pipeline_board_broker.py](../../Comms/laptop/pipeline_board_broker.py). Do not use `--cmd-every`, `--event-every`, or `--sensor-trigger` for the real pipeline. Replace the dummy ECHO-trigger logic in [tcp_client.py](../../Comms/ultra96/tcp_client.py); Configuration B's [tcp_client_mqtt.py](../../Comms/ultra96/tcp_client_mqtt.py) imports that receiver too. Otherwise result acknowledgements will generate false game events.
3. **Feed every valid SENSOR sample to AI**, in order, with a separate rolling history for each player. The actual module is `AI/board/wk10_integration.py`; some of its comments still refer to the older name `gesture_stream_sim`. Arrange its deployment/import path explicitly.
4. **Return a correlated result** over the existing framed TCP connection, and publish the real game event through Configuration B's board MQTT publisher. Keep framing, heartbeat and reconnect handling.
5. **Replace `drain_downlink()`**, which currently discards board-to-laptop TCP messages. Validate each result, map its player to `outboxes[player]`, build a CMD, and call `ble_client.post_command()`. Maintain separate command sequence counters per player.
6. **Handle ECHO as an acknowledgement**, including retryable status and correlation, never as another inference request.

Conceptual AI adapter (after validating continuity and metadata):

```python
# Imports after putting the actual AI/board module on the Python import path.
from wk10_integration import IMUSample

# clf owns this player's rolling history; do not mix player samples.
result = clf.feed(IMUSample(
    **msg["imu"],
    end_of_gesture=msg["end_of_gesture"],
    timestamp=msg["esp_timestamp_ms"] / 1000.0,
))
# Keep node, connection epoch and gesture_id alongside this inference request.
# They are not fields in GestureResult and must be attached by the adapter.
```

The AI uses a **150 x 6 rolling window**, corresponding to 1.5 seconds at 100 Hz. Feed background too; do not reset after ordinary successful gestures. Pass raw counts to `feed()` because it already converts acceleration to m/s^2 and gyro to rad/s. Do not convert twice. After reconnect or data loss, call `reset()` and require a fresh full window before accepting an EOG result.

`feed()` returns `None` for non-EOG samples, an incomplete window, or confidence rejection. Do not equate all `None` returns with INVALID. The current `CONF_THRESHOLD=0.0` disables confidence rejection, so agree with the AI owner how GOOD versus INVALID is decided and expose a reason for rejected inference. Transport loss/incomplete input should be logged as skipped inference, rather than silently reported as a bad user gesture.

For two players, isolate histories while serializing access to the shared FPGA/DMA backend. Creating two real `GestureClassifier` objects loads the overlay twice; arrange shared hardware ownership with the AI owner. Run inference through an ordered worker so it does not block BLE intake or TCP heartbeats, and bound its backlog.

Suggested TCP result schema (**new adapter contract, not implemented yet**):

```json
{"type":"GESTURE_RESULT","player":1,"stream_epoch":7,"gesture_id":42,"result":"GOOD_SYMBOL"}
```

The relay should assign a per-player stream epoch and forward it with samples; the board echoes it in results. Increment/invalidate it on reconnect or stream reset, and reject results for an old epoch. This prevents delayed results from matching IDs reused after an ESP reboot. The firmware's internal session counter is not present on the wire. Agree how reset events reach the board; sequence numbers alone cannot reliably identify every new session.

## 7. Result CMD and ECHO: laptop to ESP and back

CMD uses the same 26-byte envelope: type `0x03`, command sequence, eleven 16-bit words, then CRC.

| Word index | CMD content |
|---|---|
| 0 | `0x4701` GOOD_SYMBOL or `0x4702` INVALID_SYMBOL |
| 1 | Low 16 bits of gesture ID |
| 2 | High 16 bits of gesture ID |
| 3..10 | All zero |

The existing Python builder requires **signed** int16 fields. Convert ID words rather than passing unsigned values above 32767:

```python
from ble_packet import build_cmd_packet

def signed_word(value):
    value &= 0xffff
    return value if value < 0x8000 else value - 0x10000

def build_result_cmd(command_seq, gesture_id, good):
    if not 1 <= gesture_id <= 0xffffffff:
        raise ValueError("gesture ID must be a nonzero uint32")
    return build_cmd_packet(command_seq, [
        0x4701 if good else 0x4702,
        signed_word(gesture_id), signed_word(gesture_id >> 16),
        *([0] * 8),
    ])
```

The ESP tracks the last **16 completed gestures** in the current streaming session. It accepts only a matching completed ID; the first successfully queued result wins. ECHO uses type `0x04`, preserves command sequence/opcode/ID, replaces word 3 (bytes 8..9) with status, and recalculates CRC:

| Status | Meaning | Relay action |
|---|---|---|
| 0 | Motor pattern queued | Mark accepted; this does not mean vibration has finished |
| 1 | Already answered | Treat as acknowledged; no additional vibration |
| 2 | Unknown/stale gesture | Stop retrying; log/reset correlation as appropriate |
| 3 | Motor queue full | Bounded retry while the same session/result is still valid |
| 4 | Malformed opcode or nonzero reserved CMD bytes | Fix encoder; do not retry unchanged |

Wrong-length, bad-CRC, unencrypted or wrong-type writes are dropped before this status path. A full command queue can also drop a write without ECHO. For missing ECHO, retry the same logical result with bounded timeout/retries; never replay it across reconnect. Correlate by player, epoch, gesture ID and command sequence. Neither successful `notify()` nor a GATT write response proves application-level acceptance.

### Vibration behavior

| Event | Pattern |
|---|---|
| Local gesture start | 1 short pulse |
| Local completion or abort | 2 short pulses |
| Accepted GOOD_SYMBOL | 1 long pulse |
| Accepted INVALID_SYMBOL | 3 long pulses |

Short pulses last 150 ms at duty 180; long pulses last 500 ms at duty 170. PWM is 20 kHz, 8-bit. The gap between pulses/patterns is 500 ms. Patterns are queued, so result feedback can wait behind local start/end feedback. The queue has 16 waiting pattern slots. No `delay()` is used to play a pattern; acquisition continues while the motor runs.

## 8. Loss and reconnect rules to implement end to end

- Check SENSOR continuity modulo 256, including 255 -> 0. Also use ESP sample time (normally 10 ms between transmitted samples) because an 8-bit sequence can hide a whole 256-packet loss. Do not treat ECHO sequence as sensor sequence.
- Drop duplicates/out-of-order samples before feeding AI. On a gap, reset the affected player's history and invalidate pending work. Check continuity at the board as well, because relay queues can drop packets after BLE reception.
- ESP TX capacity is 64 packets, shared by SENSOR/ECHO. Failed notifications retry, but backlog older than 500 ms or overflow is discarded and pending result slots are cleared. EOG is not retransmitted with an application acknowledgement, so a lost EOG can mean no inference/result.
- FIFO/I2C faults reset detector/segmenter state and create a sensor sequence discontinuity. Log these instead of stitching samples across missing data.
- On a new BLE session, the ESP clears TX/results, resets the FIFO/detector and stops/clears the motor. Gesture IDs continue across BLE reconnects but restart after reboot. The relay must also purge stale samples, commands and inference results; its existing outbox purge is only part of this.
- Notification subscription/MTU readiness changes also reset streaming state. Do not assume a connected device is already producing usable samples.

## 9. Bring-up and acceptance checklist

Start with one player and test in stages before using the full network path.

1. **BLE only:** after preparing the Comms Python environment, from `Comms/` run `.\.venv\Scripts\python.exe laptop\ble_client.py --nodes 1`. Leave dummy CMD generation off. Expect `BLE,STREAMING,1` in ESP serial and approximately 100 SENSOR packets/s. This standalone receiver drains/logs samples; it does not perform AI.
2. **Decode:** verify raw axes, CRC, 10 ms sample-time progression, sequence wrap, and metadata values. Test ID/timestamp low words above `0x7fff` to catch signed reconstruction errors.
3. **Local detection:** perform a gesture. Observe `GESTURE,START`, one short pulse, then `GESTURE,END`, two short pulses, and exactly one EOG=1 packet for that ID. An `ABORT` must not trigger inference.
4. **Return path without AI:** after observing a real completed EOG, send a synthetic GOOD result for that ID through the result adapter. Expect ECHO status 0 and one long pulse. Repeat the same result: status 1 and no repeat vibration. Use a fresh completed ID to test INVALID (three long pulses). Test unknown ID and malformed command handling too.
5. **Real AI:** feed continuous samples, fill the 150-sample window, and confirm EOG causes one classification, one real game event, and one correlated result CMD. Confirm ECHO creates no game event. Validate the confirmation-tail timing against the AI training/window assumptions.
6. **Failures:** disconnect/reconnect, reset the ESP, force queue congestion, lose a sample/EOG, and submit a delayed result. Verify fresh AI windows, stale-result rejection and no replayed motor patterns. Exercise status 3 retries and lost-ECHO retries.
7. **Both players:** flash player IDs 1 and 2, run `--nodes 1,2`, and verify histories, IDs and result queues stay separate under simultaneous gestures.

After implementing the adapters, use [Comms/README.md](../../Comms/README.md) for the Configuration B tunnel and board launch steps. Those launchers currently run the dummy pipeline; launching them alone does not complete this integration.

Watch the ESP's ten-second `STATUS` report: `gaps`, `tx_drops`, `eog_drops`, `notify_failures`, `cmd_drops`, and `motor_drops`. Correlate logs by player, stream epoch and gesture ID. Hardware acceptance requires measured packet continuity and correct physical vibration, not just a successful BLE connection.

## 10. Decisions to settle with the team

- Keep this packet layout/UUID set or assign a versioned replacement; there is no version field in this packet.
- Define GOOD/INVALID with the AI owner, including confidence threshold and a distinct skipped-inference outcome.
- Agree on TCP result/reset messages, session epochs, retry limits and per-player queue ownership.
- Validate continuous 100 Hz / 150-step AI windows against real gesture durations and the 150/200 ms completion tail; long gestures can exceed the current AI history.
- Decide whether explicit start/abort events, reliable EOG delivery, hall-effect events or file transfer are required. This sketch implements SENSOR and result CMD/ECHO only.

This guide documents inspected source behavior and proposed adapter work. It does not claim that the combined firmware/relay/AI pipeline has been tested on hardware.
