# Completed-gesture transport — specification only

**Comms teammate owns Bluetooth and packet encoding. No Bluetooth sender is implemented.** The ESP prepares one complete candidate at a time; it no longer streams 100 readings per second.

## Integration

The sketch reads six-axis MPU6050 FIFO samples at nominal **500 Hz**, schema `gesture-packet-v4`, into `gesture::Capture`. `gesture::Segmenter` supplies boundaries, including up to 0.3 s / 150 readings of pretrigger context and excluding the ending confirmation tail. `capture->service()` progressively prepares the output without processing the whole gesture in one call.

When `capture->ready` is true, copy/use `capture->output` **before calling `capture->release()`**. The demonstration sketch currently prints a `GESTURE_READY` diagnostic and releases it; replace that consumer with your transport integration. Do not transmit `Detector`'s internal 100 Hz averaged samples or its legacy gate transitions as symbol packets.

Output contains `frames[steps][6]`, `steps`, `rawCount`, `startUs`, exclusive `endUs`, `triggerUs`, `detectedUs` and end reason (`motion_dip`/`nongesture`). Default steps=100, supported 32–512. Six channels are **ax, ay, az in g, gx, gy, gz in deg/s**, float32, with no amplitude normalization. Preprocessing identifier is `hann-sinc4-v1`. Copy only the first `steps` rows.

The sample grid spans `startUs` through `endUs − 2000`; thus row j represents:

`startUs + j × (rawCount − 1) × 2000 / (steps − 1)`.

This is relative time normalization, **not 100 Hz**. Keep duration=`endUs-startUs`, raw rate/count and original timing metadata. Sensor-clock timestamps are nominal, not Bluetooth arrival times.

## int16 transport encoding

The cloud exporter now uses `imu-counts-int16-v1`. The capture output above remains float32 internally. Before transmission, multiply each resampled acceleration value by 8192 and each gyro value by 65.5 in double precision, round to nearest with ties away from zero (`std::round`), clamp to [-32768, 32767], then cast to int16. Clamp before casting to avoid wraparound; record saturation counts. Apply this after resampling, not to intermediate filter calculations.

Serialize row-major in channel order ax, ay, az, gx, gy, gz, signed two's-complement little-endian. Only the six channels go in the sensor payload; CSV step/time fields are metadata. Cloud can use counts or convert back to g and degrees/s, but must use identical decoding and saved normalization for training and inference. The Python reference is `cloud_export.quantize_counts`. No firmware quantization or Bluetooth sender is implemented by this exporter change.

## Suggested logical packet

| Part | Contents |
|---|---|
| Header | Protocol version, boot/session ID, unique gesture ID, model ID, preprocessing ID, step count, six-channel order, scales and encoding ID `imu-counts-int16-v1` |
| Timing | Start/end/trigger/detection timestamps, raw count, nominal raw rate=500 Hz, schema=gesture-packet-v4, actual pretrigger duration, end reason |
| Payload | `steps × 6` interleaved signed int16 counts, little-endian |
| Integrity | Declared byte length and checksum covering metadata and payload |

A 100-step payload is **1,200 bytes per completed candidate**; 512 steps is 6,144 bytes. Transmission is event-driven after completion and resampling. There is no fixed packet-per-second or sample-per-second output rate: two accepted candidates per second would mean 2,400 payload bytes/s at 100 steps, plus overhead. Bluetooth delivery can be bursty.

This is one **logical** gesture packet, not necessarily one Bluetooth write. For BLE, fragment to the negotiated payload size; for a byte-stream transport, explicitly frame messages. Each fragment needs gesture ID, offset/index and expected total length/count. The laptop checks integrity and reassembles the entire gesture before forwarding it to the cloud; missing fragments must not become a shorter or zero-padded gesture. Transport choice, acknowledgements, bounded queues and reconnect policy belong to Comms.

## Loss and ownership

Do not block FIFO servicing while transmitting. The current capture engine has one output slot, not a transport queue; copy completed data into a bounded queue and release promptly. Count/report queue overflow rather than silently overwriting an unsent gesture. Acquisition gaps invalidate unfinished capture, while busy/overwritten resampling jobs are rejected and counted. Already copied complete packets can retain their original IDs across transport retries; the receiver should deduplicate them.

An interval without a confirmed endpoint within six seconds is rejected, not cut into artificial symbols. A completed candidate still may be background or an incorrectly split/merged symbol; the cloud needs a reject outcome. Cloud training must use the same resampling algorithm and context policy as gameplay. Full-rate training CSV files stay on the laptop for regeneration at other step counts; the 100-frame payload cannot reconstruct them.

