#!/usr/bin/env python3
"""Echo/passthrough control test: identical data pipeline to test_bitstream3.py,
only the bitstream differs (echo.xsa instead of the CNN). Any difference in
results is therefore attributable to the block, not to how data is handled."""
import time
import numpy as np
import pandas as pd
from pynq import Overlay, allocate

BITSTREAM = "echo.xsa"                # <-- ONLY intended difference vs test_bitstream3.py
CSV       = "gestures_augmented.csv"
KEEP      = ['0', '1', '2', '3', '4', '5', '6']
TIMESTEPS, CHANNELS, N_CLASSES = 100, 6, 7
TAPS = [0, 100, 199, 300, 400, 500, 599]   # input positions the echo IP returns (from the ramp test)
SCALE = 1 << 10
I16MAX, I16MIN = 32767, -32768

def to_fixed(x):
    q = np.round(np.asarray(x, dtype=np.float64) * SCALE)
    return np.clip(q, I16MIN, I16MAX).astype(np.int16)

def from_fixed(q):
    return np.asarray(q).astype(np.float32) / SCALE

def dma_wait(ch, name, timeout=3.0):
    t0 = time.time(); time.sleep(0.01)
    while not ch.idle:
        if time.time() - t0 > timeout:
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

# ---- data (same filtering/order as test_bitstream3.py) ----
df  = pd.read_csv(CSV, low_memory=False)
df  = df[df['label'].astype(str).isin(KEEP)].reset_index(drop=True)
idx = {c: i for i, c in enumerate(KEEP)}
feat_cols = [c for c in df.columns if c != 'label']
print("max |feature| =", df[feat_cols].abs().to_numpy().max())

# ---- echo passthrough check: board output should equal the input at the 7 tap positions ----
print("\n--- echo passthrough check (board output vs input @ taps) ---")
N = min(10, len(df))
n_ok = 0
for i in range(N):
    feat = df.iloc[i][feat_cols].to_numpy()
    z, _ = infer(feat)                        # z = board output, de-scaled by 2^OUT_FRAC
    exp  = from_fixed(to_fixed(feat))[TAPS]   # the input at the taps, after the same fixed-point round
    match = np.allclose(z, exp, atol=1e-6)
    n_ok += match
    print(f"sample {i:2d}:")
    print(f"  input @taps: {np.round(exp, 3)}")
    print(f"  board out:   {np.round(z,   3)}   {'MATCH' if match else 'MISMATCH'}")
print(f"\n{n_ok}/{N} samples echoed exactly")

