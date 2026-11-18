# cloud_manual_day1_int16 — dataset summary

Auto-generated summary of `index.csv` (1485 labelled gesture examples). Each example is one CSV: 100 resampled steps x 6 IMU channels (`ax,ay,az,gx,gy,gz`) stored as int16 counts. See `CLOUD_README.txt` for the encoding/wire contract.

## Collection scope

| field | value |
|---|---|
| total examples | 1485 |
| classes | 7: arc, caret, circle, infinity, star, triangle, z |
| split | train (100%) |
| participant / session / group | p1 / day1 / p1_day1 (all) |
| steps per example | 100 (fixed, already resampled) |
| raw sample rate | 500 Hz |
| capture runs (source_csv) | 61 |

## How gesture duration is computed

`duration_s = duration_us / 1_000_000`, taken directly from the `duration_us` column of `index.csv`. That column is the annotated gesture span: `duration_us = end_us - start_us = annotation_end_us - annotation_start_us` (verified: 0 / 1485 rows disagree). `start_us`/`end_us` are capture-clock timestamps in microseconds marking the labelled start and end of the gesture in its source recording. This is the **raw** gesture length *before* resampling; every example is still resampled to exactly 100 steps, so duration does not change the model input shape — it only reflects how fast/slow the gesture was performed.

## Flagged bad example (longest duration)

**`s010_gesture0021.csv`** is almost certainly mis-segmented:

| field | value |
|---|---|
| file | `s010_gesture0021.csv` |
| class | caret |
| variant | air_front / cw / standing |
| source_csv | `sessions/p1_day1_caret_air_cw_stand.csv` |
| start_us | 936699113 |
| end_us | 959429113 |
| duration_us | 22730000 |
| **duration_s** | **22.73 s** |
| clipped_values | 0 |

For comparison the median `caret` lasts 0.51 s and the next-longest example overall is `s029_gesture0027.csv` at 7.23 s. A 22.7 s 'caret' is ~45x the class median, so the annotation window for this one capture is wrong (the resampler stretched ~0.23 s of real motion per step across 100 steps). **Recommend dropping or re-annotating this example before training.**

## Per-class duration (seconds) + variant counts

Five-number summary (min, Q1, median, Q3, max) of raw gesture duration, plus mean and the count of each variant within the class. Percentiles use linear interpolation (numpy default).

| class | n | min | Q1 | median | Q3 | max | mean | air_front | table | cw | ccw | seated | standing |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| arc | 212 | 0.59 | 0.85 | 1.04 | 1.20 | 1.65 | 1.04 | 106 | 106 | 106 | 106 | 106 | 106 |
| caret | 213 | 0.31 | 0.40 | 0.51 | 0.69 | 22.73 | 0.73 | 107 | 106 | 107 | 106 | 107 | 106 |
| circle | 212 | 0.51 | 1.00 | 1.24 | 1.47 | 2.94 | 1.25 | 106 | 106 | 106 | 106 | 106 | 106 |
| infinity | 212 | 0.75 | 1.62 | 1.80 | 2.03 | 7.23 | 1.90 | 106 | 106 | 106 | 106 | 106 | 106 |
| star | 212 | 0.70 | 1.56 | 1.72 | 1.89 | 3.43 | 1.76 | 106 | 106 | 106 | 106 | 106 | 106 |
| triangle | 212 | 0.59 | 0.79 | 0.95 | 1.15 | 2.01 | 0.99 | 106 | 106 | 106 | 106 | 106 | 106 |
| z | 212 | 0.12 | 0.68 | 0.78 | 0.99 | 2.23 | 0.87 | 106 | 106 | 106 | 106 | 106 | 106 |
| **ALL** | 1485 | 0.12 | 0.77 | 1.10 | 1.59 | 22.73 | 1.22 | 743 | 742 | 743 | 742 | 743 | 742 |

> Note: the `max` column for **caret** (22.73 s) is the flagged bad example above; its class median is unaffected.

## Per-variant duration (seconds)

Same five-number summary grouped by variant value (across all classes).

| variant | value | n | min | Q1 | median | Q3 | max | mean |
|---|---|---|---|---|---|---|---|---|
| drawing_mode | air_front | 743 | 0.12 | 0.77 | 1.18 | 1.64 | 22.73 | 1.28 |
| drawing_mode | table | 742 | 0.31 | 0.77 | 1.05 | 1.53 | 3.43 | 1.16 |
| direction | ccw | 742 | 0.33 | 0.78 | 1.13 | 1.59 | 3.16 | 1.21 |
| direction | cw | 743 | 0.12 | 0.76 | 1.08 | 1.58 | 22.73 | 1.23 |
| posture | seated | 743 | 0.31 | 0.76 | 1.10 | 1.53 | 4.57 | 1.17 |
| posture | standing | 742 | 0.12 | 0.78 | 1.10 | 1.66 | 22.73 | 1.27 |

