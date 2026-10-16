"""Gesture windows on the Ultra96, handed to AI's API to classify.

Keeps each node's last samples and asks AI's ai_api.py what gesture they are,
so a new model changes nothing here. Only real IMU readings go into a window.
"""

import collections
import pathlib
import sys
import time

try:
    import ai_api  # on the board, ai_api.py sits next to this file
except ImportError:  # in the repo, it is in the AI folder instead
    sys.path.append(str(pathlib.Path(__file__).resolve().parents[2] / "AI" / "board"))
    import ai_api

WINDOW = ai_api.WINDOW
CHANNELS = ai_api.CHANNELS
LABELS = ai_api.LABELS
ClassifierError = ai_api.ClassifyError

# same rule as the laptop's ble_client, a jump of 128 or more means the seq went backwards
SEQ_BACKWARD_THRESHOLD = 128

# The IMU status in a SENSOR message's reserved[0], as in firebeetle/imu.h. Only IMU_OK
# carries a sensor reading: 0 is old firmware's dummy ramp, and 2 and 3 are the zeros sent
# while the IMU is missing or recovering
IMU_OK = 1

# result of classifying one window, missing is the number of packets lost inside it
Result = collections.namedtuple("Result", "label confidence t_infer_ms missing")

TIMEOUT_S = 0.05  # most a classification may block the event loop; it normally takes ~1 ms
FPGA_MANAGER = pathlib.Path("/sys/class/fpga_manager/fpga0")  # only exists on the board


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def imu_status(msg):
    """Return the IMU status in a SENSOR message's reserved[0], or None if it has none."""
    reserved = msg.get("reserved")
    if isinstance(reserved, list) and reserved and _is_int(reserved[0]):
        return reserved[0]
    return None


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
        self._without_imu = {}  # node -> packets in a row that carried no IMU reading

    def add(self, msg):
        """Add one SENSOR message to its node's window.

        Returns False if it was malformed, a repeat or late, or carried no IMU reading.
        """
        node, seq, imu = msg.get("node"), msg.get("seq"), msg.get("imu")
        if not (_is_int(node) and _is_int(seq) and isinstance(imu, dict)):
            return False
        sample = {name: imu.get(name) for name in CHANNELS}
        if not all(_is_int(value) for value in sample.values()):
            return False

        last = self._last_seq.get(node)
        delta = 1 if last is None else (seq - last) & 0xFF
        if delta == 0 or delta >= SEQ_BACKWARD_THRESHOLD:
            return False  # repeat or late packet
        self._last_seq[node] = seq
        window = self._samples.setdefault(node, collections.deque(maxlen=WINDOW))
        if imu_status(msg) != IMU_OK:
            # restart the window so the window doesnt join readings from both sides 
            # of an outage or classifies the old ones again
            window.clear()
            self._without_imu[node] = self._without_imu.get(node, 0) + 1
            return False
        self._without_imu[node] = 0
        window.append((sample, delta - 1))
        return True

    def size(self, node):
        return len(self._samples.get(node, ()))

    def without_imu(self, node):
        """How many packets in a row from `node` have carried no IMU reading."""
        return self._without_imu.get(node, 0)

    def classify(self, node):
        """Classify a node's window. Returns None until the window is full."""
        window = self._samples.get(node)
        if window is None or len(window) < WINDOW:
            return None
        started = time.perf_counter()
        label, confidence = self.classifier.classify([s for s, _ in window], timeout=TIMEOUT_S)
        t_infer_ms = (time.perf_counter() - started) * 1000
        # don't count the first sample's gap since it happened before the window started
        missing = sum(gap for _, gap in window) - window[0][1]
        return Result(label, confidence, t_infer_ms, missing)


class StubClassifier:
    """Used instead of AI's classifier where there is no FPGA (the laptop and tests)."""

    def classify(self, samples, timeout=None):
        """Pick a class from the counts, so the same window always gives the same class."""
        total = sum(sum(sample.values()) for sample in samples)
        return LABELS[total % len(LABELS)], 1.0


def load_classifier():
    return ai_api.GestureClassifier() if FPGA_MANAGER.exists() else StubClassifier()
