# Hardware–Comms integration

This document explains how the single MPU6050 on each player's FireBeetle is connected to the
Comms firmware, the C++ interface shared by both sides, and what must change when either side
changes its implementation.

> **Current status (16 Oct 2026):** branch `hardware-comms` reads one MPU6050 per FireBeetle,
> reports sensor faults, and keeps the existing 26-byte SENSOR packet. A board with no IMU
> attached has been flashed and checked (see [Verification](#verification-and-current-evidence)).
> Real IMU readings and a two-player run are still to be tested.
>
> **20 Oct 2026, on `mvp-full-pipeline`:** Hardware's gesture detector runs in the firmware,
> reading every sample from the MPU6050's FIFO at 500 Hz. Compiled and checked in a host
> simulation, not yet flashed.

## Architecture overview

```text
One MPU6050 per player
  physical motion -> raw ax/ay/az/gx/gy/gz counts
       |
       | I2C
       v
Hardware-facing adapter: imu.cpp
  configures, reads, checks, and recovers the sensor
       |
       | imu.h: imuBegin(), imuRead(), ranges, and ImuStatus
       v
Comms firmware: firebeetle.ino
  schedules reads and adds player sequence, status, and CRC
       |
       | encrypted BLE: one 26-byte SENSOR packet, target 100 Hz
       v
Comms laptop client
  validates, decodes, reports faults, and forwards a structured message
```

The shared API is an **in-process C++ interface** compiled into the FireBeetle sketch. Comms does
not need to know MPU6050 registers or I2C operations, while the Hardware-facing driver does not
need to know BLE, TCP, or the rest of the Comms pipeline.

## Responsibilities and shared API

[`Comms/firebeetle/imu.h`](../../Comms/firebeetle/imu.h) is the boundary between Hardware and
Comms.

| Owner | Files or component | Purpose |
|---|---|---|
| Hardware | Physical MPU6050 and mounting | Provide one sensor per player with repeatable wiring, orientation, and calibration |
| Hardware | [`Hardware/cloud_manual_day1_int16/CLOUD_README.txt`](../../Hardware/cloud_manual_day1_int16/CLOUD_README.txt) | Define the six-channel raw-count encoding used by the Hardware dataset |
| Shared boundary | [`Comms/firebeetle/imu.h`](../../Comms/firebeetle/imu.h) | Define ranges, status values, and the two functions used by Comms |
| Hardware-facing adapter | [`Comms/firebeetle/imu.cpp`](../../Comms/firebeetle/imu.cpp) | Hide sensor-specific I2C setup, reading, health checks, and recovery behind `imu.h` |
| Comms | [`Comms/firebeetle/firebeetle.ino`](../../Comms/firebeetle/firebeetle.ino) | Schedule reads and pack values, status, sequence, and CRC into BLE notifications |
| Comms | [`Comms/laptop/ble_packet.py`](../../Comms/laptop/ble_packet.py) | Validate and decode the fixed packet and expose the IMU status |
| Comms | [`Comms/laptop/ble_client.py`](../../Comms/laptop/ble_client.py) | Track sequence loss, report IMU state changes, and forward valid packets |

Hardware owns the physical signal and sensor configuration. Comms owns when samples are requested
and how they are transported. The internal code on either side may change independently while
the `imu.h` contract and the meaning of its values remain unchanged.

### What Hardware provides to Comms

Hardware provides one physical MPU6050 per FireBeetle and six raw signed counts in this order:

```text
ax, ay, az, gx, gy, gz
```

Both players must use the same axis meaning, measurement ranges, and calibration policy. The
one-IMU design is final for this interface; the two-MPU6050 sketches under `Hardware/esp_test/`
are prototypes and are not runtime dependencies.

### What Hardware exposes to Comms

`imu.h` exposes only these symbols:

| Symbol | What Comms uses it for |
|---|---|
| `IMU_ACCEL_RANGE_G` | Compile-time accelerometer range; currently `4` |
| `IMU_GYRO_RANGE_DPS` | Compile-time gyroscope range; currently `500` |
| `ImuStatus` | Distinguish a real sample from a missing or recovering sensor |
| `imuBegin(uint8_t player)` | Start and check the sensor once during firmware setup |
| `imuService()` | Read the sensor and run Hardware's gesture detector, on every pass of `loop()` |
| `imuRead(int16_t out[6])` | Give the newest ordered sample and its status, once per packet |
| `gestureFlag(sample, status)` | 1 while Hardware's detector says the hand may be making a gesture, sent in `reserved[1]` |
| `imuPrintStats()` | Print the detector's counts to serial, every 5 s |

The call used by Comms is:

```cpp
if (!imuBegin(PLAYER_ID)) {
    // BLE still starts; SENSOR packets report IMU_MISSING.
}

imuService();                      // first thing in every loop() pass

int16_t values[6];
ImuStatus status = imuRead(values);  // once per 10 ms packet
int16_t flag = gestureFlag(values, status);
```

The player ID is already passed to `imuBegin()` so per-player calibration can be added inside the
adapter without changing the call site or BLE packet.

### Input contract

The current physical and acquisition assumptions are:

| Property | Current contract |
|---|---|
| Sensor count | One MPU6050 per FireBeetle |
| I2C address | `0x68` (`AD0` low) |
| FireBeetle pins | SDA GPIO 21, SCL GPIO 22; vibration motor driver GPIO 2 |
| I2C clock | 400 kHz |
| Accelerometer | ±4 g, `8192` counts/g |
| Gyroscope | ±500 °/s, `65.5` counts/(°/s) |
| Digital low-pass filter | 42 Hz |
| MPU6050 internal rate | 500 Hz |
| FireBeetle read rate | Every 500 Hz sample, from the MPU6050's FIFO, for the gesture detector |
| Packet rate | 100 Hz, the newest sample every 10 ms |

The sensor writes every sample into its FIFO at 500 Hz. `imuService()` reads all of them for
Hardware's gesture detector, which needs every one exactly 2 ms apart. Only the newest is sent in
each 10 ms packet; the others are not transmitted. Raw counts cross the boundary unchanged: Comms
does not scale, reorder, or convert them to physical units.

**The gesture detector** is Hardware's `gesture_core.h`, `segments.h` and `model_data.h`, copied
unchanged from `Hardware/Data Collection/esp32_continuous_pipeline_test/` into
`Comms/firebeetle/`. `gestureFlag()` returns its segmenter's "in a gesture" state. If the files are
updated in Hardware's folder, copy them across again: `laptop/tests/test_firmware_detector.py`
fails until the two match, and the sketch refuses to compile with the untrained placeholder model.

### Output, timing, and failure contract

`imuRead()` always fills all six outputs and returns one `ImuStatus`:

| Value | Name | Six output fields | Meaning |
|---:|---|---|---|
| `0` | `IMU_UNREPORTED` | Unspecified | Compatibility with firmware from before this status field |
| `1` | `IMU_OK` | Real raw counts | Successful sensor sample |
| `2` | `IMU_MISSING` | All zero | The sensor did not answer |
| `3` | `IMU_RECOVERING` | All zero | The readings restarted: the sensor was set up again, is still settling (100 ms), or its FIFO lost or stopped giving samples |

Each I2C operation uses a 5 ms timeout. `imuRead()` must remain fast enough for the 10 ms packet
period, must not halt the FireBeetle on failure, and must never report a missing sensor as
valid.

A sensor fault does not stop BLE. Comms continues transmitting sequenced packets with zeros and
an explicit status so a sensor failure remains distinguishable from a dead BLE connection. Any
consumer that needs a real sample must require `IMU_OK`.

## Current sampling and recovery behaviour

1. `imuBegin()` starts I2C and writes the required power, rate, filter, gyroscope-range, and
   accelerometer-range registers.
2. The adapter reads every setting back. A write is not treated as successful until all settings
   match.
3. After any setup the readings are ignored for 100 ms while the chip settles, as Hardware's
   sketch does, then the FIFO is emptied and the detector starts again.
4. A healthy `imuService()` reads the FIFO's count and then each 12-byte sample (big-endian, accel
   then gyro), at most 8 per pass. Each goes to the detector, and the newest is kept for
   `imuRead()`.
5. If the FIFO filled up (`loop()` held up over 170 ms) or gave nothing for 50 ms, the FIFO is
   emptied and the detector starts again. The next packet after an OK one then says
   `IMU_RECOVERING`, so nothing downstream joins samples from both sides of the gap.
6. Once per second, the adapter reads the settings back to detect a reset or unplugged sensor,
   and sets the sensor up again. A reset also stops the FIFO, so packets say `IMU_RECOVERING`
   rather than sending frozen values.
7. While the sensor is offline, `imuService()` does no I2C and `imuRead()` returns `IMU_MISSING`.
   The once-per-second check detects when it is available again.

<details>
<summary>Current sensor-specific register settings</summary>

| MPU6050 register purpose | Value | Result |
|---|---:|---|
| Power/clock | `0x01` | Awake, X-gyro clock source |
| Sample-rate divider | `1` | 500 Hz internal rate with DLPF enabled |
| Configuration | `0x03` | 42 Hz DLPF |
| Gyroscope configuration | `0x08` | ±500 °/s |
| Accelerometer configuration | `0x08` | ±4 g |

These are implementation details in `imu.cpp`. They may be reorganised or replaced if the
public ranges, sample meaning, status behaviour, and timing remain equivalent.

</details>

## Message sent to the rest of Comms

`firebeetle.ino` places one Hardware sample into the existing 26-byte SENSOR packet:

| Bytes | Field | Meaning |
|---|---|---|
| `0` | `type` | `0x01` for `SENSOR` |
| `1` | `seq` | 8-bit sensor sequence, wrapping `255 -> 0` |
| `2–13` | Six `int16` fields | `ax, ay, az, gx, gy, gz`, little-endian |
| `14–15` | `reserved[0]` | `ImuStatus`, little-endian signed `int16` |
| `16–17` | `reserved[1]` | Gesture flag: `1` while Hardware's detector says the hand may be making a gesture, else `0` |
| `18–23` | `reserved[2..4]` | Zero; reserved for later use |
| `24–25` | CRC | CRC-16/CCITT-FALSE over bytes `0–23`, little-endian |

Player identity is not stored in the packet. It comes from the player-specific BLE connection.
The laptop validates the packet before forwarding this decoded message:

```json
{
  "type": "SENSOR",
  "node": 1,
  "seq": 42,
  "timestamp": 12345.678,
  "fields": [120, -4, 8190, 7, -2, 1, 1, 0, 0, 0, 0],
  "imu": {"ax": 120, "ay": -4, "az": 8190, "gx": 7, "gy": -2, "gz": 1},
  "reserved": [1, 0, 0, 0, 0],
  "crc": 12345
}
```

`timestamp` is the laptop receive time, not the sensor measurement time. The shown CRC is only
an example field; the laptop checks the actual CRC before forwarding the message. IMU status is
currently carried as `reserved[0]` rather than as a separate JSON field.

## Change guide

Hardware and Comms may change their internal implementations independently while `imu.h`, the
raw-count meanings, timing expectations, and BLE packet remain stable. These changes require
coordination:

| Change | Required coordination |
|---|---|
| Sensor model, I2C address, pins, or power | Hardware updates the physical design/adapter; Comms updates build assumptions and physical tests |
| Axis order, sign, or mounting orientation | Hardware updates mounting and dataset definitions; Comms updates field semantics and tests |
| Range, units, or calibration | Hardware updates the physical/data contract; Comms updates `imu.h`, register setup, assertions, tests, and documentation |
| Filter or internal sample rate | Hardware confirms the required signal; Comms updates `imu.cpp` and validates live timing |
| FireBeetle read/transmit rate | Comms updates scheduling and BLE tests; Hardware confirms the acquisition behaviour |
| `imuBegin`, `imuRead`, or `ImuStatus` | Update `imu.h`, `imu.cpp`, `firebeetle.ino`, the laptop decoder if needed, and tests together |
| Number of IMUs | New interface version: update sample shape, packet format, bandwidth, dataset contract, and parsers |
| BLE field order, length, or status location | Comms updates firmware, decoder, MTU gating, forwarding, and tests; Hardware confirms raw-value semantics |

Changes confined to I2C helpers, register-read structure, retry implementation, or diagnostic
text need no Comms consumer changes if the public contract stays identical.

## Run the integrated path

Build [`firebeetle.ino`](../../Comms/firebeetle/firebeetle.ino) in the Arduino IDE with the
ESP32 board package 3.x (tested on 3.2.0) and NimBLE-Arduino 2.x. On an older board package the
I2C logging fix may not apply, so check that serial shows no `i2c.master` error lines. Keep
`firebeetle.ino`, `imu.cpp`, and `imu.h` in the same sketch folder. Set `PLAYER_ID` to `1` before
flashing the first FireBeetle and `2` before flashing the second.

Expected serial output with the IMU connected:

```text
[MOTOR] ready on GPIO 2
[IMU] gesture model 342c4355152c26f4
[IMU] ready
[BLE] link encrypted -> streaming enabled
[IMU] status=1, slowest since last report: imuService ... us, imuRead ... us, gestureFlag ... us
[IMU] detector: readings=~2500 decisions=~100 max_score=... gestures started=... ended=... rejected=..., fifo overflows=0 stalls=0, detector gaps=0, clipped=...
```

And with no IMU connected, where BLE still starts and the board sends zeros:

```text
[IMU] not found
[BLE] link encrypted -> streaming enabled
[IMU] status=2, slowest since last report: imuService ... us, imuRead ... us, gestureFlag ... us
```

From `team/Comms`, run both players through the standalone laptop client:

```powershell
.\.venv\Scripts\python.exe laptop\ble_client.py --nodes 1,2 -v
```

For a longer run, omit `-v` so per-packet console output does not become the bottleneck.

## Verification and current evidence

The software-level contract is covered by:

- [`test_ble_packet.py`](../../Comms/laptop/tests/test_ble_packet.py): fixed packet layout, CRC,
  field ordering, decoded message shape, and extraction of `reserved[0]` as IMU status.
- [`test_ble_client.py`](../../Comms/laptop/tests/test_ble_client.py): warning suppression,
  missing/recovering transitions, recovery duration, reconnect behaviour, and ECHO isolation.

**On hardware so far (16 Oct, player 1, no IMU attached):** about 100 Hz with no loss, status
`2` with zeros, one warning on the laptop, and every command echoed back. The slowest `imuRead`
was about 0.35 ms. Before the I2C driver's logging was turned off it was about 13 ms, which cost
packets.

A physical run with a real IMU should still verify:

- both nodes remain near 100 Hz without sustained gaps or CRC failures;
- stationary values are plausible and both units use the expected physical axis signs;
- `reserved[0]` is `1` during healthy operation;
- unplugging a sensor produces one warning without killing BLE;
- reconnecting it produces one recovery transition followed by real readings; and
- the slowest reported `imuRead` remains below the 10 ms packet period.

The software tests do not prove physical wiring, axis orientation, calibration, live sampling
quality, or recovery on the real FireBeetles.

## Undecided for now

- **Hardware:** confirm the actual power wiring and identical mounting orientation.
- **Hardware:** decide whether each player needs stored accelerometer or gyroscope offsets.
- **Hardware:** confirm the I2C address and pins (0x68, SDA 21, SCL 22) and the internal sample
  rate (500 Hz), which `imu.cpp` assumes for now.
- **Hardware + Comms:** confirm that ±4 g, ±500 °/s, 42 Hz filtering, and live 100 Hz reads
  match the intended Hardware dataset contract.
- **Comms:** decide whether IMU status should remain in `reserved[0]` or become an explicit JSON
  field, and ensure consumers reject status `2`/`3` samples.
- **Hardware + Comms:** capture a physical two-player soak log covering normal streaming,
  unplug/recovery, packet loss, CRC failures, and worst-case read time.
