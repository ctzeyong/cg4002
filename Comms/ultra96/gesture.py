"""Gesture windows on the Ultra96, and the classifier that reads them.

Keeps each node's last 100 IMU samples and turns them into the model's input. The model
itself (input shape, fixed point, the FPGA calls) is AI's accelerator.py.
Board check: `python3 gesture.py CSV [BITSTREAM]`.
"""

import collections
import math
import pathlib
import sys
import time

try:
    import accelerator  # on the board, accelerator.py sits next to this file
except ImportError:  # in the repo, it is in the AI folder instead
    sys.path.append(str(pathlib.Path(__file__).resolve().parents[2] / "AI" / "board"))
    import accelerator

WINDOW = accelerator.TIMESTEPS  # samples per inference, one second at 100 Hz
CHANNELS = ("ax", "ay", "az", "gx", "gy", "gz")  # the model's channel order
assert len(CHANNELS) == accelerator.CHANNELS

# sensor ranges from the hardware sketch (±2 g, ±500 °/s), may change in the sensors integration
ACCEL_LSB_PER_G = 16384
GYRO_LSB_PER_DPS = 65.5
STANDARD_GRAVITY = 9.80665
ACCEL_SCALE = STANDARD_GRAVITY / ACCEL_LSB_PER_G  # counts -> m/s²
GYRO_SCALE = math.radians(1) / GYRO_LSB_PER_DPS  # counts -> rad/s
SCALES = (ACCEL_SCALE,) * 3 + (GYRO_SCALE,) * 3

# the gesture field in the AI event is just the class number until the classes are named
LABELS = tuple(str(i) for i in range(accelerator.N_CLASSES))

# same rule as the laptop's ble_client, a jump of 128 or more means the seq went backwards
SEQ_BACKWARD_THRESHOLD = 128

# result of classifying one window, missing is the number of packets lost inside it
Result = collections.namedtuple("Result", "label confidence t_infer_ms missing")

# still need to confirm with the AI team which bitstream is the latest
BITSTREAM = pathlib.Path(__file__).with_name("gesture_min_q.xsa")
DMA_TIMEOUT_S = 0.05  # an inference normally takes about 1 ms
FPGA_MANAGER = pathlib.Path("/sys/class/fpga_manager/fpga0")  # only exists on the board


class ClassifierError(Exception):
    """Raised when an inference fails, e.g. the DMA stalls. That window gets no AI event."""


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


quantise = accelerator.to_fixed  # converts a float to the model's Q6.10 int16


def to_features(samples):
    """Convert raw counts (one 6-value sample per time step) into the 600 model inputs.

    The output is in time order, the same as the AI team's CSV rows.
    """
    return [
        quantise(count * scale)
        for sample in samples
        for count, scale in zip(sample, SCALES)
    ]


def log_fields(result):
    """Return the extra AI event log fields for a classified event, or nothing for a dummy one."""
    if result is None:
        return ""
    return (
        f" conf={result.confidence:.3f} t_infer_ms={result.t_infer_ms:.3f}"
        f" missing={result.missing}"
    )


class Windows:
    """Keep the last WINDOW samples from each node. A new one is made every TCP session."""

    def __init__(self, classifier):
        self.classifier = classifier
        self._samples = {}  # node -> deque of (sample, packets missing just before it)
        self._last_seq = {}

    def add(self, msg):
        """Add one SENSOR message to its node's window.

        Returns False if it was malformed, a repeat or late.
        """
        node, seq, imu = msg.get("node"), msg.get("seq"), msg.get("imu")
        if not (_is_int(node) and _is_int(seq) and isinstance(imu, dict)):
            return False
        sample = tuple(imu.get(name) for name in CHANNELS)
        if not all(_is_int(value) for value in sample):
            return False

        last = self._last_seq.get(node)
        delta = 1 if last is None else (seq - last) & 0xFF
        if delta == 0 or delta >= SEQ_BACKWARD_THRESHOLD:
            return False  # repeat or late packet
        self._last_seq[node] = seq
        window = self._samples.setdefault(node, collections.deque(maxlen=WINDOW))
        window.append((sample, delta - 1))
        return True

    def size(self, node):
        return len(self._samples.get(node, ()))

    def classify(self, node):
        """Classify a node's window. Returns None until the window is full."""
        window = self._samples.get(node)
        if window is None or len(window) < WINDOW:
            return None
        features = to_features(sample for sample, _ in window)
        label, confidence, t_infer_ms = self.classifier.classify(features)
        # don't count the first sample's gap since it happened before the window started
        missing = sum(gap for _, gap in window) - window[0][1]
        return Result(label, confidence, t_infer_ms, missing)


class StubClassifier:
    """Used in place of the FPGA classifier when there is no FPGA (the laptop and tests)."""

    name = "stub"

    def classify(self, features):
        """Pick a class from the inputs, so the same window always gives the same class."""
        started = time.perf_counter()
        label = LABELS[sum(features) % len(LABELS)]
        return label, 1.0, (time.perf_counter() - started) * 1000


class Classifier:
    name = "fpga"

    def __init__(self, bitstream=BITSTREAM):
        self.accelerator = accelerator.Accelerator(bitstream)
        # first inference is slow so run warmup now
        started = time.perf_counter()
        self.infer([0] * (WINDOW * len(CHANNELS)))
        self.cold_ms = (time.perf_counter() - started) * 1000

    def infer(self, features):
        """Run one inference with the 600 Q6.10 inputs and return the 7 logits as floats."""
        return self.accelerator.infer(features, timeout=DMA_TIMEOUT_S)

    def classify(self, features):
        """Return the most likely class, its softmax probability and the inference time in ms."""
        started = time.perf_counter()
        try:
            logits = self.infer(features)
        except RuntimeError as exc:  # DMA timeout, or pynq refusing to use a busy channel
            raise ClassifierError(str(exc)) from exc
        t_infer_ms = (time.perf_counter() - started) * 1000
        best = max(range(len(logits)), key=logits.__getitem__)
        confidence = 1 / sum(math.exp(z - logits[best]) for z in logits)
        return LABELS[best], confidence, t_infer_ms


def load_classifier():
    return Classifier() if FPGA_MANAGER.exists() else StubClassifier()


def check(csv_path, bitstream=BITSTREAM):
    import csv

    clf = Classifier(bitstream)
    with open(csv_path, newline="") as f:
        reader = csv.reader(f)
        next(reader)
        rows = [next(reader) for _ in range(10)]

    print("--- the first 10 rows; compare with their test_accuracy.py ---")
    for row in rows:
        logits = clf.infer([quantise(float(v)) for v in row[1:]])
        best = max(range(len(logits)), key=logits.__getitem__)
        print(f"  logits: {[round(z, 3) for z in logits]}  pred: {best}  label: {row[0]}")

    times = [clf.classify([quantise(float(v)) for v in rows[i % 10][1:]])[2] for i in range(100)]
    print(f"cold (first after loading): {clf.cold_ms:.3f} ms")
    print(f"warm x100: mean {sum(times) / len(times):.3f} ms, max {max(times):.3f} ms")

    windows = Windows(clf)
    for seq in range(WINDOW):
        windows.add({"node": 1, "seq": seq, "imu": dict(zip(CHANNELS, (seq * 100,) * 6))})
    started = time.perf_counter()
    for _ in range(100):
        windows.classify(1)
    per_window = (time.perf_counter() - started) * 10  # average ms per window over 100 runs
    print(f"window -> inputs -> class, x100: mean {per_window:.3f} ms")


if __name__ == "__main__":
    check(*sys.argv[1:3])
