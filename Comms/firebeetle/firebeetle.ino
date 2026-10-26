// Advertises a fixed per-player service UUID and exposes two encryption-required
// characteristics:
//   SENSOR (READ | NOTIFY) — uplink. Streams SENSOR packets and returns CMD/file data.
//   CMD    (WRITE)         — downlink. Accepts CMD packets and streamed file chunks.
//
// Every packet starts with type and sequence. SENSOR, CMD and ECHO packets then carry 11
// int16 fields (six IMU readings, five reserved) and a CRC, 26 bytes in all; file packets
// carry a uint64 size/offset followed by raw bytes, and no CRC. In a SENSOR packet the
// first reserved field carries the IMU status and the second the gesture flag (imu.h).
//
// Nothing is transmitted until the link is both MTU-capable and encrypted. Both gates
// are polled at the top of loop().
//
// Once the link is encrypted the board requests a 15 ms connection interval. Left alone
// Windows settles at 60 ms, which is ~17 connection events/s shared between both boards.
//
// A CMD whose first reserved field is CMD_VIBRATE pulses the vibration motor. It is still
// echoed like any other CMD, and the laptop drops that echo.

#include <Arduino.h>
#include <NimBLEDevice.h>
#include <string.h>

#include "imu.h"

// the laptop and the AI model read the six values at these ranges
static_assert(IMU_ACCEL_RANGE_G == 4 && IMU_GYRO_RANGE_DPS == 500,
              "IMU range changed in imu.h: this is a data contract change, agree it with Comms and AI first");

#define PLAYER_ID 1 // change accordingly

#if PLAYER_ID == 1
#define SERVICE_UUID     "b1160001-0000-4000-8000-000000000001"
#define SENSOR_CHAR_UUID "b1160002-0000-4000-8000-000000000001"
#define CMD_CHAR_UUID    "b1160003-0000-4000-8000-000000000001"
#define DEVICE_NAME      "B16-Player1"
#elif PLAYER_ID == 2
#define SERVICE_UUID     "b1160001-0000-4000-8000-000000000002"
#define SENSOR_CHAR_UUID "b1160002-0000-4000-8000-000000000002"
#define CMD_CHAR_UUID    "b1160003-0000-4000-8000-000000000002"
#define DEVICE_NAME      "B16-Player2"
#else
#error "PLAYER_ID must be 1 or 2"
#endif

static constexpr uint8_t TYPE_SENSOR = 0x01;  // uplink, free-running stream
// 0x02 is reserved for EVENT (hall-effect open/closed)
static constexpr uint8_t TYPE_CMD  = 0x03;  // downlink, laptop -> board
static constexpr uint8_t TYPE_ECHO = 0x04;  // uplink, this board's reply to a CMD
static constexpr uint8_t TYPE_FILE_START = 0x10;
static constexpr uint8_t TYPE_FILE_DATA  = 0x11;
static constexpr uint8_t TYPE_FILE_END   = 0x12;

static constexpr size_t HEADER_LEN = 2;
static constexpr size_t IMU_FIELD_COUNT = 6;
static constexpr size_t RESERVED_FIELD_COUNT = 5;
static constexpr size_t IMU_STATUS_FIELD = IMU_FIELD_COUNT;  // reserved[0] of a SENSOR packet
// reserved[1], ASSUMED until hardware confirms it (gesture detection G0)
static constexpr size_t GESTURE_FLAG_FIELD = IMU_STATUS_FIELD + 1;
static constexpr size_t DATA_FIELD_COUNT = IMU_FIELD_COUNT + RESERVED_FIELD_COUNT;
static constexpr size_t FIELD_LEN = 2;
static constexpr size_t CRC_LEN = 2;
static constexpr size_t CRC_OFFSET = HEADER_LEN + DATA_FIELD_COUNT * FIELD_LEN;
static constexpr size_t PACKET_LEN = CRC_OFFSET + CRC_LEN;
static_assert(PACKET_LEN == 26, "fixed BLE packet must remain 26 bytes");
static constexpr uint16_t PREFERRED_MTU = 128;
static constexpr size_t FILE_HEADER_LEN = 10;  // type + seq + uint64 size/offset
static constexpr size_t FILE_MAX_PACKET_LEN = PREFERRED_MTU - 3;
static constexpr uint32_t PACKET_PERIOD_MS = 10;  // 10 = 100 Hz, the validated two-node rate; 6 = ~167 Hz, two-board maximum
static constexpr uint32_t IMU_TIMING_REPORT_MS = 5000;

// reserved[0] of a CMD is its kind, and field 0 a VIBRATE's pattern (ble_packet.py)
static constexpr size_t CMD_KIND_FIELD = IMU_FIELD_COUNT;
static constexpr int16_t CMD_VIBRATE = 1;
static constexpr int16_t VIBRATE_RECOGNISED = 1;  // the board classified a gesture

// Vibration motor, from hardware's bluetooth_mpu_motor_test.ino: GPIO 2 drives a transistor,
// never the motor directly. GPIO 2 is a strapping pin, so the driver must not pull it high
// at reset.
static constexpr uint8_t MOTOR_PIN = 2;
static constexpr uint8_t MOTOR_PWM_CHANNEL = 0;  // ESP32 core 2.x only
static constexpr uint32_t MOTOR_PWM_FREQUENCY_HZ = 20000;
static constexpr uint8_t MOTOR_PWM_RESOLUTION_BITS = 8;
static constexpr uint8_t MOTOR_DUTY = 128;  // 50 % of 0-255
static constexpr uint32_t MOTOR_PULSE_MS = 200;

// Requested BLE connection parameters.
// Intervals use 1.25 ms units: 12 × 1.25 ms = 15 ms.
// Timeout uses 10 ms units: 200 × 10 ms = 2 seconds.
static constexpr uint16_t FAST_CONN_INTERVAL = 12;
static constexpr uint16_t CONN_LATENCY = 0;
static constexpr uint16_t CONN_TIMEOUT = 200;
static constexpr uint32_t CONN_PARAM_REQUEST_DELAY_MS = 1000;

static NimBLEServer *g_server = nullptr;
static NimBLECharacteristic *g_sensorChar = nullptr;
static NimBLECharacteristic *g_cmdChar = nullptr;
static bool g_connected = false;
static uint16_t g_connHandle = 0;
static bool g_mtuReady = false;  // link can carry a full PACKET_LEN notification
static bool g_encrypted = false;  // link has completed pairing/encryption
static bool g_connParamsRequested = false;
static uint32_t g_encryptedAt = 0;

static uint8_t g_seq = 0;
static uint32_t g_lastTx = 0;

static uint32_t g_slowestImuServiceUs = 0;  // since the last timing report
static uint32_t g_slowestImuReadUs = 0;
static uint32_t g_slowestGestureFlagUs = 0;
static uint32_t g_imuTimingReportedAt = 0;

static bool g_motorOn = false;
static uint32_t g_motorOffAt = 0;

// Downlink hand-off. onWrite runs on the NimBLE host task; loop() drains it. Going
// through a buffer rather than notifying straight from onWrite keeps every notify()
// call on the one task, so sensor, command and file notifications stay serialised.
// One slot is enough: CMD is keystroke- or gesture-paced and file transfer is stop-and-wait,
// so the laptop does not write the next packet until this one has been returned.
static volatile bool g_downlinkPending = false;
static size_t g_downlinkLen = 0;
static uint8_t g_downlinkBuf[FILE_MAX_PACKET_LEN];
static bool g_fileTransferActive = false;


/**
 * Logs the connection interval, which ceilings how fast notifications go out.
 * Interval is in 1.25ms units. Several notifications can share one connection
 * event, so this is a budget rather than a hard packet rate.
 */
static void logConnInterval(const char *when, NimBLEConnInfo &info) {
    const float ms = info.getConnInterval() * 1.25f;
    Serial.printf("[BLE] %s: interval=%u (%.2f ms) -> %.0f events/s\n",
                  when, info.getConnInterval(), ms, 1000.0f / ms);
}

/** Reads field `i` of a packet as a little-endian int16. */
static int16_t readField(const uint8_t *buf, size_t i) {
    const uint16_t raw = static_cast<uint16_t>(buf[HEADER_LEN + i * FIELD_LEN]) |
                         (static_cast<uint16_t>(buf[HEADER_LEN + i * FIELD_LEN + 1]) << 8);
    return static_cast<int16_t>(raw);
}

/** Writes `v` into field `i` of a packet as a little-endian int16. */
static void writeField(uint8_t *buf, size_t i, int16_t v) {
    const uint16_t raw = static_cast<uint16_t>(v);
    buf[HEADER_LEN + i * FIELD_LEN] = static_cast<uint8_t>(raw & 0xFF);
    buf[HEADER_LEN + i * FIELD_LEN + 1] = static_cast<uint8_t>((raw >> 8) & 0xFF);
}

/** CRC-16/CCITT-FALSE: polynomial 0x1021, initial value 0xffff. */
static uint16_t calculateCrc(const uint8_t *data, size_t length) {
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < length; i++) {
        crc ^= static_cast<uint16_t>(data[i]) << 8;
        for (uint8_t bit = 0; bit < 8; bit++) {
            crc = (crc & 0x8000) ? static_cast<uint16_t>((crc << 1) ^ 0x1021)
                                 : static_cast<uint16_t>(crc << 1);
        }
    }
    return crc;
}

/** Writes the packet CRC little-endian into its final two bytes. */
static void writePacketCrc(uint8_t *buf) {
    const uint16_t crc = calculateCrc(buf, CRC_OFFSET);
    buf[CRC_OFFSET] = static_cast<uint8_t>(crc & 0xFF);
    buf[CRC_OFFSET + 1] = static_cast<uint8_t>((crc >> 8) & 0xFF);
}

/** Checks a complete fixed-size packet without modifying it. */
static bool hasValidPacketCrc(const uint8_t *buf) {
    const uint16_t received = static_cast<uint16_t>(buf[CRC_OFFSET]) |
                              (static_cast<uint16_t>(buf[CRC_OFFSET + 1]) << 8);
    return received == calculateCrc(buf, CRC_OFFSET);
}

/** Sets the motor's PWM duty: 0 is off. */
static void writeMotorDuty(uint8_t duty) {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcWrite(MOTOR_PIN, duty);
#else
    ledcWrite(MOTOR_PWM_CHANNEL, duty);
#endif
}

/** Attaches the motor pin to PWM and turns the motor off. */
static void configureMotor() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
    if (!ledcAttach(MOTOR_PIN, MOTOR_PWM_FREQUENCY_HZ, MOTOR_PWM_RESOLUTION_BITS)) {
        Serial.println("[MOTOR] PWM attach failed");
    }
#else
    ledcSetup(MOTOR_PWM_CHANNEL, MOTOR_PWM_FREQUENCY_HZ, MOTOR_PWM_RESOLUTION_BITS);
    ledcAttachPin(MOTOR_PIN, MOTOR_PWM_CHANNEL);
#endif
    writeMotorDuty(0);
}

/** Starts one MOTOR_PULSE_MS pulse, or restarts it if one is running. */
static void startMotorPulse() {
    writeMotorDuty(MOTOR_DUTY);
    g_motorOn = true;
    g_motorOffAt = millis() + MOTOR_PULSE_MS;
}

/** Ends the pulse once it is due. Called every pass of loop(), so nothing blocks. */
static void updateMotor() {
    if (!g_motorOn || static_cast<int32_t>(millis() - g_motorOffAt) < 0) {
        return;
    }
    writeMotorDuty(0);
    g_motorOn = false;
}

class ServerCallbacks : public NimBLEServerCallbacks {
    void onConnect(NimBLEServer *, NimBLEConnInfo &info) override {
        g_connected = true;
        g_connHandle = info.getConnHandle();
        g_mtuReady = false;    // every connection starts back at the 23-byte default
        g_encrypted = false;   // and unencrypted, bond or no bond
        g_downlinkPending = false;  // never answer data left from a previous session
        g_fileTransferActive = false;
        g_connParamsRequested = false;
        g_encryptedAt = 0;
        Serial.print("[BLE] connected: ");
        Serial.println(info.getAddress().toString().c_str());
        logConnInterval("connected", info);
    }

    void onDisconnect(NimBLEServer *, NimBLEConnInfo &, int reason) override {
        g_connected = false;
        g_mtuReady = false;
        g_encrypted = false;
        g_downlinkPending = false;
        g_fileTransferActive = false;
        g_connParamsRequested = false;
        g_encryptedAt = 0;
        Serial.printf("[BLE] disconnected reason=%d, restarting advertising\n", reason);
        NimBLEDevice::startAdvertising();
    }

    void onMTUChange(uint16_t mtu, NimBLEConnInfo &) override {
        Serial.printf("[BLE] MTU negotiated: %u\n", mtu);
    }

    // the central usually revises the interval shortly after connecting, so what
    // onConnect logged is a starting point, not what we end up streaming at
    void onConnParamsUpdate(NimBLEConnInfo &info) override {
        logConnInterval("params updated", info);
    }

    void onAuthenticationComplete(NimBLEConnInfo &info) override {
        Serial.printf("[BLE] paired: encrypted=%d bonded=%d authenticated=%d keySize=%u\n",
                      info.isEncrypted(), info.isBonded(),
                      info.isAuthenticated(), info.getSecKeySize());
    }
};

/** Validates one downlink packet and parks it for loop() to answer. */
class DownlinkCallbacks : public NimBLECharacteristicCallbacks {
    void onWrite(NimBLECharacteristic *chr, NimBLEConnInfo &) override {
        const NimBLEAttValue value = chr->getValue();
        const size_t length = value.length();
        if (length < 2) {
            Serial.println("[BLE] downlink dropped: missing header");
            return;
        }

        const uint8_t type = value[0];
        const bool validCmd = type == TYPE_CMD && length == PACKET_LEN;
        const bool validFileControl =
            (type == TYPE_FILE_START || type == TYPE_FILE_END) &&
            length == FILE_HEADER_LEN;
        const bool validFileData =
            type == TYPE_FILE_DATA &&
            length > FILE_HEADER_LEN &&
            length <= FILE_MAX_PACKET_LEN;

        if (!validCmd && !validFileControl && !validFileData) {
            Serial.printf("[BLE] downlink dropped: type=0x%02x length=%u\n",
                          static_cast<unsigned>(type),
                          static_cast<unsigned>(length));
            return;
        }

        if (validCmd && !hasValidPacketCrc(value.data())) {
            Serial.printf("[BLE] CMD dropped: CRC mismatch seq=%u\n",
                          static_cast<unsigned>(value[1]));
            return;
        }

        memcpy(g_downlinkBuf, value.data(), length);
        g_downlinkLen = length;
        g_downlinkPending = true;

        if (type == TYPE_FILE_START) {
            g_fileTransferActive = true;
            Serial.println("[BLE] file transfer started");
        } else if (type == TYPE_FILE_END) {
            Serial.println("[BLE] file transfer ending");
        } else if (type == TYPE_CMD) {
            Serial.printf("[BLE] CMD seq=%u\n", static_cast<unsigned>(value[1]));
        }
    }
};

/** Calls imuService() and keeps the slowest time for the timing report. */
static void timedImuService() {
    const uint32_t started = micros();
    imuService();
    const uint32_t serviceUs = micros() - started;
    if (serviceUs > g_slowestImuServiceUs) {
        g_slowestImuServiceUs = serviceUs;
    }
}

/**
 * Calls imuRead() then gestureFlag(), and prints the slowest of each, and of imuService(), to
 * serial every IMU_TIMING_REPORT_MS, so the time they take out of loop() can be checked.
 */
static ImuStatus timedImuRead(int16_t *imu, int16_t *flag) {
    uint32_t started = micros();
    const ImuStatus status = imuRead(imu);
    const uint32_t readUs = micros() - started;
    started = micros();
    *flag = gestureFlag(imu, status);
    const uint32_t flagUs = micros() - started;
    if (readUs > g_slowestImuReadUs) {
        g_slowestImuReadUs = readUs;
    }
    if (flagUs > g_slowestGestureFlagUs) {
        g_slowestGestureFlagUs = flagUs;
    }

    const uint32_t now = millis();
    if (now - g_imuTimingReportedAt >= IMU_TIMING_REPORT_MS) {
        Serial.printf("[IMU] status=%d, slowest since last report: imuService %u us, imuRead %u us, gestureFlag %u us\n",
                      static_cast<int>(status), static_cast<unsigned>(g_slowestImuServiceUs),
                      static_cast<unsigned>(g_slowestImuReadUs),
                      static_cast<unsigned>(g_slowestGestureFlagUs));
        imuPrintStats();
        g_slowestImuServiceUs = 0;
        g_slowestImuReadUs = 0;
        g_slowestGestureFlagUs = 0;
        g_imuTimingReportedAt = now;
    }
    return status;
}

/**
 * Fills buf with one PACKET_LEN SENSOR packet: the IMU reading, its status, then the
 * gesture flag.
 **/
static void buildPacket(uint8_t *buf) {
    buf[0] = TYPE_SENSOR;
    buf[1] = g_seq;

    int16_t imu[IMU_FIELD_COUNT];
    int16_t flag = 0;
    const ImuStatus status = timedImuRead(imu, &flag);

    for (size_t i = 0; i < IMU_FIELD_COUNT; i++) {
        writeField(buf, i, imu[i]);
    }
    writeField(buf, IMU_STATUS_FIELD, status);
    writeField(buf, GESTURE_FLAG_FIELD, flag);
    for (size_t i = GESTURE_FLAG_FIELD + 1; i < DATA_FIELD_COUNT; i++) {
        writeField(buf, i, 0);  // reserved for later sensor expansion
    }
    writePacketCrc(buf);
}

/**
 * Initialises BLE with encryption, creates the service and its two
 * characteristics, and starts advertising.
 */
void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.printf("\n[BLE] boot: B16 Player-%d peripheral\n", PLAYER_ID);

    configureMotor();
    Serial.printf("[MOTOR] ready on GPIO %u\n", static_cast<unsigned>(MOTOR_PIN));

    // if the IMU is missing, BLE still starts and the board sends zeros with IMU_MISSING
    // until the IMU answers
    if (imuBegin(PLAYER_ID)) {
        Serial.println("[IMU] ready");
    } else {
        Serial.println("[IMU] not found");
    }

    NimBLEDevice::init(DEVICE_NAME);
    NimBLEDevice::setMTU(PREFERRED_MTU); // negotiate MTU

    // set security mode 1 level 2: bonding + LE secure connections
    NimBLEDevice::setSecurityAuth(true, false, true);
    NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);

    g_server = NimBLEDevice::createServer();
    g_server->setCallbacks(new ServerCallbacks());

    NimBLEService *service = g_server->createService(SERVICE_UUID);

    // allow central to read the sensor value and subscribe to notifications (READ, NOTIFY)
    // reading the value requires an encrypted connection (READ_ENC); subscribing does not
    g_sensorChar = service->createCharacteristic(
        SENSOR_CHAR_UUID,
        NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::NOTIFY | NIMBLE_PROPERTY::READ_ENC);

    // allow writes with WRITE, but only when link is encrypted (WRITE_ENC)
    g_cmdChar = service->createCharacteristic(
        CMD_CHAR_UUID,
        NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_ENC);
    g_cmdChar->setCallbacks(new DownlinkCallbacks());

    service->start();

    NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
    adv->addServiceUUID(SERVICE_UUID);
    adv->start();

    Serial.printf("[BLE] advertising as %s\n", DEVICE_NAME);
}

/**
 * Return any pending downlink packet, then notify one SENSOR packet every
 * PACKET_PERIOD_MS while connected and no file transfer is active.
**/
void loop() {
    // every pass, connected or not, so the gesture detector gets every 500 Hz reading
    timedImuService();
    updateMotor();

    const uint32_t now = millis();

    // a notification carries at most MTU-3 bytes, so while the link is still on the
    // 23-byte default our PACKET_LEN packet goes out truncated and the client throws
    // it away. poll until the exchange lifts us clear, then stop looking — MTU only
    // grows within a connection. polling rather than keying off onMTUChange: if that
    // callback never arrives we want to wait, not to go silent forever.
    if (g_connected && !g_mtuReady) {
        g_mtuReady = g_server->getPeerMTU(g_connHandle) >= PACKET_LEN + 3;
    }

    // only stream sensor data once link is encrypted
    // READ_ENC protects direct reads of the sensor value, but not the
    // notification subscription (CCCD)
    if (g_connected && !g_encrypted) {
        g_encrypted = g_server->getPeerInfoByHandle(g_connHandle).isEncrypted();
        if (g_encrypted) {
            g_encryptedAt = now;
            Serial.println("[BLE] link encrypted -> streaming enabled");
        }
    }

    // after encryption and initial Windows negotiation settle,
    // request the preferred connection parameters once per BLE session.
    if (g_encrypted &&
        !g_connParamsRequested &&
        now - g_encryptedAt >= CONN_PARAM_REQUEST_DELAY_MS) {

        g_connParamsRequested = true;

        Serial.printf(
            "[BLE] requesting interval=%u (%.2f ms), latency=%u, timeout=%u ms\n",
            FAST_CONN_INTERVAL,
            FAST_CONN_INTERVAL * 1.25f,
            CONN_LATENCY,
            CONN_TIMEOUT * 10
        );

        g_server->updateConnParams(
            g_connHandle,
            FAST_CONN_INTERVAL,  // minimum: 15 ms
            FAST_CONN_INTERVAL,  // maximum: 15 ms
            CONN_LATENCY,        // do not skip connection events
            CONN_TIMEOUT         // 2-second supervision timeout
        );
    }

    // process the received command before next sensor sample, keeping round-trips
    // within one packet period instead of queueing behind the stream.
    if (g_mtuReady && g_encrypted && g_downlinkPending) {
        uint8_t buf[FILE_MAX_PACKET_LEN];
        const size_t length = g_downlinkLen;
        memcpy(buf, g_downlinkBuf, length);  // copy received packet before marking as handled
        g_downlinkPending = false;

        if (buf[0] == TYPE_CMD && readField(buf, CMD_KIND_FIELD) == CMD_VIBRATE) {
            const int16_t pattern = readField(buf, 0);
            if (pattern == VIBRATE_RECOGNISED) {
                startMotorPulse();
                Serial.printf("[MOTOR] VIBRATE seq=%u: %u ms pulse\n",
                              static_cast<unsigned>(buf[1]),
                              static_cast<unsigned>(MOTOR_PULSE_MS));
            } else {
                Serial.printf("[MOTOR] VIBRATE seq=%u: unknown pattern %d, not pulsed\n",
                              static_cast<unsigned>(buf[1]), pattern);
            }
        }

        if (buf[0] == TYPE_CMD) {
            // "Modify the data and send it back": convert into ECHO response by
            // negating the six IMU fields and then recalculating the CRC
            buf[0] = TYPE_ECHO;
            for (size_t i = 0; i < IMU_FIELD_COUNT; i++) {
                const int32_t negated = -static_cast<int32_t>(readField(buf, i));
                writeField(buf, i, static_cast<int16_t>(negated));
            }
            writePacketCrc(buf);
        }

        // file packets return unchanged. stop-and-wait lets this one buffer stream
        // any practical file size without storing the file on the board.
        const bool sent = g_sensorChar->notify(buf, length);
        if (buf[0] == TYPE_ECHO) {
            Serial.printf("[BLE] ECHO seq=%u%s\n",
                          static_cast<unsigned>(buf[1]),
                          sent ? "" : " NOT SENT (notify failed)");
        } else if (buf[0] == TYPE_FILE_END && sent) {
            g_fileTransferActive = false;
            Serial.println("[BLE] file transfer complete");
        }
    }

    // seq advances once per packet built. notify()'s result is not checked, so a packet the
    // stack fails to send still uses up its seq and shows on the laptop as a gap
    if (g_mtuReady && g_encrypted && !g_fileTransferActive &&
        (now - g_lastTx >= PACKET_PERIOD_MS)) {
        // stay on the 10 ms grid however long a pass of loop() takes, so the rate stays at
        // 100 Hz, but start the grid again after a hold-up instead of sending a burst
        g_lastTx = (now - g_lastTx >= 2 * PACKET_PERIOD_MS) ? now : g_lastTx + PACKET_PERIOD_MS;
        uint8_t buf[PACKET_LEN];
        buildPacket(buf);
        // setValue so a GATT read of the characteristic returns the latest sample;
        // pass same buffer to notify() so packet sent is exactly the one just built
        g_sensorChar->setValue(buf, PACKET_LEN);
        g_sensorChar->notify(buf, PACKET_LEN);
        g_seq++;
    }

    delay(1);
}
