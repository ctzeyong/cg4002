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
    IMU_RECOVERING = 3,  // the readings just restarted after a reset or a gap, fields are zero
};

// Starts I2C and sets the IMU up. Returns false if it did not answer.
bool imuBegin(uint8_t player);

// Reads the IMU and runs the gesture detector. Call it on every pass of loop(), streaming or
// not, so the detector gets every reading and the IMU's buffer never fills up.
void imuService();

// Gives the newest reading imuService() got (ax ay az gx gy gz). out is all zeros unless it
// returns IMU_OK, which needs a reading newer than the last call.
ImuStatus imuRead(int16_t out[6]);

// sent in the packet's reserved[1], so the board knows where each gesture ends
enum GestureFlag : int16_t {
    GESTURE_NONE = 0,
    GESTURE_ACTIVE = 1,    // Hardware's gesture detector says the hand may be making a gesture
    GESTURE_REJECTED = 2,  // sent once, on the packet after the last 1 of a gesture the detector threw away
};

// Gives the flag for the packet carrying this reading. Called with every reading imuRead() gives
GestureFlag gestureFlag(const int16_t sample[6], ImuStatus status);

// Prints the detector's counts since the last call to serial, then starts them again.
void imuPrintStats();
