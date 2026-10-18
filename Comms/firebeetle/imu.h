// The IMU interface the BLE sketch uses. Everything about the sensor itself lives in imu.cpp.
//
// Data contract imu-counts-int16-v1: six raw int16 counts in the order ax ay az gx gy gz,
// at the ranges below (8192 counts per g, 65.5 counts per degree/s).
#pragma once
#include <stdint.h>

// part of the contract with Comms and AI, so firebeetle.ino refuses to compile if they change
constexpr int16_t IMU_ACCEL_RANGE_G = 4;
constexpr int16_t IMU_GYRO_RANGE_DPS = 500;

// sent in the packet's reserved[0], so the laptop knows whether the six values are real
enum ImuStatus : int16_t {
    IMU_UNREPORTED = 0,  // firmware from before the IMU, never sent by this code
    IMU_OK = 1,
    IMU_MISSING = 2,     // no answer from the IMU, fields are zero
    IMU_RECOVERING = 3,  // the IMU had reset and was just set up again, fields are zero
};

// Starts I2C and sets the IMU up. Returns false if it did not answer.
bool imuBegin(uint8_t player);

// Reads one sample into out (ax ay az gx gy gz). out is all zeros unless it returns IMU_OK.
ImuStatus imuRead(int16_t out[6]);

// The gesture detector (to be replaced). Called with every sample imuRead() gives,
// it returns 1 while the hand may be making a gesture and 0 otherwise, and is sent in
// packet's reserved[1]
int16_t gestureFlag(const int16_t sample[6], ImuStatus status);
