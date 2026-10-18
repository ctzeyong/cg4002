"""The gesture classifier, as an API for the Comms board client.

    model = GestureClassifier()     # loads the bitstream, once per run
    gestures = Gestures(model)      # new every TCP session
    result = gestures.on_sample(player, record, timeout=0.05)  # every sample
    gestures.reset(player)          # the player's stream broke

on_sample() gets each player's samples in order, e.g.
{"ax": 120, ..., "gz": 7, "in_gesture": 1, "timestamp": 812.25}, and returns
(label, confidence) once a gesture is classified, otherwise None. Comms passes only real IMU
readings, and calls reset() after an IMU outage or a reconnect.
--sensor-trigger calls model.classify() directly, with the last WINDOW samples.

The model can change here without Comms changing. Comms runs this in its event loop, so:
- on_sample() and classify() return or raise within `timeout`. No sleeps.
- Failures raise ClassifyError. Never exit or print.
- Importing does nothing. Import pynq inside functions, and don't use numpy, since the
  laptop has neither.

Gestures and resample() are a placeholder by Comms, so the flag path can be tested now.
Replace their insides as the model needs, but keep on_sample() and reset().

Board check against test_accuracy.py: python3 ai_api.py CSV [BITSTREAM]
"""
import collections
import math
import pathlib
import sys
import time

WINDOW = 100                                      # steps the model takes per gesture
CHANNELS = ("ax", "ay", "az", "gx", "gy", "gz")  # in the order the model expects
LABELS = tuple(str(i) for i in range(7))         # "0".."6" until the gestures are named
BITSTREAM = pathlib.Path(__file__).with_name("gesture_min_q.xsa")

# counts to the model's units (m/s², rad/s), at the firmware's ±4 g and ±500 °/s (imu.h)
ACCEL_LSB_PER_G = 8192
GYRO_LSB_PER_DPS = 65.5
STANDARD_GRAVITY = 9.80665
SCALES = (STANDARD_GRAVITY / ACCEL_LSB_PER_G,) * 3 + (math.radians(1) / GYRO_LSB_PER_DPS,) * 3

# placeholder gesture lengths at 100 Hz using the 15 Oct dataset
MIN_SAMPLES = 20    # shorter than 0.2 s gives no result
MAX_SAMPLES = 400   # a stuck flag keeps only the last 4 s

FRAC_BITS = 10      # inputs are ap_fixed<16,6>
OUT_FRAC = 10       # so are the outputs; use 8 if they are widened to <16,8>
I16MIN, I16MAX = -32768, 32767


class ClassifyError(RuntimeError):
    """classify() failed, e.g. the DMA did not finish in time."""


def to_fixed(x):
    """Float -> int16 with FRAC_BITS fraction bits, clipped to the int16 range."""
    return min(max(round(x * (1 << FRAC_BITS)), I16MIN), I16MAX)


def to_inputs(samples):
    """Turn raw-count samples into the model's 600 inputs, all 6 channels of t0, then t1..."""
    return [to_fixed(sample[name] * scale)
            for sample in samples
            for name, scale in zip(CHANNELS, SCALES)]


# placeholder by Comms, down to the end of Gestures


def round_counts(x):
    """Round to whole counts, ties away from zero, like the training set."""
    return int(math.floor(abs(x) + 0.5)) * (1 if x >= 0 else -1)


def resample(samples, steps=WINDOW):
    """Stretch or squeeze samples to `steps` with straight lines (training used hann-sinc4-v1)."""
    last = len(samples) - 1
    out = []
    for step in range(steps):
        x = step * last / (steps - 1)
        i = min(int(x), last - 1) if last else 0
        frac = x - i
        a, b = samples[i], samples[min(i + 1, last)]
        out.append({name: round_counts(a[name] + (b[name] - a[name]) * frac) for name in CHANNELS})
    return out


class Gestures:
    """Collect each player's flagged samples and classify a gesture when its flag drops."""

    def __init__(self, model):
        self.model = model
        self._gestures = {}  # player -> flagged samples so far

    def on_sample(self, player, record, timeout=3.0):
        """Take one sample. Return (label, confidence) when it ends a gesture, otherwise None."""
        if record["in_gesture"]:
            gesture = self._gestures.setdefault(player, collections.deque(maxlen=MAX_SAMPLES))
            gesture.append(record)
            return None
        # popped first so a failed gesture isn't retried
        gesture = self._gestures.pop(player, None)
        if gesture is None or len(gesture) < MIN_SAMPLES:
            return None
        return self.model.classify(resample(list(gesture)), timeout=timeout)

    def reset(self, player):
        """Forget the player's gesture in progress."""
        self._gestures.pop(player, None)


class GestureClassifier:
    def __init__(self, bitstream=BITSTREAM):
        from pynq import Overlay, allocate

        ol = Overlay(str(bitstream))
        if 'cnn_axi_0' not in ol.ip_dict:
            raise RuntimeError(f"{bitstream}: cnn_axi_0 has no AXI-Lite control -- are you "
                               "on the OLD ap_ctrl_none bitstream? Rebuild with s_axilite (3.0).")
        self.dma = ol.axi_dma_0
        self.cnn = ol.cnn_axi_0
        self.inb = allocate(shape=(WINDOW * len(CHANNELS),), dtype="int16", cacheable=False)
        self.outb = allocate(shape=(len(LABELS),), dtype="int16", cacheable=False)
        # first inference is slow so run it now, not on a real gesture
        started = time.perf_counter()
        self.logits([0] * len(self.inb))
        self.cold_ms = (time.perf_counter() - started) * 1000

    def classify(self, samples, timeout=3.0):
        """Return the most likely gesture and its softmax probability."""
        if len(samples) != WINDOW:
            raise ClassifyError(f"need {WINDOW} samples, got {len(samples)}")
        z = self.logits(to_inputs(samples), timeout)
        best = max(range(len(z)), key=z.__getitem__)
        confidence = 1 / sum(math.exp(v - z[best]) for v in z)
        return LABELS[best], confidence

    def logits(self, words, timeout=3.0):
        """Run the 600 inputs through the FPGA and return the 7 logits, as test_timing.py does."""
        try:
            self.inb[:] = words
            self.cnn.write(0x00, 0x01)                  # ap_start
            self.dma.recvchannel.transfer(self.outb)    # arm the receive before sending
            self.dma.sendchannel.transfer(self.inb)
        except RuntimeError as exc:                     # pynq raises this if a channel is busy
            raise ClassifyError(str(exc)) from exc
        # poll without sleeping, one deadline for both channels
        deadline = time.perf_counter() + timeout
        for name, ch in (("SEND", self.dma.sendchannel), ("RECV", self.dma.recvchannel)):
            while not ch.idle:
                if time.perf_counter() > deadline:
                    raise ClassifyError(f"DMA {name} still busy after {timeout * 1000:.0f} ms")
        return [int(v) / (1 << OUT_FRAC) for v in self.outb]


def check(csv_path, bitstream=BITSTREAM):
    """Print the same 10 rows as test_accuracy.py, then time classify()."""
    import csv

    model = GestureClassifier(bitstream)
    with open(csv_path, newline="") as f:
        reader = csv.reader(f)
        next(reader)
        rows = [next(reader) for _ in range(10)]

    print("--- the first 10 rows; compare with test_accuracy.py ---")
    for row in rows:                        # label, then 600 values already in m/s² and rad/s
        z = model.logits([to_fixed(float(v)) for v in row[1:]])
        best = max(range(len(z)), key=z.__getitem__)
        print(f"  logits: {[round(v, 3) for v in z]}  pred: {best}  label: {row[0]}")

    # turn the first row back into raw counts, which is what Comms passes in
    values, n = [float(v) for v in rows[0][1:]], len(CHANNELS)
    samples = [dict(zip(CHANNELS, (round(v / s) for v, s in zip(values[t * n:(t + 1) * n], SCALES))))
               for t in range(WINDOW)]
    times = []
    for _ in range(100):
        started = time.perf_counter()
        model.classify(samples, timeout=0.05)
        times.append((time.perf_counter() - started) * 1000)
    print(f"cold (first after loading): {model.cold_ms:.3f} ms")
    print(f"classify() x100: mean {sum(times) / len(times):.3f} ms, max {max(times):.3f} ms")


if __name__ == "__main__":
    check(*sys.argv[1:3])
