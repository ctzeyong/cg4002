#!/usr/bin/env python3
"""Board test with ap_ctrl_hs: pulse ap_start per inference, then DMA."""
import time
import numpy as np
import pandas as pd
from pynq import Overlay, allocate

BITSTREAM = "gesture_min_q.xsa"          # <-- your ap_ctrl_hs (3.0) build + matching .hwh
CSV       = "gestures_augmented.csv"
KEEP      = ['0', '1', '2', '3', '4', '5', '6']
TIMESTEPS, CHANNELS, N_CLASSES = 100, 6, 7
SCALE = 1 << 10
I16MAX, I16MIN = 32767, -32768

def to_fixed(x):
    q = np.round(np.asarray(x, dtype=np.float64) * SCALE)
    return np.clip(q, I16MIN, I16MAX).astype(np.int16)

def from_fixed(q):
    return np.asarray(q).astype(np.float32) / SCALE

def dma_wait(ch, name, timeout=3.0):
    t0 = time.perf_counter()
    while not ch.idle:
        if time.perf_counter() - t0 > timeout:
            print(f"  [TIMEOUT] {name} after {timeout}s", flush=True); return False
    return True

# ---- overlay ----
ol  = Overlay(BITSTREAM)
dma = ol.axi_dma_0
if 'cnn_axi_0' not in ol.ip_dict:
    raise SystemExit("cnn_axi_0 has no AXI-Lite control -- are you on the OLD "
                     "ap_ctrl_none bitstream? Rebuild with s_axilite (3.0) first.")
cnn = ol.cnn_axi_0                   # the IP's control port (new in the ap_ctrl_hs build)
# print(cnn.register_map)
inb  = allocate(shape=(TIMESTEPS * CHANNELS,), dtype=np.int16)   # 600
outb = allocate(shape=(N_CLASSES,),            dtype=np.int16)   # 7

OUT_FRAC = 10   # ap_fixed<16,6> -> 10 frac bits; use 8 if you widened to <16,8>

def infer(feat600):
    x = np.asarray(feat600, dtype=np.float32).reshape(TIMESTEPS, CHANNELS)
    inb[:] = to_fixed(x).reshape(-1)
    inb.flush()
    cnn.write(0x00, 0x01)            # pulse ap_start -> one clean frame
    dma.recvchannel.transfer(outb)  # arm receiver
    dma.sendchannel.transfer(inb)   # send input
    ok_s = dma_wait(dma.sendchannel, "SEND")
    ok_r = dma_wait(dma.recvchannel, "RECV")
    if not (ok_s and ok_r):
        raise RuntimeError(f"DMA stalled (send={ok_s}, recv={ok_r})")
    outb.invalidate()
    z = np.asarray(outb).astype(np.float64) / (1 << OUT_FRAC)   # fixed-point logits -> float
    p = np.exp(z - z.max()); p /= p.sum()                       # softmax (exact, in float)
    return z, p

# ---- data (same filtering/order as before) ----
df  = pd.read_csv(CSV, low_memory=False)
df  = df[df['label'].astype(str).isin(KEEP)].reset_index(drop=True)
idx = {c: i for i, c in enumerate(KEEP)}
feat_cols = [c for c in df.columns if c != 'label']
print("max |feature| =", df[feat_cols].abs().to_numpy().max())

# ---- single inferences ----
print("\n--- 10 single inferences ---")
for i in range(10):
    row    = df.iloc[i]
    z, p = infer(row[feat_cols].to_numpy())
    print(f"  logits: {np.round(z,3)}")
    print(f"  softmax: {np.round(p,3)}  pred: {int(p.argmax())}  label: {row['label']}")

# ---- batch accuracy ----
N = min(10, len(df))
correct = 0
for _, row in df.head(N).iterrows():
    pred = int(infer(row[feat_cols].to_numpy())[1].argmax())
    correct += (pred == idx[str(row['label'])])
print(f"\nboard accuracy on {N} samples: {correct}/{N} = {correct / N:.2%}")
