/*
  ESP32 Bluetooth + MPU6050 + motor integration test

  Laptop controls (Bluetooth serial terminal or USB Serial Monitor):
    Space  Read one MPU6050 sample and pulse the motor
    I      Read one MPU6050 sample only
    M      Pulse the motor only
    P      Bluetooth/serial ping
    H      Show this help

  Bluetooth device name: CG4002_ESP32_TEST
  Terminal settings: 115200 baud for USB; line ending does not matter.

  Wiring:
    MPU6050 SDA -> GPIO21, SCL -> GPIO22, VCC -> 3.3 V, GND -> GND
    Motor driver input -> GPIO2

  Do not power a motor directly from GPIO2. Use a transistor/MOSFET driver,
  a flyback diode for a brushed motor, a suitable motor supply, and common GND.

  Required Arduino libraries:
    - ESP32 board package (BluetoothSerial is included)
    - I2Cdevlib MPU6050 library
*/

#include <Arduino.h>
#include <BluetoothSerial.h>
#include <Wire.h>
#include "I2Cdev.h"
#include "MPU6050.h"

#if !defined(CONFIG_BT_ENABLED) || !defined(CONFIG_BLUEDROID_ENABLED)
#error Bluetooth is not enabled for the selected ESP32 board.
#endif
#if !defined(CONFIG_BT_SPP_ENABLED)
#error Bluetooth Classic SPP is not available on the selected ESP32 board.
#endif

constexpr char BLUETOOTH_NAME[] = "CG4002_ESP32_TEST";

constexpr uint8_t SDA_PIN = 21;
constexpr uint8_t SCL_PIN = 22;
constexpr uint8_t MOTOR_PIN = 2;

constexpr uint8_t PWM_CHANNEL = 0;
constexpr uint32_t PWM_FREQUENCY_HZ = 20000;
constexpr uint8_t PWM_RESOLUTION_BITS = 8;
constexpr uint8_t MOTOR_TEST_DUTY = 128;  // 50% of the 0-255 range.
constexpr uint32_t MOTOR_TEST_DURATION_MS = 200;

BluetoothSerial SerialBT;
MPU6050 mpu;

bool mpuConnected = false;
bool bluetoothStarted = false;
bool motorRunning = false;
bool combinedTestRunning = false;
bool bluetoothWasConnected = false;
uint32_t motorStopTimeMs = 0;

void sendLine(const char *message) {
  Serial.println(message);
  if (bluetoothStarted) {
    SerialBT.println(message);
  }
}

void writeMotorDuty(uint8_t duty) {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(MOTOR_PIN, duty);
#else
  ledcWrite(PWM_CHANNEL, duty);
#endif
}

void configureMotorPwm() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  if (!ledcAttach(MOTOR_PIN, PWM_FREQUENCY_HZ, PWM_RESOLUTION_BITS)) {
    sendLine("ERROR,MOTOR_PWM_ATTACH_FAILED");
  }
#else
  ledcSetup(PWM_CHANNEL, PWM_FREQUENCY_HZ, PWM_RESOLUTION_BITS);
  ledcAttachPin(MOTOR_PIN, PWM_CHANNEL);
#endif
  writeMotorDuty(0);
}

void showHelp() {
  sendLine("COMMANDS: Space=IMU+motor, I=IMU, M=motor, P=ping, H=help");
}

void sendImuReading() {
  if (!mpuConnected) {
    sendLine("ERROR,MPU6050_NOT_FOUND");
    return;
  }

  int16_t ax, ay, az, gx, gy, gz;
  mpu.getMotion6(&ax, &ay, &az, &gx, &gy, &gz);

  char message[128];
  snprintf(message, sizeof(message),
           "IMU,%lu,%d,%d,%d,%d,%d,%d",
           static_cast<unsigned long>(millis()),
           ax, ay, az, gx, gy, gz);
  sendLine(message);
}

void startMotorPulse(bool partOfCombinedTest) {
  writeMotorDuty(MOTOR_TEST_DUTY);
  motorRunning = true;
  combinedTestRunning = partOfCombinedTest;
  motorStopTimeMs = millis() + MOTOR_TEST_DURATION_MS;
  sendLine("MOTOR,ON,duty=128,duration_ms=200");
}

void updateMotor() {
  if (!motorRunning ||
      static_cast<int32_t>(millis() - motorStopTimeMs) < 0) {
    return;
  }

  writeMotorDuty(0);
  motorRunning = false;
  sendLine("MOTOR,OFF");

  if (combinedTestRunning) {
    combinedTestRunning = false;
    sendLine("TEST,COMPLETE");
  }
}

void runCombinedTest() {
  sendLine("TEST,BEGIN");
  sendImuReading();
  startMotorPulse(true);
}

void handleCommand(char command) {
  switch (command) {
    case ' ':
      runCombinedTest();
      break;
    case 'i':
    case 'I':
      sendImuReading();
      break;
    case 'm':
    case 'M':
      startMotorPulse(false);
      break;
    case 'p':
    case 'P':
      sendLine("PONG");
      break;
    case 'h':
    case 'H':
    case '?':
      showHelp();
      break;
    case '\r':
    case '\n':
    case '\t':
      break;
    default:
      sendLine("ERROR,UNKNOWN_COMMAND (send H for help)");
      break;
  }
}

void updateBluetoothConnection() {
  const bool connected = SerialBT.hasClient();
  if (connected && !bluetoothWasConnected) {
    Serial.println("BLUETOOTH,CLIENT_CONNECTED");
    SerialBT.println("CONNECTED,CG4002_ESP32_TEST");
    showHelp();
  } else if (!connected && bluetoothWasConnected) {
    Serial.println("BLUETOOTH,CLIENT_DISCONNECTED");
  }
  bluetoothWasConnected = connected;
}

void setup() {
  Serial.begin(115200);
  delay(300);

  configureMotorPwm();

  Wire.begin(SDA_PIN, SCL_PIN, 400000);
  Wire.setTimeOut(20);
  mpu.initialize();
  mpuConnected = mpu.testConnection();

  if (mpuConnected) {
    // Match the data-collection sketch: +/-4 g and +/-500 degrees/s.
    mpu.setFullScaleAccelRange(MPU6050_ACCEL_FS_4);
    mpu.setFullScaleGyroRange(MPU6050_GYRO_FS_500);
  }

  bluetoothStarted = SerialBT.begin(BLUETOOTH_NAME);
  if (!bluetoothStarted) {
    Serial.println("ERROR,BLUETOOTH_START_FAILED");
  }

  sendLine("READY,CG4002_ESP32_TEST");
  sendLine(mpuConnected ? "MPU6050,OK" : "MPU6050,NOT_FOUND");
  sendLine("MOTOR,READY,GPIO2");
  showHelp();
}

void loop() {
  updateBluetoothConnection();
  updateMotor();

  while (SerialBT.available()) {
    handleCommand(static_cast<char>(SerialBT.read()));
  }

  // USB accepts the same commands, which helps isolate Bluetooth problems.
  while (Serial.available()) {
    handleCommand(static_cast<char>(Serial.read()));
  }

  delay(2);
}
