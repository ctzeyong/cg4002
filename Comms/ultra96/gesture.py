"""The transport rules for the SENSOR stream on the Ultra96, in front of AI's API.

Repeats, late packets and samples with no IMU reading never reach AI, and AI is told when
a node's stream broke. With --gesture-flag every other sample goes to AI's ai_api.py, which
decides where a gesture is, so a new model changes nothing here. --sensor-trigger keeps
each node's last samples in a window instead, until the flag path replaces it.
"""

import collections
import logging
import math
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

# A gap this long between a node's packets, in the laptop's receive time, means its stream
# stopped: a BLE reconnect or a reboot. seq can't show either, since it carries on after a
# reconnect and restarts at 0 after a reboot, so the node's window starts again. At 100 Hz it
# is 50 missing samples, so the old window was no use anyway.
RESET_GAP_S = 0.5

# The IMU status in a SENSOR message's reserved[0], as in firebeetle/imu.h. Only IMU_OK
# carries a sensor reading: 0 is old firmware's dummy ramp, and 2 and 3 are the zeros sent
# while the IMU is missing or recovering
IMU_OK = 1

# reserved[1] carries the glove's gesture flag, 1 for every sample of a possible gesture.
FLAG_FIELD = 1

# result of classifying one window or gesture, missing is the number of packets lost inside it
Result = collections.namedtuple("Result", "label confidence t_infer_ms missing")

TIMEOUT_S = 0.05  # most a classification may block the event loop; it normally takes ~1 ms
FPGA_MANAGER = pathlib.Path("/sys/class/fpga_manager/fpga0")  # only exists on the board

log = logging.getLogger("gesture")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def imu_status(msg):
    """Return the IMU status in a SENSOR message's reserved[0], or None if it has none."""
    reserved = msg.get("reserved")
    if isinstance(reserved, list) and reserved and _is_int(reserved[0]):
        return reserved[0]
    return None


def receive_time(msg):
    """Return the laptop's receive time of a message, or None if it has no usable one."""
    t = msg.get("timestamp")
    if isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t):
        return float(t)
    return None


def gesture_flag(msg):
    """Return the gesture flag in a SENSOR message's reserved fields, or 0 if it has none."""
    reserved = msg.get("reserved")
    if isinstance(reserved, list) and len(reserved) > FLAG_FIELD and _is_int(reserved[FLAG_FIELD]):
        return reserved[FLAG_FIELD]
    return 0


def to_record(sample, msg):
    """Return AI's record for one sample: its six counts, the gesture flag and receive time."""
    return {**sample, "in_gesture": gesture_flag(msg), "timestamp": receive_time(msg)}


def log_fields(result):
    """Return the extra AI event log fields for a classified event, or nothing for a dummy one."""
    if result is None:
        return ""
    return (
        f" conf={result.confidence:.3f} t_infer_ms={result.t_infer_ms:.3f}"
        f" missing={result.missing}"
    )


class Windows:
    """Apply the transport rules per node, then window or pass on its samples.

    A new one is made every TCP session.
    """

    def __init__(self, classifier):
        self.classifier = classifier
        # new per session like everything here, so a reconnect also starts AI's gestures again
        self.ai = ai_api.Gestures(classifier)
        self._samples = {}  # node -> deque of (sample, packets missing just before it)
        self._last_seq = {}
        self._without_imu = {}  # node -> packets in a row that carried no IMU reading
        self._last_time = {}  # node -> laptop receive time of its latest packet
        self._flagged = {}  # node -> (samples, packets lost) of its flagged gesture so far

    def _accept(self, msg):
        """Apply the transport rules to one SENSOR message.

        Returns (node, sample, packets lost just before it), or None if it was malformed,
        a repeat or late, or carried no IMU reading.
        """
        node, seq, imu = msg.get("node"), msg.get("seq"), msg.get("imu")
        if not (_is_int(node) and _is_int(seq) and isinstance(imu, dict)):
            return None
        sample = {name: imu.get(name) for name in CHANNELS}
        if not all(_is_int(value) for value in sample.values()):
            return None

        # before the seq rule, because after a reboot the new seq can look like a late packet
        t = receive_time(msg)
        last_t = self._last_time.get(node)
        if t is not None and last_t is not None and t - last_t > RESET_GAP_S:
            self._forget(node)
            last_t = None

        last = self._last_seq.get(node)
        delta = 1 if last is None else (seq - last) & 0xFF
        if delta == 0 or delta >= SEQ_BACKWARD_THRESHOLD:
            return None  # repeat or late packet
        self._last_seq[node] = seq
        if t is not None:
            self._last_time[node] = t if last_t is None else max(last_t, t)
        if imu_status(msg) != IMU_OK:
            # restart once per outage so neither the window nor AI joins readings from
            # both sides of it or classifies the old ones again
            if not self._without_imu.get(node):
                self._restart(node)
            self._without_imu[node] = self._without_imu.get(node, 0) + 1
            return None
        self._without_imu[node] = 0
        return node, sample, delta - 1

    def add(self, msg):
        """Add one SENSOR message to its node's window, for --sensor-trigger.

        Returns False if it was malformed, a repeat or late, or carried no IMU reading.
        """
        accepted = self._accept(msg)
        if accepted is None:
            return False
        node, sample, gap = accepted
        self._samples.setdefault(node, collections.deque(maxlen=WINDOW)).append((sample, gap))
        return True

    def feed(self, msg):
        """Pass one SENSOR message to AI, for --gesture-flag.

        Returns a Result when AI classifies a gesture, otherwise None. Raises ClassifierError
        if AI fails.
        """
        accepted = self._accept(msg)
        if accepted is None:
            return None
        node, sample, gap = accepted
        record = to_record(sample, msg)
        flagged = self._flagged.pop(node, None)
        if record["in_gesture"]:
            # don't count the first flagged sample's gap since it happened before the gesture
            self._flagged[node] = (1, 0) if flagged is None else (flagged[0] + 1, flagged[1] + gap)
        started = time.perf_counter()
        answer = self.ai.on_sample(node, record, timeout=TIMEOUT_S)
        if answer is None:
            if flagged is not None and not record["in_gesture"]:
                # so a detector that only fires briefly still shows in the log
                log.info("no AI event: node %d's flagged gesture of %d samples gave no result",
                         node, flagged[0])
            return None
        t_infer_ms = (time.perf_counter() - started) * 1000
        label, confidence = answer
        return Result(label, confidence, t_infer_ms, 0 if flagged is None else flagged[1] + gap)

    def _restart(self, node):
        """Empty a node's window and tell AI to forget its gesture in progress."""
        self._samples.pop(node, None)
        self._flagged.pop(node, None)
        self.ai.reset(node)

    def _forget(self, node):
        """Drop everything about a node, so its next packet starts again."""
        self._restart(node)
        for state in (self._last_seq, self._without_imu, self._last_time):
            state.pop(node, None)

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
