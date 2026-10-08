/*
  Two MPU6050s connected to one FireBeetle ESP32 DFR0478

  Wrist MPU6050:
    AD0 connected to GND
    I2C address = 0x68

  Finger MPU6050:
    AD0 connected to 3.3 V
    I2C address = 0x69

  Both MPU6050s share the same SDA and SCL wires.

  Configuration:
    Accelerometer range: ±4 g
    Gyroscope range:     ±500 degrees/second
    Digital low-pass filter: 42 Hz
    Internal sample rate: approximately 50 Hz
*/

#include <Wire.h>
#include "I2Cdev.h"
#include "MPU6050.h"

// FireBeetle ESP32 DFR0478 I2C pins
constexpr uint8_t SDA_PIN = 21;
constexpr uint8_t SCL_PIN = 22;

// Sensor addresses
constexpr uint8_t WRIST_MPU_ADDRESS  = 0x68;
constexpr uint8_t FINGER_MPU_ADDRESS = 0x69;

// Both sensors use the same I2C bus
MPU6050 wristMPU(WRIST_MPU_ADDRESS);
MPU6050 fingerMPU(FINGER_MPU_ADDRESS);

// Sensor configuration
constexpr uint8_t ACCEL_RANGE = MPU6050_ACCEL_FS_2;
constexpr uint8_t GYRO_RANGE  = MPU6050_GYRO_FS_500;
constexpr uint8_t DLPF_MODE   = MPU6050_DLPF_BW_42;

// With DLPF enabled, the MPU6050 gyroscope output rate is 1 kHz.
// Sample rate = 1000 / (1 + SAMPLE_RATE_DIVIDER)
//             = 1000 / (1 + 19)
//             = 50 Hz
constexpr uint8_t SAMPLE_RATE_DIVIDER = 19;

// Sensitivities for the selected measurement ranges
// constexpr float ACCEL_SENSITIVITY = 8192.0f;  // LSB/g at ±4 g
// constexpr float GYRO_SENSITIVITY  = 65.5f;    // LSB/(degree/s) at ±500°/s

// Desired interval between paired readings
constexpr unsigned long SAMPLE_INTERVAL_MS = 20;

// Wrist sensor raw readings
int16_t wristAxRaw, wristAyRaw, wristAzRaw;
int16_t wristGxRaw, wristGyRaw, wristGzRaw;

// Finger sensor raw readings
int16_t fingerAxRaw, fingerAyRaw, fingerAzRaw;
int16_t fingerGxRaw, fingerGyRaw, fingerGzRaw;

bool blinkState = false;

/*
  Configure one MPU6050.

  Both sensors currently use the same measurement ranges,
  filter bandwidth and sample rate.
*/
void configureMPU(MPU6050 &mpu) {
  mpu.setFullScaleAccelRange(ACCEL_RANGE);
  mpu.setFullScaleGyroRange(GYRO_RANGE);
  mpu.setDLPFMode(DLPF_MODE);
  mpu.setRate(SAMPLE_RATE_DIVIDER);
}

/*
  Print one sensor reading as CSV.

  Acceleration is printed in g.
  Angular velocity is printed in degrees per second.
*/
void printSensorData(
  unsigned long sampleTime,
  const char *sensorName,
  int16_t axRaw,
  int16_t ayRaw,
  int16_t azRaw,
  int16_t gxRaw,
  int16_t gyRaw,
  int16_t gzRaw
) {
  // float ax = axRaw / ACCEL_SENSITIVITY;
  // float ay = ayRaw / ACCEL_SENSITIVITY;
  // float az = azRaw / ACCEL_SENSITIVITY;

  // float gx = gxRaw / GYRO_SENSITIVITY;
  // float gy = gyRaw / GYRO_SENSITIVITY;
  // float gz = gzRaw / GYRO_SENSITIVITY;

  Serial.print(sampleTime);
  Serial.print(",");
  Serial.print(sensorName);
  Serial.print(",");

  Serial.print(axRaw);
  Serial.print(",");
  Serial.print(ayRaw);
  Serial.print(",");
  Serial.print(azRaw);
  Serial.print(",");

  Serial.print(gxRaw);
  Serial.print(",");
  Serial.print(gyRaw);
  Serial.print(",");
  Serial.println(gzRaw);
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println();
  Serial.println("Starting I2C bus...");

  // Start the ESP32 I2C controller
  Wire.begin(SDA_PIN, SCL_PIN);

  // Use 400 kHz I2C for faster reads
  Wire.setClock(400000);

  /*
    Initialise wrist MPU6050 at address 0x68.
  */
  Serial.println("Initializing wrist MPU6050 at 0x68...");
  wristMPU.initialize();

  Serial.println("Testing wrist MPU6050 connection...");

  if (!wristMPU.testConnection()) {
    Serial.println("ERROR: Wrist MPU6050 connection failed.");
    Serial.println("Check that its AD0 pin is connected to GND.");

    while (true) {
      delay(1000);
    }
  }

  Serial.println("Wrist MPU6050 connection successful.");

  /*
    Initialise finger MPU6050 at address 0x69.
  */
  Serial.println("Initializing finger MPU6050 at 0x69...");
  fingerMPU.initialize();

  Serial.println("Testing finger MPU6050 connection...");

  if (!fingerMPU.testConnection()) {
    Serial.println("ERROR: Finger MPU6050 connection failed.");
    Serial.println("Check that its AD0 pin is connected to 3.3 V.");

    while (true) {
      delay(1000);
    }
  }

  Serial.println("Finger MPU6050 connection successful.");

  /*
    Configure the measurement ranges, filter and sample rate.
  */
  configureMPU(wristMPU);
  configureMPU(fingerMPU);

  /*
    Set calibration offsets separately.

    Replace these zero values with offsets obtained by
    calibrating each physical sensor individually.
  */

  // Wrist MPU6050 offsets
  wristMPU.setXAccelOffset(0);
  wristMPU.setYAccelOffset(0);
  wristMPU.setZAccelOffset(0);

  wristMPU.setXGyroOffset(0);
  wristMPU.setYGyroOffset(0);
  wristMPU.setZGyroOffset(0);

  // Finger MPU6050 offsets
  fingerMPU.setXAccelOffset(0);
  fingerMPU.setYAccelOffset(0);
  fingerMPU.setZAccelOffset(0);

  fingerMPU.setXGyroOffset(0);
  fingerMPU.setYGyroOffset(0);
  fingerMPU.setZGyroOffset(0);

  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  Serial.println();
  Serial.println("Both MPU6050s initialized and configured.");
  Serial.println("Accelerometer range: +/-4 g");
  Serial.println("Gyroscope range: +/-500 degrees/s");
  Serial.println("Digital low-pass filter: 42 Hz");
  Serial.println("Internal sample rate: 50 Hz");
  Serial.println();

  // CSV column headings
  Serial.println("time_ms,sensor,ax_g,ay_g,az_g,gx_dps,gy_dps,gz_dps");
}

void loop() {
  unsigned long loopStartTime = millis();

  /*
    Read the wrist sensor first, followed by the finger sensor.
    Both calls use the same physical I2C bus.
  */
  wristMPU.getMotion6(
    &wristAxRaw,
    &wristAyRaw,
    &wristAzRaw,
    &wristGxRaw,
    &wristGyRaw,
    &wristGzRaw
  );

  fingerMPU.getMotion6(
    &fingerAxRaw,
    &fingerAyRaw,
    &fingerAzRaw,
    &fingerGxRaw,
    &fingerGyRaw,
    &fingerGzRaw
  );

  unsigned long sampleTime = millis();

  // Print wrist data
  printSensorData(
    sampleTime,
    "wrist",
    wristAxRaw,
    wristAyRaw,
    wristAzRaw,
    wristGxRaw,
    wristGyRaw,
    wristGzRaw
  );

  // Print finger data
  printSensorData(
    sampleTime,
    "finger",
    fingerAxRaw,
    fingerAyRaw,
    fingerAzRaw,
    fingerGxRaw,
    fingerGyRaw,
    fingerGzRaw
  );

  blinkState = !blinkState;
  digitalWrite(LED_BUILTIN, blinkState);

  /*
    Account for the time spent reading and printing instead of
    adding an unconditional 20 ms delay.
  */
  unsigned long loopDuration = millis() - loopStartTime;

  if (loopDuration < SAMPLE_INTERVAL_MS) {
    delay(SAMPLE_INTERVAL_MS - loopDuration);
  }
}