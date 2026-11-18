# Hardware

This directory contains the hardware-related resources for Group 16's gesture-recognition system, including enclosure designs, IMU data collection tools, training data, and an ESP32 motion-detection model.

## Contents

- `CAD/` — FreeCAD and STL files for the ESP32 and MPU6050 enclosures.
- `Data Collection/` — ESP32 firmware and Python tools for recording, labelling, preparing, and exporting gesture data. See its [README](Data%20Collection/README.md) for setup and usage.
- `Dataset for ESP model/` — Recorded MPU6050 gesture data with labels and metadata used for model development.
- `ESP model training result/` — Exported model, gate configuration, and validation report. The current model is provisional and intended for pipeline testing only.

Source code for the ESP32 that is used for the actual game is in ../Comms/firebeetle

## Hardware overview

The system uses an ESP32 with an MPU6050 mounted on the hand. Motion is sampled at 500 Hz, segmented on the ESP32, and converted into a 100 × 6 accelerometer/gyroscope input for downstream gesture classification.

Start with the data-collection README for wiring, firmware upload, recording, annotation, and model-export instructions.
