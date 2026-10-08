#!/usr/bin/env python3
"""Power of the CNN accelerator on the Ultra96-V2, measured with PMBus.

1. Loads the overlay and checks one prediction.
2. Times one inference on one 600 value sample, then sizes a loop to last DURATION s.
3. Runs that loop while PMBus records every power rail, then reports the mean
   power, inferences per second and energy per inference.

Run it the same way as test_bitstream3.py, in the folder with the .xsa and the CSV.
Writes power_<LABEL>.png (the four key rails and the total over time),
power_<LABEL>_samples.csv (every reading), power_summary.csv (one row per run)
and, once two labels exist, power_compare.png.

Nothing is tuned by default. To compare a setting later: set one entry in KNOBS,
give LABEL a new name, run again. Every knob is undone when the run ends.
"""
import glob
import os
import subprocess
import time

import matplotlib
matplotlib.use("Agg")                          # no display on the board; plots go to PNG files
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pynq
from pynq import Clocks, Overlay, allocate

# ---- run settings ----
BITSTREAM = "gesture_min_q.xsa"
CSV       = "gestures_augmented.csv"
KEEP      = ['0', '1', '2', '3', '4', '5', '6']
TIMESTEPS, CHANNELS, N_CLASSES = 100, 6, 7
SCALE     = 1 << 10
OUT_FRAC  = 10    # ap_fixed<16,6> -> 10 frac bits; use 8 if you widened to <16,8>
I16MAX, I16MIN = 32767, -32768

DURATION = 45     # seconds of inference to average over (30 to 60)
INTERVAL = 0.5    # seconds between PMBus samples
SETTLE   = 5      # seconds to wait after changing a knob

# ---- power knobs: all None = baseline. Set one, rename LABEL, rerun. ----
LABEL = "custom"
KNOBS = dict(
    cpu_khz=299999,          # e.g. 599999; options in /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_frequencies
    cpu_cores=None,        # e.g. 2 keeps cpu0 and cpu1 on, switches cpu2 and cpu3 off
    pl_mhz=10,           # e.g. 50; slows your IP and the DMA together
    bluetooth=None,        # False blocks Bluetooth
    wifi_power_save=None,  # True lets the Wi-Fi radio sleep between packets
)

KEY_RAILS = ["PSINT_FP", "INT", "PSDDR", "3V3"]
WHAT = {"PSINT_FP": "PS: A53 cores, DDR controller", "INT": "PL: your CNN and the DMA",
        "PSDDR": "DDR memory", "3V3": "3.3 V peripherals", "TOTAL": "all 10 rails"}
COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]   # validated categorical slots 1-5
INK, MUTED, GRID, SURFACE = "#0b0b0b", "#898781", "#e1e0d9", "#fcfcfb"
CPUFREQ = "/sys/devices/system/cpu/cpu0/cpufreq/"


# ---- inference (same pipeline as test_bitstream3.py) ----

def to_fixed(x):
    q = np.round(np.asarray(x, dtype=np.float64) * SCALE)
    return np.clip(q, I16MIN, I16MAX).astype(np.int16)


def dma_wait(ch, name, timeout=3.0):
    """Poll until the channel is idle. No sleep: the old 10 ms sleep made every inference take 20+ ms."""
    t0 = time.perf_counter()
    while not ch.idle:
        if time.perf_counter() - t0 > timeout:
            raise RuntimeError(f"DMA {name} stalled after {timeout} s")


def load_overlay():
    """Program the PL and return infer(feat600) -> (logits, softmax)."""
    ol = Overlay(BITSTREAM)
    if 'cnn_axi_0' not in ol.ip_dict:
        raise SystemExit("cnn_axi_0 has no AXI-Lite control -- wrong bitstream?")
    dma, cnn = ol.axi_dma_0, ol.cnn_axi_0
    inb  = allocate(shape=(TIMESTEPS * CHANNELS,), dtype=np.int16)   # 600
    outb = allocate(shape=(N_CLASSES,),            dtype=np.int16)   # 7

    def infer(feat600):
        x = np.asarray(feat600, dtype=np.float32).reshape(TIMESTEPS, CHANNELS)
        inb[:] = to_fixed(x).reshape(-1)
        inb.flush()
        cnn.write(0x00, 0x01)            # pulse ap_start -> one clean frame
        dma.recvchannel.transfer(outb)  # arm receiver
        dma.sendchannel.transfer(inb)   # send input
        dma_wait(dma.sendchannel, "SEND")
        dma_wait(dma.recvchannel, "RECV")
        outb.invalidate()
        z = np.asarray(outb).astype(np.float64) / (1 << OUT_FRAC)   # fixed-point logits -> float
        p = np.exp(z - z.max()); p /= p.sum()                       # softmax (exact, in float)
        return z, p

    return infer


def first_sample():
    """First row whose label is in KEEP (the same row test_bitstream3.py starts with)."""
    for chunk in pd.read_csv(CSV, chunksize=500, low_memory=False):
        hit = chunk[chunk['label'].astype(str).isin(KEEP)]
        if len(hit):
            row = hit.iloc[0]
            return row.drop('label').to_numpy(dtype=np.float32), KEEP.index(str(row['label']))
    raise SystemExit(f"no rows labelled {KEEP} in {CSV}")


def time_one(infer, x, repeats=10):
    """Median time of one inference in seconds, after a warm-up call.
    One run on its own is noisy in Python, so take the median of a few."""
    infer(x)
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        infer(x)
        times.append(time.perf_counter() - t0)
    return float(np.median(times))


# ---- power ----

def record_loop(infer, x, n, sensors):
    """Run infer(x) n times while PMBus samples every rail. Returns (watts per sample, seconds)."""
    rec = pynq.DataRecorder(*sensors)
    t0 = time.perf_counter()
    with rec.record(INTERVAL):
        for _ in range(n):
            infer(x)
    elapsed = time.perf_counter() - t0
    samples = rec.frame.drop(columns="Mark")
    samples.columns = [c.replace("_power", "") for c in samples.columns]
    samples["TOTAL"] = samples.sum(axis=1)
    return samples, elapsed


def report(samples, n, elapsed):
    """Print the means and return them with one summary row for power_summary.csv."""
    mean = samples.mean()
    other = mean["TOTAL"] - mean[KEY_RAILS].sum()
    mj = 1000 * mean["TOTAL"] * elapsed / n
    print(f"\nMeasured {n} inferences in {elapsed:.1f} s "
          f"({1000 * elapsed / n:.3f} ms each, {n / elapsed:.0f} per s)")
    print("\nMean power over the loop:")
    for r in KEY_RAILS:
        print(f"  {r:9s}{mean[r]:7.3f} W   {WHAT[r]}")
    print(f"  {'Other':9s}{other:7.3f} W   the other 6 rails")
    print(f"  {'TOTAL':9s}{mean['TOTAL']:7.3f} W")
    print(f"\nEnergy per inference: {mj:.3f} mJ")
    row = {"label": LABEL, "inferences": n, "seconds": round(elapsed, 2),
           "ms_per_inf": 1000 * elapsed / n, **{r: mean[r] for r in KEY_RAILS},
           "Other": other, "TOTAL": mean["TOTAL"], "mJ_per_inf": mj, **KNOBS}
    return mean, row


# ---- plots ----

def _style(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def plot_run(samples, mean, title, path):
    """One panel per key rail plus TOTAL, each with its mean. Every panel spans the same watts."""
    panels = KEY_RAILS + ["TOTAL"]
    secs = (samples.index - samples.index[0]).total_seconds().to_numpy()
    span = max(0.3, 1.2 * max(samples[p].max() - samples[p].min() for p in panels))
    fig, axes = plt.subplots(len(panels), 1, sharex=True, figsize=(8, 9), facecolor=SURFACE)
    for ax, name, color in zip(axes, panels, COLORS[:4] + [INK]):
        mid = (samples[name].max() + samples[name].min()) / 2
        ax.plot(secs, samples[name], color=color, linewidth=1.6)
        ax.axhline(mean[name], color=MUTED, linewidth=1)
        _style(ax)
        ax.set_ylim(mid - span / 2, mid + span / 2)
        ax.ticklabel_format(axis="y", useOffset=False)
        ax.set_ylabel("W", color=MUTED)
        ax.set_title(f"{name}  ({WHAT[name]})   mean {mean[name]:.3f} W",
                     loc="left", color=INK, fontsize=10)
    axes[-1].set_xlabel("Seconds into the inference loop", color=MUTED)
    fig.suptitle(title, x=0.01, ha="left", color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(path, dpi=120, facecolor=SURFACE)
    plt.close(fig)


def plot_compare(summary, path):
    """One stacked bar per label (its latest run), total power and energy on top."""
    s = summary.drop_duplicates("label", keep="last").set_index("label")
    fig, ax = plt.subplots(figsize=(8, 4.5), facecolor=SURFACE)
    s[KEY_RAILS + ["Other"]].plot(kind="bar", stacked=True, color=COLORS, width=0.6,
                                  edgecolor=SURFACE, linewidth=1.5, ax=ax)
    for i, (total, mj) in enumerate(zip(s["TOTAL"], s["mJ_per_inf"])):
        ax.annotate(f"{total:.2f} W\n{mj:.2f} mJ/inf", (i, total), xytext=(0, 4),
                    textcoords="offset points", ha="center", va="bottom", color=INK, fontsize=9)
    _style(ax)
    ax.set_ylim(0, s["TOTAL"].max() * 1.2)
    ax.set_ylabel("Mean power (W)", color=MUTED)
    ax.set_xlabel("")
    ax.tick_params(axis="x", rotation=0)
    ax.set_title("Mean power by rail per run", loc="left", color=INK)
    handles, labels = ax.get_legend_handles_labels()     # reversed so it reads like the stack
    ax.legend(handles[::-1], labels[::-1], frameon=False, loc="upper left", bbox_to_anchor=(1, 1))
    fig.tight_layout()
    fig.savefig(path, dpi=120, facecolor=SURFACE)
    plt.close(fig)


# ---- knobs (writing to /sys needs root, like loading the overlay) ----

def sysfs(path, value=None):
    """Read a sysfs file, or write `value` to it."""
    if value is None:
        with open(path) as f:
            return f.read().strip()
    with open(path, "w") as f:
        f.write(str(value))


def rfkill_path(kind):
    """sysfs folder of the rfkill switch of this type ("bluetooth" or "wlan")."""
    for d in sorted(glob.glob("/sys/class/rfkill/rfkill*")):
        if sysfs(d + "/type") == kind:
            return d
    raise RuntimeError(f"no {kind} switch under /sys/class/rfkill on this image")


def wifi_power_save(on=None, iface="wlan0"):
    """Read Wi-Fi power save (on=None), or turn it on or off."""
    if on is None:
        out = subprocess.run(["iw", "dev", iface, "get", "power_save"],
                             capture_output=True, text=True, check=True).stdout
        return out.strip().endswith("on")
    subprocess.run(["iw", "dev", iface, "set", "power_save", "on" if on else "off"], check=True)


def apply_knobs(k, undo):
    """Apply every knob that isn't None, pushing a function that undoes it onto `undo`."""
    if k["cpu_khz"] is not None:
        gov = sysfs(CPUFREQ + "scaling_governor")
        khz = sysfs(CPUFREQ + "scaling_cur_freq")
        sysfs(CPUFREQ + "scaling_governor", "userspace")   # all four A53 cores share this clock
        sysfs(CPUFREQ + "scaling_setspeed", k["cpu_khz"])

        def undo_cpu():
            sysfs(CPUFREQ + "scaling_governor", gov)
            if gov == "userspace":
                sysfs(CPUFREQ + "scaling_setspeed", khz)
        undo.append(undo_cpu)
    if k["cpu_cores"] is not None:
        paths = [f"/sys/devices/system/cpu/cpu{c}/online" for c in (1, 2, 3)]   # cpu0 stays on
        before = [sysfs(p) for p in paths]
        undo.append(lambda: [sysfs(p, v) for p, v in zip(paths, before)])
        for c, p in zip((1, 2, 3), paths):
            sysfs(p, int(c < k["cpu_cores"]))
    if k["pl_mhz"] is not None:
        mhz = Clocks.fclk0_mhz
        undo.append(lambda: setattr(Clocks, "fclk0_mhz", mhz))
        Clocks.fclk0_mhz = k["pl_mhz"]
    if k["bluetooth"] is not None:
        soft = rfkill_path("bluetooth") + "/soft"
        bt = sysfs(soft)
        undo.append(lambda: sysfs(soft, bt))
        sysfs(soft, 0 if k["bluetooth"] else 1)
    if k["wifi_power_save"] is not None:
        ps = wifi_power_save()
        undo.append(lambda: wifi_power_save(ps))
        wifi_power_save(k["wifi_power_save"])


# ---- run ----

def main():
    rails = pynq.get_rails()
    sensors = [r.power for r in rails.values() if r.power is not None]   # in1..in11 are voltage only
    missing = [r for r in KEY_RAILS if r not in rails or rails[r].power is None]
    if missing:
        raise SystemExit(f"no power reading for {missing}; this image has: {list(rails)}")

    infer = load_overlay()
    x, true = first_sample()
    z, p = infer(x)
    print(f"check: pred {int(p.argmax())}, true {true}, logits {np.round(z, 3)}")

    tuned = {k: v for k, v in KNOBS.items() if v is not None}
    print(f"run '{LABEL}', knobs: {tuned or 'none (baseline)'}")
    undo = []
    try:
        apply_knobs(KNOBS, undo)
        if tuned:
            time.sleep(SETTLE)
        t_one = time_one(infer, x)
        n = max(1, round(DURATION / t_one))
        print(f"one inference: {1000 * t_one:.3f} ms (median of 10) -> {n} inferences for ~{DURATION} s")
        samples, elapsed = record_loop(infer, x, n, sensors)
    finally:
        for u in reversed(undo):
            u()

    mean, row = report(samples, n, elapsed)
    samples.to_csv(f"power_{LABEL}_samples.csv")
    first = not os.path.exists("power_summary.csv")
    pd.DataFrame([row]).to_csv("power_summary.csv", mode="a", header=first, index=False)
    plot_run(samples, mean, f"{LABEL}: {n} inferences in {elapsed:.0f} s, "
             f"{row['mJ_per_inf']:.2f} mJ per inference", f"power_{LABEL}.png")
    written = [f"power_{LABEL}.png", f"power_{LABEL}_samples.csv", "power_summary.csv"]
    summary = pd.read_csv("power_summary.csv")
    if summary["label"].nunique() > 1:
        plot_compare(summary, "power_compare.png")
        written.append("power_compare.png")
    print("\nwrote " + ", ".join(written))


if __name__ == "__main__":
    main()

