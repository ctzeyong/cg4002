"""Export the continuous recordings into a new training dataset folder (new_gesture_100hz format).

    python -m gesture_pipeline.export_dataset --out new_gesture_100hz_v2

Every labelled symbol interval of every reviewed recording under SESSIONS_DIR (subfolders
included) is cropped to [start_us, end_us), decimated 500 -> 100 Hz by taking every 5th sample
(processing decimate-500to100-v1), quantized to int16 counts (accel x8192, gyro x65.5, round
half away from zero, clamp to int16) and written as one CSV per gesture, plus index.csv /
index.json / skipped.json / CLOUD_README.txt. The format and encoding are identical to
new_gesture_100hz, so data.load reads it unchanged (DataConfig(dir=<out>)).

Not exported: `ignore` intervals, and `noise` intervals (in the noise recordings those mark
random gestures of the 7 classes; the noise class is the unlabelled background, read straight
from the recordings by data.load_background). Crops across a sensor gap are skipped. The output
folder (relative to DATA_ROOT, or absolute) must be new or empty, so nothing stale mixes in.
"""
import argparse
import csv
import glob
import json
import os

import numpy as np

from .config import DATA_ROOT, SESSIONS_DIR, RAW_PERIOD_US, NOISE
from .data import SESSION_CHANNELS, SESSION_COUNTS, DECIMATE

COUNT_CHANNELS = ["ax_counts", "ay_counts", "az_counts", "gx_counts", "gy_counts", "gz_counts"]
NOT_EXPORTED = ("ignore", NOISE)
PROCESSING = "decimate-500to100-v1"
ENCODING = "imu-counts-int16-v1"


def quantize(g_dps):
    """(T,6) g / deg/s floats -> int16 counts: scale, round half away from zero, clamp.
    Returns (counts, number of clamped values)."""
    c = np.asarray(g_dps, np.float64) * SESSION_COUNTS
    r = np.copysign(np.floor(np.abs(c) + 0.5), c)
    clipped = int(np.count_nonzero((r < -32768) | (r > 32767)))
    return np.clip(r, -32768, 32767).astype(np.int16), clipped


def export(sessions_dir, out_dir):
    import pandas as pd
    if os.path.exists(out_dir) and os.listdir(out_dir):
        raise SystemExit(f"{out_dir} is not empty; pick a new folder so no stale examples mix in")
    os.makedirs(out_dir, exist_ok=True)
    labels = sorted(glob.glob(os.path.join(sessions_dir, "**", "*.labels.json"), recursive=True))
    index, skipped, sid = [], [], 0
    for lp in labels:
        doc = json.load(open(lp))
        cp = lp[:-len(".labels.json")] + ".csv"
        src = os.path.basename(cp)
        if not doc.get("reviewed") or not os.path.exists(cp):
            skipped.append(dict(source_csv=src, reason="unreviewed" if os.path.exists(cp) else "no csv"))
            continue
        symbols = [iv for iv in doc["intervals"] if iv["label"] not in NOT_EXPORTED]
        if not symbols:
            continue
        meta = json.load(open(cp[:-len(".csv")] + ".meta.json"))
        df = pd.read_csv(cp, usecols=["esp_time_us", *SESSION_CHANNELS])
        t = df["esp_time_us"].to_numpy(np.int64)
        x = df[SESSION_CHANNELS].to_numpy(np.float64)
        part, sess = meta.get("participant", ""), meta.get("session", "")
        i = 0
        for iv in symbols:
            m = (t >= iv["start_us"]) & (t < iv["end_us"])
            tt, raw = t[m], x[m]
            reason = ("fewer_than_two_readings" if len(raw) < 2 * DECIMATE else
                      "sensor_gap" if np.any(np.diff(tt) != RAW_PERIOD_US) else None)
            if reason:
                skipped.append(dict(source_csv=src, label=iv["label"], start_us=iv["start_us"],
                                    end_us=iv["end_us"], reason=reason))
                continue
            counts, clipped = quantize(raw[::DECIMATE])
            td = tt[::DECIMATE]
            name = f"s{sid:03d}_gesture{i:04d}.csv"
            with open(os.path.join(out_dir, name), "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["step", "time_from_start_s", *COUNT_CHANNELS])
                w.writerows([j, format((td[j] - td[0]) / 1e6, ".9f"), *map(int, row)]
                            for j, row in enumerate(counts))
            index.append(dict(
                file=name, label=iv["label"], source="labels", split="train",
                drawing_mode=iv.get("drawing_mode", ""), direction=iv.get("direction", ""),
                posture=iv.get("posture", ""), source_csv=src, participant=part, session=sess,
                group=f"{part}_{sess}", mode="variable", steps=len(counts), raw_count=len(raw),
                start_us=int(tt[0]), end_us=int(tt[-1]) + RAW_PERIOD_US,
                duration_us=len(raw) * RAW_PERIOD_US, raw_hz=500, decimated_hz=500 // DECIMATE,
                processing=PROCESSING, encoding=ENCODING, dtype="int16",
                accel_counts_per_g=8192, gyro_counts_per_dps=65.5, clipped_values=clipped))
            i += 1
        sid += 1
    with open(os.path.join(out_dir, "index.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(index[0]))
        w.writeheader(); w.writerows(index)
    json.dump(index, open(os.path.join(out_dir, "index.json"), "w"), indent=2)
    json.dump(skipped, open(os.path.join(out_dir, "skipped.json"), "w"), indent=2)
    with open(os.path.join(out_dir, "CLOUD_README.txt"), "w", encoding="utf-8") as f:
        f.write(f"{os.path.basename(out_dir)}: raw int16 counts decimated 500 -> 100 Hz "
                f"(processing {PROCESSING})\n\nExported by gesture_pipeline.export_dataset from "
                f"{sessions_dir}: every labelled\nsymbol of every reviewed recording, cropped, every "
                "5th sample (true 100 Hz, variable length),\nquantized to int16 counts (accel x8192, "
                "gyro x65.5, round half away from zero, clamp\n[-32768,32767]). One CSV per gesture; "
                "columns: step, time_from_start_s, ax_counts..gz_counts\n(six signed int16; exclude "
                "step & time). index.csv maps file -> label, participant, variant,\nsource. ignore and "
                "noise intervals are not exported (noise intervals are random gestures).\n")
    return index, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="output folder, relative to DATA_ROOT or absolute")
    ap.add_argument("--sessions", default=SESSIONS_DIR, help="recordings folder (default: SESSIONS_DIR)")
    a = ap.parse_args()
    out = a.out if os.path.isabs(a.out) else os.path.join(DATA_ROOT, a.out)
    index, skipped = export(a.sessions, out)
    counts = {}
    for r in index:
        counts[r["label"]] = counts.get(r["label"], 0) + 1
    print(f"exported {len(index)} gestures to {out}: {dict(sorted(counts.items()))}; "
          f"skipped {len(skipped)} (see skipped.json)")


if __name__ == "__main__":
    main()
