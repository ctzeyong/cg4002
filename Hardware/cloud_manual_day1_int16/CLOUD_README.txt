Cloud gesture dataset: imu-counts-int16-v1

Read index.csv or index.json to map each file to its label and split.
CSV input columns, in order: ax_counts, ay_counts, az_counts, gx_counts, gy_counts, gz_counts.
Read these six columns as signed int16. Exclude step and time_from_start_s from model input.
NPZ exports contain an int16 array named frames. Each example has shape (steps, 6).
Values have already been filtered and resampled; do not resample again.
Encoding: acceleration 8192 counts/g; gyro 65.5 counts/(degree/s).
After resampling: scale in float64, round nearest (ties away from zero),
clamp to [-32768, 32767], then cast to int16. Index clipped_values counts saturation.
Bluetooth contract: signed little-endian int16, row-major ax,ay,az,gx,gy,gz.
100 steps = 1200 binary payload bytes, excluding headers. CSV itself is text, not a wire packet.
The firmware transport must implement this encoding; the current capture buffer is float32.
Cloud can cast counts to float32 for training or decode to physical units first.
Use identical decoding and normalization at inference; fit normalization on training data only.
Keep collection groups/days separate across train, validation and test.
Manual-label exports contain clean labelled gestures, not background rejection examples.
