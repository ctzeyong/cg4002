"""The gesture classifier, as an API for the Comms board client.

    model = GestureClassifier()                   # loads the bitstream
    label, confidence = model.classify(samples, timeout=0.05)

Comms passes the last WINDOW samples from one player, oldest first. Each sample is the raw
IMU counts from a sensor packet, e.g. {"ax": 120, "ay": -4, ..., "gz": 7}.

Everything about the model stays in this file, so you can change the model without Comms
changing anything. Only three things must stay true, because Comms runs classify() inside
an event loop that handles every network packet:
- classify() returns or raises within `timeout`. Don't add sleeps.
- If it fails, it raises ClassifyError. It never exits or prints.
- Importing the file does nothing. Import pynq inside functions, and don't use numpy: Comms
  also imports this file on a laptop that has neither.

Check it on the board against test_accuracy.py: python3 ai_api.py CSV [BITSTREAM]
"""
import math
import pathlib
import sys
import time

WINDOW = 100                                      # samples per gesture (1 s at 100 Hz)
CHANNELS = ("ax", "ay", "az", "gx", "gy", "gz")  # in the order the model expects
LABELS = tuple(str(i) for i in range(7))         # "0".."6" until the gestures are named
BITSTREAM = pathlib.Path(__file__).with_name("gesture_min_q.xsa")

# Counts -> m/s² and rad/s, the units the model was trained on.
# Assumes the ±2 g and ±500 °/s ranges set in the hardware sketch.
ACCEL_LSB_PER_G = 16384
GYRO_LSB_PER_DPS = 65.5
STANDARD_GRAVITY = 9.80665
SCALES = (STANDARD_GRAVITY / ACCEL_LSB_PER_G,) * 3 + (math.radians(1) / GYRO_LSB_PER_DPS,) * 3

FRAC_BITS = 10      # inputs are ap_fixed<16,6>
OUT_FRAC = 10       # so are the outputs; use 8 if they are widened to <16,8>
I16MIN, I16MAX = -32768, 32767


class ClassifyError(RuntimeError):
    """classify() failed, e.g. the DMA did not finish in time."""


def to_fixed(x):
    """Float -> int16 with FRAC_BITS fraction bits, clipped to the int16 range."""
    return min(max(round(x * (1 << FRAC_BITS)), I16MIN), I16MAX)


def to_inputs(samples):
    """Samples of raw counts -> the model's 600 inputs: all 6 channels of t0, then t1, ..."""
    return [to_fixed(sample[name] * scale)
            for sample in samples
            for name, scale in zip(CHANNELS, SCALES)]


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
        # The first inference is slow, so run one now instead of on a real gesture.
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
        """Run the 600 fixed-point inputs through the FPGA and return the 7 logits as floats.

        Same steps as infer_timed() in test_timing.py.
        """
        try:
            self.inb[:] = words
            self.cnn.write(0x00, 0x01)                  # ap_start
            self.dma.recvchannel.transfer(self.outb)    # arm the receive before sending
            self.dma.sendchannel.transfer(self.inb)
        except RuntimeError as exc:                     # pynq raises this if a channel is busy
            raise ClassifyError(str(exc)) from exc
        # Poll without sleeping. One deadline covers both channels.
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
