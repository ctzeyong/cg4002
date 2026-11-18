Gesture dataset: raw int16 counts decimated 500 -> 100 Hz (processing decimate-500to100-v1)

Built from raw_int16/ (int16 counts) with gesture spans from sessions/*.labels.json,
matched by esp_time_us. NOT sinc-resampled: each gesture is the raw samples in its
labelled [start_us, end_us) window, keeping every 5th sample (true 100 Hz, 10 ms apart).

One CSV per gesture. Columns: step, time_from_start_s, ax_counts, ay_counts, az_counts,
gx_counts, gy_counts, gz_counts. The six sensor columns are signed int16; exclude step
and time_from_start_s from model input. Row count VARIES per gesture (longer gesture =
more rows); pad/mask to batch. Read index.csv / index.json for label, split, variant,
source_csv, duration and row count (steps). skipped.json lists excluded crops.

Encoding: acceleration 8192 counts/g; gyro 65.5 counts/(degree/s); same scale as
cloud_manual_day1_int16 and raw_int16 (imu-counts-int16-v1). Decode to physical units by
dividing, or train on counts directly; fit any normalization on training data only.
Gestures marked ignore or containing sensor gaps are excluded. Keep groups/days separate
across train/validation/test. Raw sessions and raw_int16 are unchanged.
