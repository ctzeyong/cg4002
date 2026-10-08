/*
  Two MPU6050s connected to one FireBeetle ESP32 DFR0478

  Wrist MPU6050:
    AD0 connected to GND
    I2C address = 0x68

  Finger MPU6050:
    AD0 connected to 3.3 V
    I2C address = 0x69

  Both MPU6050s share the same SDA and SCL wires.
*/

#include <Wire.h>
#include "I2Cdev.h"
#include "MPU6050.h"

// FireBeetle ESP32 DFR0478 I2C pins
constexpr uint8_t SDA_PIN = 21;
constexpr uint8_t SCL_PIN = 22;

// Sensor addresses
constexpr uint8_t WRIST_MPU_ADDRESS  = 0x68;  // AD0 low
constexpr uint8_t FINGER_MPU_ADDRESS = 0x69;  // AD0 high

// Both sensors use the same Wire bus but have different addresses
MPU6050 wristMPU(WRIST_MPU_ADDRESS);
MPU6050 fingerMPU(FINGER_MPU_ADDRESS);

// Wrist sensor raw readings
int16_t wristAx, wristAy, wristAz;
int16_t wristGx, wristGy, wristGz;

// Finger sensor raw readings
int16_t fingerAx, fingerAy, fingerAz;
int16_t fingerGx, fingerGy, fingerGz;

bool blinkState = false;

void setup() {
  Serial.begin(115200);
  delay(1000);

  Serial.println();
  Serial.println("Starting I2C bus...");

  // Start the ESP32 I2C controller
  Wire.begin(SDA_PIN, SCL_PIN);

  // Optional: use 400 kHz I2C for faster reads
  Wire.setClock(400000);

  /*
    Initialise wrist MPU6050 at address 0x68
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
    Initialise finger MPU6050 at address 0x69
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
    Set calibration offsets separately.

    Replace these zero values with the offsets obtained by
    calibrating each sensor individually.
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

  Serial.println();
  Serial.println("Both MPU6050s initialized.");
  Serial.println(
    "time_ms,sensor,ax,ay,az,gx,gy,gz"
  );
}

void loop() {
  /*
    Read the wrist sensor first, followed by the finger sensor.
    Both calls use the same physical I2C bus.
  */

  wristMPU.getMotion6(
    &wristAx,
    &wristAy,
    &wristAz,
    &wristGx,
    &wristGy,
    &wristGz
  );

  fingerMPU.getMotion6(
    &fingerAx,
    &fingerAy,
    &fingerAz,
    &fingerGx,
    &fingerGy,
    &fingerGz
  );

  unsigned long sampleTime = millis();

  // Print wrist MPU6050 data
  Serial.print(sampleTime);
  Serial.print(",wrist,");
  Serial.print(wristAx);
  Serial.print(",");
  Serial.print(wristAy);
  Serial.print(",");
  Serial.print(wristAz);
  Serial.print(",");
  Serial.print(wristGx);
  Serial.print(",");
  Serial.print(wristGy);
  Serial.print(",");
  Serial.println(wristGz);

  // Print finger MPU6050 data
  Serial.print(sampleTime);
  Serial.print(",finger,");
  Serial.print(fingerAx);
  Serial.print(",");
  Serial.print(fingerAy);
  Serial.print(",");
  Serial.print(fingerAz);
  Serial.print(",");
  Serial.print(fingerGx);
  Serial.print(",");
  Serial.print(fingerGy);
  Serial.print(",");
  Serial.println(fingerGz);

  blinkState = !blinkState;
  digitalWrite(LED_BUILTIN, blinkState);

  // Approximately 50 paired samples per second
  delay(20);
}