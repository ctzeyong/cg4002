# Provisional ESP pipeline-test model

This model is exported for end-to-end project pipeline testing. It did not meet the final validation requirements and must be replaced after collecting more independent training and validation data.

- Training data: days 1 and 2, corrected labels
- Validation data: day 3
- Training policy: complete 300 ms windows only; mixed gesture/background boundary windows excluded
- Model: eight-tree depth-four forest
- Start threshold: 0.5
- Stop threshold: 0.2
- Stop hold: 200 ms
- Gesture samples retained: 87.72%
- Gestures retaining at least 95% of their samples: 33/70
- Background retained: 29.08%
- Packet precision: 69.12%
- Packet recall: 67.14%
- Merged packets: 6

The full twelve-setting validation report is saved as `selection_report.json`. The exported firmware header is `esp32_continuous/model_data.h`.
