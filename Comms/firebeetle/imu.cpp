// One MPU6050 over I2C, behind the interface in imu.h.
//
// Two modes. Healthy: each imuRead() does one 14-byte read. Offline: imuRead() does no I2C
// and returns IMU_MISSING. Once a second, in either mode, the chip's settings are read back
// to catch a reset or an unplugged IMU, and to bring it back. Every I2C call is checked, so
// a dead chip can never pass for a working one.
#include "imu.h"

#include <Arduino.h>
#include <Wire.h>
#include <esp_log.h>

// --- wiring and chip settings -------------------------------------------------------------

// ASSUMED until Hardware confirms them (docs/integration/hardware-comms.md, "Undecided for now")
static constexpr uint8_t IMU_ADDRESS = 0x68;    // AD0 on GND
static constexpr int SDA_PIN = 21;
static constexpr int SCL_PIN = 22;
static constexpr uint8_t DLPF_42_HZ = 0x03;     // as in Hardware's dual_MPU6050_test_v2
static constexpr uint8_t RATE_DIV_500_HZ = 1;   // 1 kHz / (1 + 1), the dataset's capture rate

static constexpr uint32_t I2C_CLOCK_HZ = 400000;
static constexpr uint16_t I2C_TIMEOUT_MS = 5;   // the default 50 ms would stall BLE in loop()
static constexpr uint32_t CHECK_PERIOD_MS = 1000;

// register addresses, from the MPU6050 register map
static constexpr uint8_t REG_SMPLRT_DIV = 0x19;
static constexpr uint8_t REG_CONFIG = 0x1A;
static constexpr uint8_t REG_GYRO_CONFIG = 0x1B;
static constexpr uint8_t REG_ACCEL_CONFIG = 0x1C;
static constexpr uint8_t REG_ACCEL_XOUT_H = 0x3B;  // start of the 14 data bytes
static constexpr uint8_t REG_PWR_MGMT_1 = 0x6B;

static_assert(IMU_ACCEL_RANGE_G == 4 && IMU_GYRO_RANGE_DPS == 500,
              "imu.h ranges changed: update the two range values in SETTINGS to match");

struct Setting {
    uint8_t reg;
    uint8_t value;
};

// written in this order, and read back to check the chip still holds them
static constexpr Setting SETTINGS[] = {
    // a reset puts PWR_MGMT_1 back to 0x40 (asleep), which is how the check spots one
    {REG_PWR_MGMT_1, 0x01},    // awake, clocked from the X gyro
    {REG_SMPLRT_DIV, RATE_DIV_500_HZ},
    {REG_CONFIG, DLPF_42_HZ},
    {REG_GYRO_CONFIG, 0x08},   // ±500 °/s
    {REG_ACCEL_CONFIG, 0x08},  // ±4 g
};

static bool g_online = false;
static uint32_t g_lastCheckMs = 0;

// --- I2C helpers --------------------------------------------------------------------------

static bool writeRegister(uint8_t reg, uint8_t value) {
    Wire.beginTransmission(IMU_ADDRESS);
    Wire.write(reg);
    Wire.write(value);
    return Wire.endTransmission() == 0;
}

// true only if all len bytes came back
static bool readRegisters(uint8_t reg, uint8_t *buf, size_t len) {
    Wire.beginTransmission(IMU_ADDRESS);
    Wire.write(reg);
    // false keeps the bus for the read, so nothing is sent until requestFrom, and only its
    // byte count says whether the chip answered
    Wire.endTransmission(false);
    if (Wire.requestFrom(IMU_ADDRESS, len) != len) {
        return false;
    }
    for (size_t i = 0; i < len; i++) {
        buf[i] = Wire.read();
    }
    return true;
}

// two bytes, high byte first
static int16_t bigEndian(const uint8_t *bytes) {
    return static_cast<int16_t>((bytes[0] << 8) | bytes[1]);
}

// --- chip state ---------------------------------------------------------------------------

enum class Check { NO_ANSWER, CHANGED, MATCHES };

// stops at the first failed read, so an offline check costs a single I2C attempt
static Check readBackSettings() {
    for (const Setting &s : SETTINGS) {
        uint8_t value;
        if (!readRegisters(s.reg, &value, 1)) {
            return Check::NO_ANSWER;
        }
        if (value != s.value) {
            return Check::CHANGED;
        }
    }
    return Check::MATCHES;
}

// writes every setting, then confirms by reading them back rather than trusting the writes
static bool setUp() {
    for (const Setting &s : SETTINGS) {
        if (!writeRegister(s.reg, s.value)) {
            return false;
        }
    }
    return readBackSettings() == Check::MATCHES;
}

// --- interface (imu.h) --------------------------------------------------------------------

bool imuBegin(uint8_t player) {
    (void)player;  // unused until Hardware adds per-board calibration offsets
    Wire.begin(SDA_PIN, SCL_PIN, I2C_CLOCK_HZ);
    Wire.setTimeOut(I2C_TIMEOUT_MS);
    // the ESP-IDF I2C driver prints four error lines on every failed call, and printing them
    // stalled loop() for ~13 ms, so turn its logging off
    esp_log_level_set("i2c.master", ESP_LOG_NONE);
    g_online = setUp();
    g_lastCheckMs = millis();
    return g_online;
}

ImuStatus imuRead(int16_t out[6]) {
    for (int i = 0; i < 6; i++) {
        out[i] = 0;
    }

    const uint32_t now = millis();
    if (now - g_lastCheckMs >= CHECK_PERIOD_MS) {
        g_lastCheckMs = now;
        switch (readBackSettings()) {
            case Check::NO_ANSWER:
                g_online = false;
                break;
            case Check::MATCHES:
                g_online = true;
                break;
            case Check::CHANGED:
                // the chip reset. set it up again and report the reset on this packet, so
                // the laptop knows it happened
                g_online = setUp();
                if (g_online) {
                    return IMU_RECOVERING;
                }
                break;
        }
    }

    if (!g_online) {
        return IMU_MISSING;
    }

    // 14 bytes: accel x y z (0-5), temperature (6-7, skipped), gyro x y z (8-13)
    uint8_t raw[14];
    if (!readRegisters(REG_ACCEL_XOUT_H, raw, sizeof(raw))) {
        g_online = false;
        return IMU_MISSING;
    }
    out[0] = bigEndian(raw + 0);
    out[1] = bigEndian(raw + 2);
    out[2] = bigEndian(raw + 4);
    out[3] = bigEndian(raw + 8);
    out[4] = bigEndian(raw + 10);
    out[5] = bigEndian(raw + 12);
    return IMU_OK;
}
