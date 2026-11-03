"""The transport rules for the SENSOR stream on the Ultra96, in front of AI's API.

Repeats, late packets and samples with no IMU reading never reach AI, and AI is told when
a node's stream broke. A gesture the glove rejected is never ended, so AI never classifies it.
Every sample that passes goes to AI's wk10_integration.py, which buffers and classifies, so a
new model changes nothing here.
"""

import collections
import importlib
import logging
import math
import pathlib
import sys
import time

import ai_events

# AI's files sit next to this one on the board, but in the AI folder in the repo
AI_BOARD = pathlib.Path(__file__).resolve().parents[2] / "AI" / "board"
if AI_BOARD.is_dir() and str(AI_BOARD) not in sys.path:
    sys.path.append(str(AI_BOARD))

# the IMU fields of a SENSOR message, in the order AI's IMUSample takes them
CHANNELS = ("ax", "ay", "az", "gx", "gy", "gz")


class ClassifierError(RuntimeError):
    """Raised when AI fails on one gesture, so the session can carry on."""


# same rule as the laptop's ble_client, a jump of 128 or more means the seq went backwards
SEQ_BACKWARD_THRESHOLD = 128

# A gap this long between a node's packets, in the laptop's receive time, means its stream
# stopped: a BLE reconnect or a reboot. seq can't show either, since it carries on after a
# reconnect and restarts at 0 after a reboot, so AI starts the node's gesture again. At 100 Hz
# it is 50 missing samples, so the gesture in progress was no use anyway.
RESET_GAP_S = 0.5

# The IMU status in a SENSOR message's reserved[0], as in firebeetle/imu.h. Only IMU_OK
# carries a sensor reading: 0 is old firmware's dummy ramp, and 2 and 3 are the zeros sent
# while the IMU is missing or recovering
IMU_OK = 1

# reserved[1] carries the glove's gesture flag, as in firebeetle/imu.h. It is 1 for every sample
# of a possible gesture, and 2 on the sample after a gesture the glove's detector threw away
FLAG_FIELD = 1
IN_GESTURE = 1
REJECTED = 2

# AI's model and the bitstream it loads
AI_MODULE = "wk10_integration"
AI_BITSTREAM = "baseline_improved.xsa"

# result of classifying one gesture, missing is the number of packets lost inside it
Result = collections.namedtuple("Result", "label confidence t_infer_ms missing")

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
    """Apply the transport rules per node, then pass its samples on to AI.

    A new one is made every TCP session.
    """

    def __init__(self, ai):
        self.ai = ai
        # AI's buffers live as long as the model, so a reconnect empties them here
        self.ai.reset()
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
            # restart once per outage so AI never joins readings from both sides of it
            # or classifies the old ones again
            if not self._without_imu.get(node):
                self._restart(node)
            self._without_imu[node] = self._without_imu.get(node, 0) + 1
            return None
        self._without_imu[node] = 0
        return node, sample, delta - 1

    def feed(self, msg):
        """Pass one SENSOR message to AI.

        Returns a Result when AI classifies a gesture, otherwise None. Raises ClassifierError
        if AI fails.
        """
        accepted = self._accept(msg)
        if accepted is None:
            return None
        node, sample, gap = accepted
        flag = gesture_flag(msg)
        flagged = self._flagged.pop(node, None)
        if flag == IN_GESTURE:
            # don't count the first flagged sample's gap since it happened before the gesture
            self._flagged[node] = (1, 0) if flagged is None else (flagged[0] + 1, flagged[1] + gap)
        elif flag == REJECTED and flagged is not None:
            # AI isn't reset, so its buffer is still full if the player draws again straight away
            log.info("no AI event: node %d's glove rejected its flagged gesture of %d samples",
                     node, flagged[0])
            flagged = None
        # AI classifies on end_of_gesture, so it goes on the first sample after the flag drops
        ended = flagged is not None and flag != IN_GESTURE
        started = time.perf_counter()
        answer = self.ai.on_sample(node, to_record(sample, msg, ended))
        if answer is None:
            if ended:
                # so a detector that only fires briefly still shows in the log
                log.info("no AI event: node %d's flagged gesture of %d samples gave no result%s",
                         node, flagged[0], self._why())
            return None
        t_infer_ms = (time.perf_counter() - started) * 1000
        label, confidence = answer
        return Result(label, confidence, t_infer_ms, 0 if flagged is None else flagged[1] + gap)

    def _why(self):
        """Return why AI gave no result, e.g. ' (noise p=0.94)', or '' if it didn't say."""
        reason = getattr(self.ai, "reject_reason", lambda: None)()
        return f" ({reason})" if reason else ""

    def _restart(self, node):
        """Tell AI to forget a node's gesture in progress."""
        self._flagged.pop(node, None)
        self.ai.reset(node)

    def _forget(self, node):
        """Drop everything about a node, so its next packet starts again."""
        self._restart(node)
        for state in (self._last_seq, self._without_imu, self._last_time):
            state.pop(node, None)


class AI:
    """AI's model in wk10_integration.py."""

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

    def reject_reason(self):
        """Return why the model's latest inference gave no result, or None."""
        return getattr(self.model, "last_reject", None)

    def reset(self, player=None):
        """Forget a player's samples, or every player's."""
        self.model.reset(player)


class StubAI:
    """Used instead of AI's model where there is no FPGA (the laptop and tests)."""

    def on_sample(self, player, record):
        """Answer at the end of every gesture, with a gesture picked from the last sample's counts."""
        if not record["end_of_gesture"]:
            return None
        total = sum(record[name] for name in CHANNELS)
        return ai_events.GESTURES[total % len(ai_events.GESTURES)], 1.0

    def reset(self, player=None):
        pass


def load_classifier(ai_h5=None, vocab=None):
    """Return AI's model on the board, or a stub anywhere else.

    With `ai_h5`, run that Keras model on the laptop instead (AI/board/keras_stream.py).
    `vocab` selects the FPGA model's label vocabulary, "old" (7-class) or "new" (8-class
    + noise); None uses wk10_integration's default. The loaded bitstream must output that
    many classes in that order. Ignored by the ai_h5 path (keras_stream reads it from the model).
    """
    if ai_h5:
        import keras_stream  # AI/board is on sys.path, added at the top
        return AI(keras_stream, keras_stream.GestureClassifier(ai_h5))
    if not FPGA_MANAGER.exists():
        return StubAI()
    module = importlib.import_module(AI_MODULE)  # needs numpy, so only on the board
    bitstream = pathlib.Path(module.__file__).with_name(AI_BITSTREAM)
    config = module.vocab_config(vocab)
    return AI(module, module.GestureClassifier(bitstream=str(bitstream), config=config))
