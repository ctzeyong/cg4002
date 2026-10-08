#!/usr/bin/env python3
"""Timing test built on test_bitstream3.py: N_SAMPLES inferences, each split into
stages, to show the inference itself against the communication around it.

Stages of one inference, each timed on the PS with time.perf_counter():
  prepare   float -> fixed point, copy into the DMA input buffer       PS processing
  flush     write the CPU cache back to DDR so the DMA reads new data  communication
  start     pulse ap_start, program both DMA channels                  communication
  send      wait until the DMA has streamed all 600 inputs into the IP communication
  result    wait until the IP finishes and its 7 outputs are in DDR    inference
  read      drop stale cache lines, read the 7 logits                  communication
  softmax   softmax on the PS                                          PS processing

The IP starts computing while the input streams in, so part of its work hides
inside "send". Set HLS_LATENCY_CYCLES to also print the IP's own latency.
"""
import time
import numpy as np
import pandas as pd
from pynq import Clocks, Overlay, allocate

N_SAMPLES = 100                # 1 in timing_1sample.py, 10 in timing_10samples.py
BITSTREAM = "gesture_min_q.xsa"          # <-- your ap_ctrl_hs (3.0) build + matching .hwh
CSV       = "gestures_augmented.csv"
KEEP      = ['0', '1', '2', '3', '4', '5', '6']
TIMESTEPS, CHANNELS, N_CLASSES = 100, 6, 7
SCALE = 1 << 10
I16MAX, I16MIN = 32767, -32768
OUT_FRAC = 10   # ap_fixed<16,6> -> 10 frac bits; use 8 if you widened to <16,8>

STAGES = ["prepare", "flush", "start", "send", "result", "read", "softmax"]
GROUPS = [("inference (IP computing, waiting for results)", ["result"]),
          ("communication (flush, start, send, read)", ["flush", "start", "send", "read"]),
          ("PS processing (prepare input, softmax)", ["prepare", "softmax"])]

# ---- overlay ----
ol  = Overlay(BITSTREAM)
dma = ol.axi_dma_0
if 'cnn_axi_0' not in ol.ip_dict:
    raise SystemExit("cnn_axi_0 has no AXI-Lite control -- are you on the OLD "
                     "ap_ctrl_none bitstream? Rebuild with s_axilite (3.0) first.")
cnn = ol.cnn_axi_0                   # the IP's control port (new in the ap_ctrl_hs build)
inb  = allocate(shape=(TIMESTEPS * CHANNELS,), dtype=np.int16, cacheable=False)   # 600
outb = allocate(shape=(N_CLASSES,),            dtype=np.int16, cacheable=False)   # 7
scratch = np.empty(TIMESTEPS * CHANNELS, dtype=np.float32)

def to_fixed(x):
    np.multiply(x, SCALE, out=scratch)        # float32 × 1024, in place
    np.round(scratch, out=scratch)                  # quantize to 10 frac bits
    np.clip(scratch, I16MIN, I16MAX, out=scratch)   # saturate to int16 range
    return scratch

def dma_wait(ch, name, timeout=3.0):
    """Poll until the channel is idle. No sleep: the old 10 ms sleep would swamp these timings."""
    t0 = time.perf_counter()
    while not ch.idle:
        if time.perf_counter() - t0 > timeout:
            raise RuntimeError(f"DMA {name} stalled after {timeout} s")

def infer_timed(feat600):
    """infer() from test_bitstream3.py with a timestamp after each stage.
    Returns the softmax and the time of each stage in microseconds."""
    t = [time.perf_counter()]
    inb[:] = to_fixed(feat600)
    t.append(time.perf_counter())                                  # prepare
    #inb.flush()
    t.append(time.perf_counter())                                  # flush
    cnn.write(0x00, 0x01)            # pulse ap_start -> one clean frame
    dma.recvchannel.transfer(outb)  # arm receiver
    dma.sendchannel.transfer(inb)   # send input
    t.append(time.perf_counter())                                  # start
    dma_wait(dma.sendchannel, "SEND")
    t.append(time.perf_counter())                                  # send
    dma_wait(dma.recvchannel, "RECV")
    t.append(time.perf_counter())                                  # result
    #outb.invalidate()
    z = np.asarray(outb).astype(np.float32) / (1 << OUT_FRAC)     # fixed-point logits -> float
    t.append(time.perf_counter())                                  # read
    p = np.argmax(z)
    # p = np.exp(z - z.max()); p /= p.sum()                          # softmax (exact, in float)
    t.append(time.perf_counter())                                  # softmax
    return p, 1e6 * np.diff(t)

# ---- data (same filtering as test_bitstream3.py) ----
# read the header only to get column names, then read the data as float32 (label excluded)
cols = pd.read_csv(CSV, nrows=0).columns
feat_cols = [c for c in cols if c != 'label']
dtypes = {c: np.float32 for c in feat_cols}
df = pd.read_csv(CSV, low_memory=False, dtype=dtypes)

df  = df[df['label'].astype(str).isin(KEEP)].reset_index(drop=True)
idx = {c: i for i, c in enumerate(KEEP)}
rows = df.sample(n=N_SAMPLES, random_state=0)   # N different samples; sample 1 is the same in both versions

# ---- timed inferences (the pandas row -> numpy step stays outside the timing) ----
times, correct = [], 0
print(f"\nStage times per inference, in microseconds ({N_SAMPLES} sample{'s' * (N_SAMPLES > 1)}):")
print(f"{'#':>3}{'true':>6}{'pred':>6}" + "".join(f"{s:>9}" for s in STAGES) + f"{'total':>9}")
for i, (_, row) in enumerate(rows.iterrows(), start=1):
    feat = row[feat_cols].to_numpy(dtype=np.float32)   
    p, us = infer_timed(feat)                           
    true = idx[str(row['label'])]                       
    pred = p
    correct += pred == true
    times.append(us)
    print(f"{i:>3}{true:>6}{pred:>6}" + "".join(f"{v:9.0f}" for v in us) + f"{us.sum():9.0f}")

times = np.array(times)
if N_SAMPLES > 1:
    use = times[1:]
    print(f"{'mean 2-' + str(N_SAMPLES):>15}" + "".join(f"{v:9.0f}" for v in use.mean(axis=0))
          + f"{use.sum(axis=1).mean():9.0f}")
    what = (f"warm mean of samples 2-{N_SAMPLES}. Sample 1 is slower because the first "
            "inference after loading the overlay pays one-time costs")
    print(f"\n{correct}/{N_SAMPLES} predictions correct")
else:
    use = times
    what = ("one cold inference: the first after loading the overlay, so it includes "
            "one-time costs. timing_10samples.py shows warm inferences")

# ---- inference vs communication ----
mean = use.mean(axis=0)
total = mean.sum()
print(f"\nSplit of one inference ({what}):")
for name, stages in GROUPS:
    v = sum(mean[STAGES.index(s)] for s in stages)
    print(f"  {name:48s}{v:8.0f} us {100 * v / total:5.1f} %")
print(f"  {'total':48s}{total:8.0f} us")

