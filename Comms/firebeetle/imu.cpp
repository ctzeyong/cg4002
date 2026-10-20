// One MPU6050 over I2C, behind the interface in imu.h, and Hardware's gesture detector.
//
// The IMU writes every reading into its FIFO at 500 Hz. imuService() empties the FIFO on every
// pass of loop(), gives each reading to the detector and keeps the newest one for the next
// packet. The detector is gesture_core.h, segments.h and model_data.h, copied unchanged from
// Hardware/Data Collection/esp32_continuous_pipeline_test.
//
// Two modes. Healthy: the FIFO is read. Offline: no I2C at all. Once a second, in either mode,
// the chip's settings are read back to catch a reset or an unplugged IMU, and to bring it back.
// Every I2C call is checked, so a dead chip can never pass for a working one.
#include "imu.h"

#include <Arduino.h>
#include <Wire.h>
#include <esp_log.h>

#include "gesture_core.h"
#include "segments.h"

// the model in Hardware's esp32_continuous folder is a placeholder that never detects anything
static_assert(MODEL_READY,
              "model_data.h is the untrained placeholder: copy the trained one from "
              "Hardware/Data Collection/esp32_continuous_pipeline_test");

// --- wiring and chip settings -------------------------------------------------------------

// ASSUMED until Hardware confirms them (docs/integration/hardware-comms.md, "Undecided for now")
static constexpr uint8_t IMU_ADDRESS = 0x68;    // AD0 on GND
static constexpr int SDA_PIN = 21;
static constexpr int SCL_PIN = 22;
static constexpr uint8_t DLPF_42_HZ = 0x03;     // Hardware's capture setting, DLPF 3
static constexpr uint8_t RATE_DIV_500_HZ = 1;   // 1 kHz / (1 + 1), the rate the detector needs

static constexpr uint32_t I2C_CLOCK_HZ = 400000;
static constexpr uint16_t I2C_TIMEOUT_MS = 5;   // the default 50 ms would stall BLE in loop()
static constexpr uint32_t CHECK_PERIOD_MS = 1000;
static constexpr uint32_t SETTLE_MS = 100;      // Hardware's sketch waits this long after waking the chip
static constexpr uint32_t STALL_MS = 50;        // no reading for this long means the FIFO stopped

static constexpr int FRAME_BYTES = 12;          // one reading in the FIFO, ax ay az gx gy gz
static constexpr int FIFO_FULL = 1024;          // a full FIFO has overwritten readings
static constexpr int MAX_FRAMES_PER_PASS = 8;   // so catching up after a hold-up doesn't stall BLE
static constexpr int16_t CLIPPED = 32700;       // Hardware's threshold for a saturated reading

// register addresses, from the MPU6050 register map
static constexpr uint8_t REG_SMPLRT_DIV = 0x19;
static constexpr uint8_t REG_CONFIG = 0x1A;
static constexpr uint8_t REG_GYRO_CONFIG = 0x1B;
static constexpr uint8_t REG_ACCEL_CONFIG = 0x1C;
static constexpr uint8_t REG_FIFO_EN = 0x23;
static constexpr uint8_t REG_USER_CTRL = 0x6A;
static constexpr uint8_t REG_PWR_MGMT_1 = 0x6B;
static constexpr uint8_t REG_FIFO_COUNT_H = 0x72;  // two bytes, high first
static constexpr uint8_t REG_FIFO_R_W = 0x74;

static constexpr uint8_t FIFO_ACCEL_AND_GYRO = 0x78;  // accel then gyro in each reading, no temperature
static constexpr uint8_t USER_CTRL_FIFO_ON = 0x40;
static constexpr uint8_t USER_CTRL_FIFO_RESET = 0x04; // clears itself, so it reads back as FIFO_ON

static_assert(IMU_ACCEL_RANGE_G == 4 && IMU_GYRO_RANGE_DPS == 500,
              "imu.h ranges changed: update the two range values in SETTINGS to match");
static_assert(gesture::RAW_PERIOD_US == 2000, "the detector expects the 500 Hz rate set below");

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
    {REG_FIFO_EN, FIFO_ACCEL_AND_GYRO},
    {REG_USER_CTRL, USER_CTRL_FIFO_ON},
};

static bool g_online = false;
static bool g_settled = false;          // readings are only used once the chip has settled
static uint32_t g_lastCheckMs = 0;
static uint32_t g_onlineSinceMs = 0;
static uint32_t g_lastReadingMs = 0;

static int16_t g_newest[6] = {};
static uint32_t g_fresh = 0;            // readings since the last imuRead()
// readings restarted since the last packet. reported only after an OK packet, since the board
// already starts again on anything else, so it never joins readings across the gap
static bool g_restarted = false;
static bool g_lastPacketOk = false;

static gesture::Detector g_detector;
static gesture::Segmenter g_segmenter;
static uint32_t g_readingSeq = 0;
static int64_t g_readingUs = 0;         // each reading is 2 ms after the one before
static int64_t g_restartUs = 0;         // so a gesture is never backdated to before a restart

// counts since the last imuPrintStats()
struct Stats {
    uint32_t readings, decisions, started, completed, rejected;
    uint32_t overflows, stalls, detectorGaps, clipped;
    float maxScore;
};
static Stats g_stats = {};

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

static void goOnline(uint32_t now) {
    g_online = true;
    g_settled = false;
    g_onlineSinceMs = now;
}

// coming back always goes through the settling time, which restarts the readings
static void goOffline() {
    g_online = false;
    g_settled = false;
}

// empties the FIFO and starts the detector again, so it never joins readings across a gap
static bool restartReadings() {
    const bool ok = writeRegister(REG_FIFO_EN, 0) &&
                    writeRegister(REG_USER_CTRL, USER_CTRL_FIFO_RESET) &&
                    writeRegister(REG_USER_CTRL, USER_CTRL_FIFO_ON) &&
                    writeRegister(REG_FIFO_EN, FIFO_ACCEL_AND_GYRO);
    g_detector.reset();
    g_segmenter.reset();
    g_fresh = 0;
    g_restarted = true;
    g_restartUs = g_readingUs;
    g_lastReadingMs = millis();
    return ok;
}

// gives one reading to the detector, the way Hardware's esp32_continuous loop() does
static void detect(const int16_t counts[6]) {
    float x[6];
    for (int c = 0; c < 6; c++) {
        x[c] = counts[c] / (c < 3 ? 8192.0f : 65.5f);  // g and °/s, the detector's units
    }
    const gesture::Result r = g_detector.push(g_readingSeq++, g_readingUs, x);
    if (r.gap) {
        g_stats.detectorGaps++;  // readings go in 2 ms apart, so this would be a bug here
    }
    if (r.decisionReady) {
        g_stats.decisions++;
        if (r.score > g_stats.maxScore) {
            g_stats.maxScore = r.score;
        }
        const gesture::SegmentEvent e = g_segmenter.step(r.score, r.energy, g_readingUs, g_restartUs);
        if (e.kind == gesture::SEG_START) {
            g_stats.started++;
        } else if (e.kind == gesture::SEG_COMPLETE) {
            g_stats.completed++;
        } else if (e.kind == gesture::SEG_REJECT) {
            g_stats.rejected++;
        }
    }
    g_readingUs += gesture::RAW_PERIOD_US;
}

// --- interface (imu.h) --------------------------------------------------------------------

bool imuBegin(uint8_t player) {
    (void)player;  // unused until Hardware adds per-board calibration offsets
    Wire.begin(SDA_PIN, SCL_PIN, I2C_CLOCK_HZ);
    Wire.setTimeOut(I2C_TIMEOUT_MS);
    // the ESP-IDF I2C driver prints four error lines on every failed call, and printing them
    // stalled loop() for ~13 ms, so turn its logging off
    esp_log_level_set("i2c.master", ESP_LOG_NONE);
    Serial.printf("[IMU] gesture model %s\n", MODEL_ID);
    const uint32_t now = millis();
    g_lastCheckMs = now;
    if (!setUp()) {
        goOffline();
        return false;
    }
    goOnline(now);
    return true;
}

void imuService() {
    const uint32_t now = millis();
    if (now - g_lastCheckMs >= CHECK_PERIOD_MS) {
        g_lastCheckMs = now;
        switch (readBackSettings()) {
            case Check::NO_ANSWER:
                goOffline();
                break;
            case Check::MATCHES:
                if (!g_online) {
                    goOnline(now);
                }
                break;
            case Check::CHANGED:
                // the chip reset. set it up again, and it reports recovering until it settles
                if (setUp()) {
                    goOnline(now);
                } else {
                    goOffline();
                }
                break;
        }
    }

    if (!g_online) {
        return;
    }
    if (!g_settled) {
        if (now - g_onlineSinceMs < SETTLE_MS) {
            return;
        }
        // throw away what the chip wrote while it settled
        if (!restartReadings()) {
            goOffline();
            return;
        }
        g_settled = true;
        return;
    }

    uint8_t countBytes[2];
    if (!readRegisters(REG_FIFO_COUNT_H, countBytes, sizeof(countBytes))) {
        goOffline();
        return;
    }
    const int count = (countBytes[0] << 8) | countBytes[1];
    if (count >= FIFO_FULL) {
        // loop() was held up for over 170 ms, so readings were lost
        g_stats.overflows++;
        if (!restartReadings()) {
            goOffline();
        }
        return;
    }

    int frames = count / FRAME_BYTES;
    if (frames == 0) {
        if (now - g_lastReadingMs > STALL_MS) {
            // the chip writes a reading every 2 ms, so the FIFO has stopped
            g_stats.stalls++;
            if (!restartReadings()) {
                goOffline();
            }
        }
        return;
    }
    if (frames > MAX_FRAMES_PER_PASS) {
        frames = MAX_FRAMES_PER_PASS;  // the rest wait for the next pass
    }
    for (int k = 0; k < frames; k++) {
        uint8_t raw[FRAME_BYTES];
        if (!readRegisters(REG_FIFO_R_W, raw, FRAME_BYTES)) {
            goOffline();
            return;
        }
        int16_t counts[6];
        bool clipped = false;
        for (int c = 0; c < 6; c++) {
            counts[c] = bigEndian(raw + 2 * c);
            if (counts[c] >= CLIPPED || counts[c] <= -CLIPPED) {
                clipped = true;
            }
        }
        if (clipped) {
            g_stats.clipped++;
        }
        detect(counts);
        memcpy(g_newest, counts, sizeof(g_newest));
        g_fresh++;
        g_stats.readings++;
    }
    g_lastReadingMs = now;
}

ImuStatus imuRead(int16_t out[6]) {
    for (int i = 0; i < 6; i++) {
        out[i] = 0;
    }
    const bool restarted = g_restarted;
    g_restarted = false;
    ImuStatus status;
    if (!g_online) {
        status = IMU_MISSING;
    } else if (!g_settled || g_fresh == 0 || (restarted && g_lastPacketOk)) {
        status = IMU_RECOVERING;
    } else {
        memcpy(out, g_newest, sizeof(g_newest));
        g_fresh = 0;
        status = IMU_OK;
    }
    g_lastPacketOk = status == IMU_OK;
    return status;
}

int16_t gestureFlag(const int16_t sample[6], ImuStatus status) {
    (void)sample;  // the detector already had this reading, and every one before it
    return (status == IMU_OK && g_segmenter.active) ? 1 : 0;
}

void imuPrintStats() {
    Serial.printf("[IMU] detector: readings=%u decisions=%u max_score=%.2f gestures started=%u "
                  "ended=%u rejected=%u, fifo overflows=%u stalls=%u, detector gaps=%u, clipped=%u\n",
                  static_cast<unsigned>(g_stats.readings), static_cast<unsigned>(g_stats.decisions),
                  g_stats.maxScore, static_cast<unsigned>(g_stats.started),
                  static_cast<unsigned>(g_stats.completed), static_cast<unsigned>(g_stats.rejected),
                  static_cast<unsigned>(g_stats.overflows), static_cast<unsigned>(g_stats.stalls),
                  static_cast<unsigned>(g_stats.detectorGaps), static_cast<unsigned>(g_stats.clipped));
    g_stats = {};
}
