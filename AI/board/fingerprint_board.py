#!/usr/bin/env python3
"""Fingerprint test: which input encoding does the CNN IP actually decode?
Same data path as timing_1sample.py. The first N rows are sent in five encodings
and scored under each, giving one row of accuracies. Every candidate bug gives a
different row (EXPECTED, computed off-board with cnn_small.keras on the same rows),
so the row this board prints names the bug."""
import time
import numpy as np
import pandas as pd
from pynq import Overlay, allocate

BITSTREAM = "cnn_logits.xsa"          # works on the older cnn_small builds too
CSV       = "gestures_augmented.csv"
KEEP      = ['0', '1', '2', '3', '4', '5', '6']
TIMESTEPS, CHANNELS, N_CLASSES = 100, 6, 7
N = 200                               # df.head(N): same rows as the Keras reference

def q(x, frac):
    return np.clip(np.round(x * (1 << frac)), -32768, 32767).astype(np.int16)

ENCODE = {                            # (100, 6) floats -> 600 int16 words in DMA order
    "Q.10 time": lambda x: q(x, 10).reshape(-1),     # what infer() sends today
    "Q.8 time":  lambda x: q(x, 8).reshape(-1),
    "Q.12 time": lambda x: q(x, 12).reshape(-1),
    "Q.0 time":  lambda x: q(x, 0).reshape(-1),      # plain integers
    "Q.10 chan": lambda x: q(x, 10).T.reshape(-1),   # channel-major
}
EXPECTED = {                          # accuracy per encoding if the IP decodes the words this way
    "correct: bits as Q6.10, time-major":   [1.00, 0.30, 0.34, 0.15, 0.16],
    "int16 taken as a value (no .range())": [0.15, 0.15, 0.15, 0.46, 0.15],
    "bits read with 8 frac bits":           [0.29, 1.00, 0.18, 0.15, 0.15],
    "bits read with 12 frac bits":          [0.30, 0.15, 1.00, 0.15, 0.20],
    "reads channel-major (c*100+t)":        [0.30, 0.30, 0.29, 0.15, 1.00],
    "output order reversed":                [0.15, 0.12, 0.20, 0.15, 0.15],
}

def dma_wait(ch, name, timeout=3.0):
    t0 = time.perf_counter()
    while not ch.idle:
        if time.perf_counter() - t0 > timeout:
            raise RuntimeError(f"DMA {name} stalled after {timeout} s")

# ---- overlay ----
ol  = Overlay(BITSTREAM)
dma = ol.axi_dma_0
cnn = ol.cnn_axi_0 if 'cnn_axi_0' in ol.ip_dict else None   # None on the old ap_ctrl_none builds
inb  = allocate(shape=(TIMESTEPS * CHANNELS,), dtype=np.int16)   # 600
outb = allocate(shape=(N_CLASSES,),            dtype=np.int16)   # 7

def run(words):
    """Send 600 int16 words exactly as given; return the 7 raw int16 outputs."""
    inb[:] = words
    inb.flush()
    if cnn is not None:
        cnn.write(0x00, 0x01)        # pulse ap_start
    dma.recvchannel.transfer(outb)
    dma.sendchannel.transfer(inb)
    dma_wait(dma.sendchannel, "SEND")
    dma_wait(dma.recvchannel, "RECV")
    outb.invalidate()
    return np.array(outb)

# ---- data (same filtering/order as the Keras reference => same rows) ----
df  = pd.read_csv(CSV, low_memory=False)
df  = df[df['label'].astype(str).isin(KEEP)].reset_index(drop=True).head(N)
idx = {c: i for i, c in enumerate(KEEP)}
X   = df.drop(columns='label').to_numpy(np.float64).reshape(N, TIMESTEPS, CHANNELS)
y   = df['label'].astype(str).map(idx).to_numpy()

# ---- 1. determinism: the same input twice must give the same words ----
a = run(ENCODE["Q.10 time"](X[0]))
b = run(ENCODE["Q.10 time"](X[0]))
print("row 0 raw out, run 1:", a.tolist())
print("row 0 raw out, run 2:", b.tolist(),
      "(same)" if np.array_equal(a, b) else "(DIFFERENT: state leaks between inferences)")

# ---- 2. one accuracy per encoding ----
board = []
for name, enc in ENCODE.items():
    raw  = np.array([run(enc(x)) for x in X])
    pred = raw.argmax(1)
    board.append((pred == y).mean())
    if name == "Q.10 time":
        raw0, pred0 = raw, pred

print(f"\n{'accuracy on df.head(%d)' % N:38s}" + "".join(f"{e:>11s}" for e in ENCODE))
for h, row in EXPECTED.items():
    print(f"{h:38s}" + "".join(f"{v:11.0%}" for v in row))
print(f"{'>>> THIS BOARD':38s}" + "".join(f"{v:11.0%}" for v in board))
dist = {h: np.abs(np.array(r) - board).sum() for h, r in EXPECTED.items()}
best = min(dist, key=dist.get)
print(f"\nclosest row: {best}  (distance {dist[best]:.2f}; under 0.3 is a clear match. "
      "No clear match points at the weights in the IP or the output packing)")

# ---- 3. detail for the current encoding ----
cm = np.zeros((N_CLASSES, N_CLASSES), int)
np.add.at(cm, (y, pred0), 1)
print("\nconfusion matrix @ Q.10 time (rows = true, cols = predicted):")
print(cm)
print("\nraw int16 outputs @ Q.10 time, first row of each class:")
for c in range(N_CLASSES):
    i = int(np.flatnonzero(y == c)[0])
    print(f"  true {c}  row {i:3d}  pred {pred0[i]}  raw {raw0[i].tolist()}")

