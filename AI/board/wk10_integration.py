#!/usr/bin/env python3
"""Gesture inference module for the board.

Two uses, one file, no networking:

  * server mode  -- comms imports `GestureClassifier` and drives it live. Comms
                    owns the socket / framing / heartbeat / reconnect; this module
                    only loads the bitstream, decodes, infers and postprocesses.
  * test  mode   -- `TestHarness` evaluates accuracy from a decoded CSV. Accuracy
                    lives here because only the test set carries labels.

PARITY INVARIANT (must stay byte-identical to training / cosim):
  * window WIN=150 steps x CHANNELS=6 = 900 DMA inputs; gesture length GEST=100.
  * fixed-point I/O: to_fixed(x)=clip(round(x*1024),-32768,32767).int16;
    logits = outb / 1024 (OUT_FRAC=10); softmax in float.
  * preprocessing (the ONLY preprocessing, matches prepare_real_dataset.py):
      channel order [ax, ay, az, gx, gy, gz], step-major (f0..f5 = step 0).
      decode (no normalization):
        accel ch 0-2: counts * 0.001197100830078125  (= 9.80665 / 8192)   -> m/s^2
        gyro  ch 3-5: counts * 0.00026646248122050834 (= (pi/180) / 65.5) -> rad/s
  * label map (class id -> gesture):
        0 arc, 1 caret, 2 circle, 3 infinity, 4 star, 5 triangle, 6 z

Server-mode contract (networking is comms' job, NOT ours):

    from gesture_stream_sim import GestureClassifier, IMUSample, ai_event_from_result
    clf = GestureClassifier(bitstream="xsa/baseline_improved.xsa")   # once at startup
    # per received JSON packet (comms owns the socket):
    res = clf.feed(IMUSample(ax=msg["ax"], ay=msg["ay"], az=msg["az"],
                             gx=msg["gx"], gy=msg["gy"], gz=msg["gz"],
                             end_of_gesture=msg["eog"]), player=player)
    if res is not None:
        await send_message(writer, ai_event_from_result(res, player, seq))

Each player has their own ring buffer, which rolls continuously -- no reset
between gestures.

Importing this module has NO side effects (no training, no socket, no inference).

Run as a script:
    python3 gesture_stream_sim.py --mode test              # accuracy, continuous (default)
    python3 gesture_stream_sim.py --mode test --windowed    # isolated / centered gestures
    python3 gesture_stream_sim.py --mode test --sweep=-40:40:10   # offset robustness sweep
    python3 gesture_stream_sim.py --mode test --zeros        # zero-pad filler instead of noise
    python3 gesture_stream_sim.py --mode server              # build + idle; comms drives it
Off the board (no pynq): add --mock.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import time
from dataclasses import dataclass, field

import numpy as np

# ---- baked constants (keep identical to training / cosim) -----------------
WIN            = 150          # ring-buffer / model window, in steps
GEST           = 100          # gesture length in the CSV, in steps
CHANNELS       = 6
N_CLASSES      = 7
SCALE          = 1 << 10      # to_fixed multiplier (1024)
OUT_FRAC       = 10           # ap_fixed<16,6> -> 10 frac bits (logits = outb / 1024)
CONF_THRESHOLD = 0.7          # reject top class below this as rest/blank; 0.0 = never reject

CROP           = 40           # test only: max |offset| steps the EOG may miss the gesture by
NOISE_FRAC     = 0.05         # test only: filler noise std as a fraction of per-channel gesture std

BITSTREAM = "xsa/baseline_improved.xsa"   # default overlay (constructor arg)
CSV       = "gestures_real_train.csv"      # default decoded dataset for test mode
KEEP      = ['0', '1', '2', '3', '4', '5', '6']

# decode scales: counts -> physical units (the ONLY preprocessing)
ACCEL_TO_M_S2 = 0.001197100830078125       # = 9.80665 / 8192
GYRO_TO_RAD_S = 0.00026646248122050834     # = (pi/180) / 65.5

# class id -> gesture name (all 7 are real gestures; rest is handled by CONF_THRESHOLD)
LABEL_MAP = {0: "arc", 1: "caret", 2: "circle", 3: "infinity",
             4: "star", 5: "triangle", 6: "z"}


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------
@dataclass
class IMUSample:
    """One IMU timestep as raw int16 counts, in channel order [ax,ay,az,gx,gy,gz]."""
    ax: int
    ay: int
    az: int
    gx: int
    gy: int
    gz: int
    end_of_gesture: bool = False
    timestamp: float | None = None


@dataclass
class GestureResult:
    """One classified gesture."""
    class_id: int
    gesture: str
    confidence: float
    logits: list
    timestamp: float | None = None


@dataclass
class ModelConfig:
    """Baked defaults, all overridable. Decode scales + label map match the parity
    invariant in the module docstring and (on the board) label_map.json."""
    win: int = WIN
    gest: int = GEST
    channels: int = CHANNELS
    n_classes: int = N_CLASSES
    scale: int = SCALE
    out_frac: int = OUT_FRAC
    conf_threshold: float = CONF_THRESHOLD
    label_map: dict = field(default_factory=lambda: dict(LABEL_MAP))
    accel_to_m_s2: float = ACCEL_TO_M_S2
    gyro_to_rad_s: float = GYRO_TO_RAD_S


# ---------------------------------------------------------------------------
# Backends (internal; selected by `mock`)
# ---------------------------------------------------------------------------
class _BoardBackend:
    """The real board inference path, lifted unchanged from test_accuracy.py /
    BoardAccel.infer: pynq Overlay + axi_dma_0 + cnn_axi_0, ap_start pulse, cache
    flush/invalidate. Owns the DMA-contiguous inb (win*channels) / outb (n_classes)."""

    def __init__(self, bitstream: str, config: ModelConfig):
        from pynq import Overlay, allocate
        self.ol = Overlay(bitstream)
        self.dma = self.ol.axi_dma_0
        if 'cnn_axi_0' not in self.ol.ip_dict:
            raise SystemExit("cnn_axi_0 has no AXI-Lite control -- old ap_ctrl_none "
                             "bitstream? Rebuild with s_axilite (3.0) first.")
        self.cnn = self.ol.cnn_axi_0
        self.inb = allocate(shape=(config.win * config.channels,), dtype=np.int16)   # 900
        self.outb = allocate(shape=(config.n_classes,), dtype=np.int16)              # 7

    def _wait(self, ch, name, timeout=3.0):
        t0 = time.time(); time.sleep(0.01)
        while not ch.idle:
            if time.time() - t0 > timeout:
                raise RuntimeError(f"DMA {name} stalled after {timeout}s")

    def run(self):
        """Transfer the current inb through the accelerator; return raw outb int16."""
        self.inb.flush()
        self.cnn.write(0x00, 0x01)                # pulse ap_start -> one clean frame
        self.dma.recvchannel.transfer(self.outb)  # arm receiver
        self.dma.sendchannel.transfer(self.inb)   # send input
        self._wait(self.dma.sendchannel, "SEND")
        self._wait(self.dma.recvchannel, "RECV")
        self.outb.invalidate()
        return np.asarray(self.outb)


class _MockBackend:
    """Off-board stand-in so the decode/buffer/EOG path runs without pynq.
    Returns all-zero logits -> uniform softmax (pred 0, conf 1/n_classes)."""

    def __init__(self, bitstream: str, config: ModelConfig):
        self.inb = np.zeros((config.win * config.channels,), dtype=np.int16)
        self.outb = np.zeros((config.n_classes,), dtype=np.int16)

    def run(self):
        self.outb[:] = 0
        return np.asarray(self.outb)


# ---------------------------------------------------------------------------
# GestureClassifier -- the exposed API (one instance, one ring buffer per player)
# ---------------------------------------------------------------------------
class GestureClassifier:
    """Load bitstream, decode, infer, postprocess. No networking, no accuracy.

    Comms calls `feed(sample, player)` once per received packet; everything else
    is internal or for the test harness. The accelerator is shared, but each
    player has their own ring buffer."""

    def __init__(self, bitstream: str = BITSTREAM, config: ModelConfig = ModelConfig(),
                 mock: bool = False):
        self.config = config
        self._backend = (_MockBackend if mock else _BoardBackend)(bitstream, config)
        self.inb = self._backend.inb     # DMA input buffer (win*channels int16)
        self.outb = self._backend.outb   # DMA output buffer (n_classes int16)
        self.bufs = {}                   # player -> rolling ring buffer
        self.last_result: GestureResult | None = None
        self._assert_parity(bitstream)   # best-effort label_map.json guard (no-op off-board)

    # ---- public API -------------------------------------------------------
    def feed(self, sample: IMUSample, player=None) -> GestureResult | None:
        """The single method comms calls per packet. Decode + append to the
        player's ring buffer; if sample.end_of_gesture, run infer() and return its
        result (with the packet timestamp attached), else return None. Never
        resets the buffer."""
        self._buf(player).append(self._decode([sample.ax, sample.ay, sample.az,
                                               sample.gx, sample.gy, sample.gz]))
        if not sample.end_of_gesture:
            return None
        res = self.infer(player)
        if res is not None:
            res.timestamp = sample.timestamp   # res is self.last_result (same object)
        return res

    def infer(self, player=None) -> GestureResult | None:
        """Infer on the last `win` samples of the player's ring buffer. None if the
        buffer is not full or the top class is below conf_threshold."""
        buf = self._buf(player)
        if len(buf) < self.config.win:
            return None
        return self._run_window(np.stack(buf, axis=0))

    def infer_window(self, samples) -> GestureResult | None:
        """Comms-owns-the-buffer variant: infer on a caller-supplied (win, channels)
        block of already-decoded physical units. One-line future switch for feed()."""
        window = np.asarray(samples, dtype=np.float32).reshape(self.config.win,
                                                               self.config.channels)
        return self._run_window(window)

    def push_decoded(self, vec6, player=None) -> None:
        """Inject one already-decoded step (6 physical-unit channels), bypassing
        decode. Used by the test harness to feed decoded CSV values."""
        self._buf(player).append(np.asarray(vec6, dtype=np.float32).reshape(self.config.channels))

    def reset(self, player=None) -> None:
        """Clear one player's ring buffer, or every player's when player is None."""
        if player is None:
            self.bufs.clear()
        else:
            self.bufs.pop(player, None)

    # ---- internal stages --------------------------------------------------
    def _buf(self, player):
        """The player's ring buffer, made empty on their first sample."""
        if player not in self.bufs:
            self.bufs[player] = collections.deque(maxlen=self.config.win)
        return self.bufs[player]

    def _decode(self, counts):
        """Raw int16 counts -> physical units. The ONLY preprocessing (no norm).
        accel ch 0-2 -> m/s^2, gyro ch 3-5 -> rad/s. Returns float32."""
        c = np.asarray(counts, dtype=np.float64)
        out = np.empty(c.shape, dtype=np.float32)
        out[..., 0:3] = c[..., 0:3] * self.config.accel_to_m_s2
        out[..., 3:6] = c[..., 3:6] * self.config.gyro_to_rad_s
        return out

    def _to_fixed(self, x):
        q = np.round(np.asarray(x, dtype=np.float64) * self.config.scale)
        return np.clip(q, -32768, 32767).astype(np.int16)

    def _run_window(self, window):
        self.inb[:] = self._to_fixed(window).reshape(-1)
        return self._postprocess(self._backend.run())

    def _postprocess(self, raw) -> GestureResult | None:
        z = np.asarray(raw).astype(np.float64) / (1 << self.config.out_frac)   # fixed -> float logits
        p = np.exp(z - z.max()); p /= p.sum()                                   # softmax (float)
        pred = int(p.argmax()); conf = float(p[pred])
        if conf < self.config.conf_threshold:
            self.last_result = None
            return None
        res = GestureResult(class_id=pred,
                            gesture=self.config.label_map.get(pred, str(pred)),
                            confidence=conf, logits=z.tolist(), timestamp=None)
        self.last_result = res
        return res

    def _assert_parity(self, bitstream: str) -> None:
        """Guard: assert decode constants + label map equal label_map.json when it
        is found next to the bitstream / CSV / this file. Silently skipped off-board
        (file absent) so --mock and plain imports work. Expected schema is either
        {"arc": 0, ...} or {"0": "arc", ...}, optionally nested under "label_map",
        with optional decode scales under accel_to_m_s2 / gyro_to_rad_s (or
        accel_scale / gyro_scale)."""
        path = None
        seen = set()
        for d in (os.path.dirname(os.path.abspath(bitstream)),
                  os.path.dirname(os.path.abspath(CSV)),
                  os.path.dirname(os.path.abspath(__file__)),
                  os.getcwd()):
            if d in seen:
                continue
            seen.add(d)
            cand = os.path.join(d, "label_map.json")
            if os.path.exists(cand):
                path = cand
                break
        if path is None:
            return   # off-board / file not shipped here -> no-op

        with open(path) as f:
            meta = json.load(f)
        lm = meta.get("label_map", meta) if isinstance(meta, dict) else meta

        # normalize label_map.json to {id: name}, whichever orientation it uses
        id_to_name = None
        if isinstance(lm, dict):
            items = list(lm.items())
            if items and all(str(k).lstrip("-").isdigit() for k, _ in items):
                id_to_name = {int(k): str(v) for k, v in items}          # {"0": "arc"}
            elif items and all(str(v).lstrip("-").isdigit() for _, v in items):
                id_to_name = {int(v): str(k) for k, v in items}          # {"arc": 0}
        if id_to_name is not None:
            assert id_to_name == self.config.label_map, (
                f"label_map mismatch vs {path}:\n  json  = {id_to_name}\n"
                f"  baked = {self.config.label_map}")
        else:
            print(f"[parity] WARNING: unrecognized label_map.json schema in {path}; "
                  f"skipped label-map check.")

        # optional decode-scale check (only asserts keys that are present)
        if isinstance(meta, dict):
            for keys, baked in ((("accel_to_m_s2", "accel_scale"), self.config.accel_to_m_s2),
                                (("gyro_to_rad_s", "gyro_scale"), self.config.gyro_to_rad_s)):
                for k in keys:
                    if k in meta:
                        assert math.isclose(float(meta[k]), baked, rel_tol=0, abs_tol=1e-18), (
                            f"decode scale {k} mismatch vs {path}: "
                            f"json={meta[k]} baked={baked}")
                        break


# ---------------------------------------------------------------------------
# Comms helper: shape a result like ai_events.random_event for EventSource.emit
# ---------------------------------------------------------------------------
def ai_event_from_result(result: GestureResult, player, seq):
    """Shape a GestureResult into the AI_EVENT dict comms sends over TCP."""
    return {"type": "AI_EVENT", "player": player, "seq": seq,
            "gesture": result.gesture, "mana_cost": 0, "timestamp": time.time()}


# ---------------------------------------------------------------------------
# TestHarness (test mode ONLY): CSV load + 150-window synthesis + scoring.
# Feeds the classifier via push_decoded then infer(). Accuracy lives here
# because it needs labels; server mode has no accuracy.
# ---------------------------------------------------------------------------
class TestHarness:
    def __init__(self, classifier: GestureClassifier, config: ModelConfig = ModelConfig(),
                 seed: int = 0):
        self.clf = classifier
        self.config = config
        self.rng = np.random.default_rng(seed)
        self.seed = seed

    # ---- data -------------------------------------------------------------
    def load_windows(self, csv: str, keep=KEEP):
        """Each CSV row is one GEST-step gesture already in physical units
        (f0..f5 = step 0's 6 channels). Returns (X[n,GEST,CHANNELS], y[n])."""
        import pandas as pd
        df = pd.read_csv(csv, low_memory=False)
        df = df[df['label'].astype(str).isin(keep)].reset_index(drop=True)
        idx = {c: i for i, c in enumerate(keep)}
        feat_cols = [c for c in df.columns if c != 'label']
        X = df[feat_cols].to_numpy(dtype=np.float32).reshape(-1, self.config.gest,
                                                             self.config.channels)
        # int(float(...)) because a pandas row upcasts int labels to float ("0.0").
        y = np.array([idx[str(int(float(v)))] for v in df['label'].to_numpy()])
        return X, y

    @staticmethod
    def parse_offsets(spec):
        """'LO:HI:STEP' -> inclusive int list ('-40:40:10' -> [-40,-30,...,40]);
        a bare 'd' -> [d]."""
        parts = [int(p) for p in str(spec).split(":")]
        if len(parts) == 1:
            return parts
        lo, hi = parts[0], parts[1]
        step = parts[2] if len(parts) > 2 else 1
        return list(range(lo, hi + 1, step))

    # ---- 150-window synthesis --------------------------------------------
    def build_filler(self, n, rng=None, ref=None, zeros=False):
        """`n` filler steps (n, channels). --zeros / no ref -> zeros; else gaussian
        rest-noise with per-channel std = NOISE_FRAC * the gesture's per-channel std."""
        ch = self.config.channels
        if zeros or ref is None or rng is None:
            return np.zeros((n, ch), dtype=np.float32)
        std = np.std(np.asarray(ref, dtype=np.float32), axis=0)
        return rng.normal(0.0, NOISE_FRAC * std, size=(n, ch)).astype(np.float32)

    def make_window(self, gesture, offset=0, rng=None, zeros=False):
        """Embed a GEST-step gesture in a WIN-step window of filler. offset shifts
        the gesture from centered (offset 0 = centered = training-like); a large
        |offset| (up to CROP) trails the gesture partly out of the window to model a
        mistimed end-of-gesture. Returns (WIN, channels) physical units."""
        win, gest, ch = self.config.win, self.config.gest, self.config.channels
        gesture = np.asarray(gesture, dtype=np.float32).reshape(gest, ch)
        out = self.build_filler(win, rng, ref=gesture, zeros=zeros)
        start = (win - gest) // 2 + int(offset)     # centered at offset 0
        w0, w1 = max(0, start), min(win, start + gest)
        if w1 > w0:
            g0 = w0 - start
            out[w0:w1] = gesture[g0:g0 + (w1 - w0)]
        return out

    def _run_one(self, gesture, offset, rng, zeros):
        """Synthesize a window, stream it through the classifier via push_decoded,
        and infer. Resets first (isolated window)."""
        self.clf.reset()
        for step in self.make_window(gesture, offset, rng, zeros):
            self.clf.push_decoded(step)
        return self.clf.infer()

    # ---- scoring modes ----------------------------------------------------
    def simulate_windowed(self, X, y, n, zeros=False):
        """Each gesture centered in its window (offset 0). Reproduces the training
        ceiling. Returns per-gesture GestureResult list (None where rejected)."""
        results, correct, emitted = [], 0, 0
        for i in range(n):
            res = self._run_one(X[i], 0, self.rng, zeros)
            results.append(res)
            if res is None:
                print(f"{i:>3}  true={y[i]}  pred=--  (rejected as rest/blank)")
                continue
            emitted += 1
            correct += res.class_id == y[i]
            print(f"{i:>3}  true={y[i]}  pred={res.class_id} {res.gesture:<9} "
                  f"conf={res.confidence:.2f}")
        self._report(emitted, correct, n)
        return results

    def simulate_continuous(self, X, y, n, zeros=False):
        """Each gesture at a random offset in [-CROP, CROP] -- a mistimed-EOG stream.
        Returns per-gesture GestureResult list."""
        results, correct, emitted = [], 0, 0
        for i in range(n):
            offset = int(self.rng.integers(-CROP, CROP + 1))
            res = self._run_one(X[i], offset, self.rng, zeros)
            results.append(res)
            if res is None:
                print(f"{i:>3}  true={y[i]}  pred=--  (no event; offset={offset:+d})")
                continue
            emitted += 1
            correct += res.class_id == y[i]
            print(f"{i:>3}  true={y[i]}  pred={res.class_id} {res.gesture:<9} "
                  f"conf={res.confidence:.2f}  offset={offset:+d}")
        self._report(emitted, correct, n)
        return results

    def simulate_sweep(self, X, y, n, offsets, zeros=False):
        """Fixed-offset robustness sweep: fire the EOG exactly `offset` steps off every
        gesture and report accuracy, mean confidence and per-class accuracy. Each
        offset replays identical seeded filler, so the only variable is the offset.
        Returns per-offset summary dicts."""
        classes = sorted({int(v) for v in y[:n]})
        grid, summaries = {}, []
        print(f"{'offset':>6}  {'emitted':>9}  {'acc':>6}  {'mean_conf':>9}")
        for d in offsets:
            rng = np.random.default_rng(self.seed)     # identical filler per offset
            correct = emitted = 0
            conf_sum = 0.0
            cls_correct, cls_total = collections.Counter(), collections.Counter()
            for i in range(n):
                res = self._run_one(X[i], d, rng, zeros)
                cls_total[int(y[i])] += 1
                if res is not None:
                    emitted += 1
                    conf_sum += res.confidence
                    if res.class_id == y[i]:
                        correct += 1
                        cls_correct[int(y[i])] += 1
            acc = correct / n                            # a rejected window counts as wrong
            mean_conf = conf_sum / (emitted or 1)
            print(f"{d:>+6d}  {f'{emitted}/{n}':>9}  {acc:6.1%}  {mean_conf:9.2f}")
            grid[d] = (cls_correct, cls_total)
            summaries.append({"offset": d, "emitted": emitted, "acc": acc,
                              "mean_conf": mean_conf})

        print("\nper-class accuracy by offset (%):")
        print(f"{'  class':<14}" + "".join(f"{d:>+5d}" for d in offsets))
        for c in classes:
            cells = ""
            for d in offsets:
                cc, ct = grid[d]
                tot = ct.get(c, 0)
                cells += f"{100 * cc.get(c, 0) / tot:>5.0f}" if tot else f"{'--':>5}"
            print(f"  {c} {self.config.label_map.get(c, str(c)):<10}" + cells)
        return summaries

    @staticmethod
    def _report(emitted, correct, n):
        denom = emitted or 1
        print(f"\nemitted {emitted}/{n},  correct {correct}/{emitted} = {correct / denom:.2%}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Gesture inference: test accuracy or "
                                             "build a server-mode classifier (no socket).")
    ap.add_argument("--mode", choices=["test", "server"], default="test",
                    help="test: score a CSV; server: build a classifier and idle (comms drives it)")
    ap.add_argument("--windowed", action="store_true",
                    help="test: each gesture centered in its window; default is a continuous stream")
    ap.add_argument("--sweep", nargs="?", const="-40:40:10", default=None, metavar="LO:HI:STEP",
                    help="test: fixed-offset robustness sweep, e.g. --sweep=-40:40:10 (overrides mode)")
    ap.add_argument("--zeros", action="store_true",
                    help="test: zero-pad the window instead of rest-noise filler")
    ap.add_argument("--n", type=int, default=50, help="test: number of gestures to simulate")
    ap.add_argument("--seed", type=int, default=0, help="test: RNG seed for filler/offsets")
    ap.add_argument("--csv", default=CSV, help="test: decoded gestures CSV")
    ap.add_argument("--mock", action="store_true", help="off-board: skip pynq, stub the accelerator")
    ap.add_argument("--bitstream", default=BITSTREAM)
    args = ap.parse_args()

    if args.mode == "server":
        GestureClassifier(bitstream=args.bitstream, mock=args.mock)
        print(f"GestureClassifier ready (bitstream={args.bitstream}, mock={args.mock}). "
              f"Comms owns the socket; import this module and call feed(). Idle.")
        return

    clf = GestureClassifier(bitstream=args.bitstream, mock=args.mock)
    harness = TestHarness(clf, config=clf.config, seed=args.seed)
    X, y = harness.load_windows(args.csv)
    n = min(args.n, len(X))

    if args.sweep is not None:
        offsets = harness.parse_offsets(args.sweep)
        print(f"{len(X)} windows in {args.csv}; sweep over offsets {offsets} "
              f"(n={n}, seed={args.seed}, zeros={args.zeros})\n")
        harness.simulate_sweep(X, y, n, offsets, zeros=args.zeros)
    elif args.windowed:
        print(f"{len(X)} windows in {args.csv}; simulating {n} (windowed/centered, "
              f"zeros={args.zeros})\n")
        harness.simulate_windowed(X, y, n, zeros=args.zeros)
    else:
        print(f"{len(X)} windows in {args.csv}; simulating {n} (continuous, "
              f"offset +/-{CROP}, seed={args.seed}, zeros={args.zeros})\n")
        harness.simulate_continuous(X, y, n, zeros=args.zeros)


if __name__ == "__main__":
    main()
