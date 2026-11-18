# gesture_stream_sim — AI inference for comms

One self-contained file. Comms **imports** it and drives it from inside the existing
asyncio receive loop. It does **no networking** — comms owns the socket, framing,
heartbeat and reconnect. This module only: load the bitstream, decode, infer,
postprocess.

> `--mode server` on the CLI is just a smoke test (loads the overlay and idles).
> Comms never runs this script; it imports the class.

## Import

Drop `gesture_stream_sim.py` next to your comms files, then:

```python
from gesture_stream_sim import GestureClassifier, IMUSample, ai_event_from_result
```

## Quick start (the whole integration)

```python
# ONCE at process startup (loading the overlay is expensive — do NOT do this per packet):
clf = GestureClassifier(bitstream="xsa/baseline_improved.xsa")

# per received IMU packet, inside your receiver loop:
res = clf.feed(IMUSample(ax=msg["ax"], ay=msg["ay"], az=msg["az"],
                         gx=msg["gx"], gy=msg["gy"], gz=msg["gz"],
                         end_of_gesture=msg["eog"],
                         timestamp=msg.get("timestamp")),
               player=player)
if res is not None:                       # None = mid-gesture, nothing to send
    event = ai_event_from_result(res, player, seq)
    await send_message(writer, event)
    seq += 1
```

Each player has their own ring buffer, which rolls continuously — **no reset
between gestures**. Feed every sample with its player; a result comes back only on
the packet whose `end_of_gesture` is True.

## Input contract — `IMUSample` (comms fills this)

One IMU timestep, **raw int16 counts** (not decoded — the module decodes). Channel
order is fixed: `[ax, ay, az, gx, gy, gz]`.

| field            | type          | required | meaning                                             |
|------------------|---------------|----------|-----------------------------------------------------|
| `ax, ay, az`     | `int`         | yes      | accelerometer counts (raw int16, ±32768)            |
| `gx, gy, gz`     | `int`         | yes      | gyroscope counts (raw int16, ±32768)                |
| `end_of_gesture` | `bool`        | no (False) | set True on the last packet of a gesture → triggers inference |
| `timestamp`      | `float\|None` | no (None) | packet time; copied straight onto the result        |

Notes:
- Pass **raw counts**, not physical units. The module applies the only
  preprocessing (accel → m/s², gyro → rad/s) itself, identical to training.
- `end_of_gesture` is the signal to classify the last 150 steps. Every other
  packet just fills the buffer.

## Output contract — `GestureResult` (comms consumes this)

Returned by `feed()` **only** on an end-of-gesture packet (and only once the buffer
holds a full 150-step window). `None` otherwise.

| field        | type          | meaning                                              |
|--------------|---------------|------------------------------------------------------|
| `class_id`   | `int`         | predicted class, 0–6                                 |
| `gesture`    | `str`         | gesture name for `class_id` (see label map)          |
| `confidence` | `float`       | top softmax probability, 0.0–1.0                     |
| `logits`     | `list[float]` | the 7 raw logits (float), for logging/debug          |
| `timestamp`  | `float\|None` | whatever `timestamp` the triggering `IMUSample` had  |

Label map (`class_id` → `gesture`):

| id | gesture  | id | gesture  |
|----|----------|----|----------|
| 0  | arc      | 4  | star     |
| 1  | caret    | 5  | triangle |
| 2  | circle   | 6  | z        |
| 3  | infinity |    |          |

## Exposed API

### `GestureClassifier(bitstream="xsa/baseline_improved.xsa", config=ModelConfig(), mock=False)`
Build **once** at startup. Loads the overlay + DMA and allocates the DMA buffers.
Each player's 150-step ring buffer is made on their first sample.
- `bitstream` — path to the `.xsa` (relative to the working dir, or absolute).
- `mock=True` — off-board stand-in (no pynq); returns a uniform softmax. For
  testing comms wiring on a laptop.

### `feed(sample: IMUSample, player=None) -> GestureResult | None`
**The one method comms calls per packet.** Decodes the sample, appends it to that
player's ring buffer, and — if `sample.end_of_gesture` — runs inference on that
buffer and returns the
`GestureResult`. Returns `None` on every non-EOG packet, and on an EOG packet if
the buffer isn't full yet or confidence is below threshold.

### `reset(player=None) -> None`
Clears one player's ring buffer, or every player's when `player` is None. The
buffer is meant to roll continuously, so use it only to drop history that would
join two unrelated stretches of samples (e.g. after a long stall or a reconnect).

### `last_result` *(attribute)*
The most recent `GestureResult` (or `None`). For a caller that prefers to poll
instead of using `feed()`'s return value.

### `ai_event_from_result(result: GestureResult, player, seq) -> dict`
Shapes a `GestureResult` into the `AI_EVENT` dict comms sends over TCP:
```python
{"type": "AI_EVENT", "player": player, "seq": seq,
 "gesture": result.gesture, "mana_cost": 0, "timestamp": <now>}
```
`mana_cost` is `0` — set it here if the game needs a real value.

### Advanced (comms usually won't need these)
- `infer(player=None) -> GestureResult | None` — classify a player's current buffer
  on demand (what `feed` calls internally on EOG).
- `infer_window(samples) -> GestureResult | None` — classify a caller-owned
  `(150, 6)` block of already-decoded physical units; bypasses the ring buffer.

## One board, two players

One physical accelerator can't back two `GestureClassifier` instances (each would
re-flash the PL), so one instance serves both players: the backend is shared and
each `player` gets their own ring buffer. Interleaved packets from two players never
mix, as long as each `feed()` passes the right `player`. `player=None` is a buffer
like any other; the test harness uses it.
