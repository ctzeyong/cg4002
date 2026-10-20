# Provisional ESP pipeline-test firmware

This folder is a separate copy of `esp32_continuous` configured for end-to-end pipeline testing. Upload `esp32_continuous.ino` from this folder.

- `RECORD_MODE=false`
- Model ID: `342c4355152c26f4`
- Model: eight-tree depth-four forest
- Training: days 1 and 2 with corrected labels and complete-window training
- Validation: day 3
- Start threshold: 0.5
- Stop threshold: 0.2
- Stop hold: 200 ms

This provisional model did not meet final validation targets. It retained 87.72% of gesture samples, retained 29.08% of background, and achieved 69.12% packet precision and 67.14% packet recall. Replace it after collecting more independent training and validation data.

The sketch reports decisions and `GESTURE_READY` over serial. Bluetooth transmission and sending the captured 100 x 6 tensor are not implemented; `g.frames` is the integration point.

The original `esp32_continuous` folder remains in recording mode with its safe untrained placeholder model.
