"""The transport rules for the SENSOR stream on the Ultra96, in front of AI's API.

Repeats, late packets and samples with no IMU reading never reach AI, and AI is told when
a node's stream broke. With --gesture-flag every other sample goes to AI's
wk10_integration.py, which buffers and classifies, so a new model changes nothing here.
--sensor-trigger keeps each node's last samples in a window for ai_api.py instead, until
the flag path replaces it.
"""

import collections
import importlib
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

# AI's per-sample model for --gesture-flag and the bitstream it loads
AI_MODULE = "wk10_integration"
AI_BITSTREAM = "baseline_improved.xsa"

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


def to_record(sample, msg, end_of_gesture):
    """Return AI's IMUSample fields for one sample: its six counts, end_of_gesture, receive time."""
    return {**sample, "end_of_gesture": end_of_gesture, "timestamp": receive_time(msg)}


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

    A new one is made every TCP session. `classifier` classifies a window for --sensor-trigger,
    or with per_sample=True it is AI's model that takes every sample, for --gesture-flag.
    """

    def __init__(self, classifier, per_sample=False, resample=False):
        self.classifier = classifier
        self.ai = classifier if per_sample else None
        if self.ai is not None:
            # AI's buffers live as long as the model, so a reconnect empties them here
            self.ai.reset()
        # --resample: comms owns the buffer, resamples each gesture to the training
        # window and classifies it whole, instead of feeding raw samples one at a
        # time. Only meaningful with the per-sample AI, off by default so the
        # per-sample path below is untouched. preprocess (numpy) is imported only
        # when it is on, so the default pipeline never loads it through here.
        self.resample = bool(resample) and self.ai is not None
        self._pp = None
        if self.resample:
            import preprocess  # lazy: ultra96/ is on sys.path (flat imports)
            self._pp = preprocess
        self._samples = {}  # node -> deque of (sample, packets missing just before it)
        self._last_seq = {}
        self._without_imu = {}  # node -> packets in a row that carried no IMU reading
        self._last_time = {}  # node -> laptop receive time of its latest packet
        self._flagged = {}  # node -> (samples, packets lost) of its flagged gesture so far
        self._spans = {}  # node -> {"samples": [...counts...], "missing": int} for --resample

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
        if self.resample:
            return self._feed_resampled(node, sample, msg, gap)
        in_gesture = gesture_flag(msg)
        flagged = self._flagged.pop(node, None)
        if in_gesture:
            # don't count the first flagged sample's gap since it happened before the gesture
            self._flagged[node] = (1, 0) if flagged is None else (flagged[0] + 1, flagged[1] + gap)
        # AI classifies on end_of_gesture, so it goes on the first sample after the flag drops
        ended = flagged is not None and not in_gesture
        started = time.perf_counter()
        answer = self.ai.on_sample(node, to_record(sample, msg, ended))
        if answer is None:
            if ended:
                # so a detector that only fires briefly still shows in the log
                log.info("no AI event: node %d's flagged gesture of %d samples gave no result",
                         node, flagged[0])
            return None
        t_infer_ms = (time.perf_counter() - started) * 1000
        label, confidence = answer
        return Result(label, confidence, t_infer_ms, 0 if flagged is None else flagged[1] + gap)

    def _feed_resampled(self, node, sample, msg, gap):
        """--resample variant of feed(): buffer the flagged span, then at the end of
        the gesture resample it to the training window and classify that whole window
        in one shot. The per-sample path in feed() is left untouched; this runs only
        when the Windows was built with resample=True.
        """
        in_gesture = gesture_flag(msg)
        span = self._spans.get(node)
        if in_gesture:
            if span is None:
                # first flagged sample: its preceding gap belongs to before the gesture
                span = self._spans[node] = {"samples": [], "missing": 0}
            else:
                span["missing"] += gap
            span["samples"].append([sample[name] for name in CHANNELS])
            return None
        if span is None:
            return None  # not in a gesture, and not the first sample after one ended
        del self._spans[node]
        n = len(span["samples"])
        if n < 2:  # resample_steps needs at least two samples
            log.info("no AI event: node %d's flagged gesture of %d samples too short to resample",
                     node, n)
            return None
        window, clipped = self._pp.gesture_window(span["samples"])
        if clipped:
            log.debug("node %d: %d resampled counts saturated int16", node, clipped)
        started = time.perf_counter()
        answer = self.ai.infer_window(node, window)
        t_infer_ms = (time.perf_counter() - started) * 1000
        if answer is None:
            log.info("no AI event: node %d's resampled gesture of %d samples gave no result",
                     node, n)
            return None
        label, confidence = answer
        return Result(label, confidence, t_infer_ms, span["missing"])

    def _restart(self, node):
        """Empty a node's window and tell AI to forget its gesture in progress."""
        self._samples.pop(node, None)
        self._flagged.pop(node, None)
        self._spans.pop(node, None)  # drop any --resample span in progress
        if self.ai is not None:
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


class AI:
    """AI's per-sample model in wk10_integration.py."""

    def __init__(self, module, model):
        self.module = module  # for its IMUSample
        self.model = model

    def on_sample(self, player, record):
        """Feed one sample. Returns (label, confidence) when it ends a gesture, otherwise None."""
        try:
            result = self.model.feed(self.module.IMUSample(**record), player=player)
        except RuntimeError as exc:  # its DMA stall, so the session survives it
            raise ClassifierError(str(exc)) from exc
        return None if result is None else (result.gesture, result.confidence)

    def infer_window(self, player, window):
        """Classify one prepared (WIN, CHANNELS) window of decoded physical units,
        for --resample. player is unused: the window is already a complete gesture."""
        try:
            result = self.model.infer_window(window)
        except RuntimeError as exc:  # DMA stall, handled the same as on_sample
            raise ClassifierError(str(exc)) from exc
        return None if result is None else (result.gesture, result.confidence)

    def reset(self, player=None):
        """Forget a player's samples, or every player's."""
        self.model.reset(player)


class StubAI:
    """Used instead of AI's per-sample model where there is no FPGA (the laptop and tests)."""

    def on_sample(self, player, record):
        """Answer at the end of every gesture, with a class picked from the last sample's counts."""
        if not record["end_of_gesture"]:
            return None
        return LABELS[sum(record[name] for name in CHANNELS) % len(LABELS)], 1.0

    def infer_window(self, player, window):
        """Answer from the window's sum, for --resample without an FPGA."""
        total = sum(float(sum(row)) for row in window)
        return LABELS[int(abs(total)) % len(LABELS)], 1.0

    def reset(self, player=None):
        pass


def load_classifier(per_sample=False, ai_h5=None):
    """AI's model on the board, a stub anywhere else. per_sample picks the --gesture-flag one.

    ai_h5: path to a local Keras .h5 model to run instead of the FPGA bitstream (per-sample
    path), for end-to-end testing off the board. See AI/board/keras_stream.py.
    """
    if ai_h5:
        import keras_stream  # AI/board is on sys.path via the ai_api import at the top
        return AI(keras_stream, keras_stream.GestureClassifier(ai_h5))
    if not FPGA_MANAGER.exists():
        return StubAI() if per_sample else StubClassifier()
    if not per_sample:
        return ai_api.GestureClassifier()
    module = importlib.import_module(AI_MODULE)  # needs numpy, so only on the board
    bitstream = pathlib.Path(module.__file__).with_name(AI_BITSTREAM)
    return AI(module, module.GestureClassifier(bitstream=str(bitstream)))
