#include <Wire.h>

constexpr uint8_t ADDRESS = 0x68;
constexpr uint8_t WHO_AM_I = 0x75;

void setup() {
  Serial.begin(115200);
  delay(2000);

  Wire.begin(21, 22);
  Wire.setClock(100000);

  Wire.beginTransmission(ADDRESS);
  Wire.write(WHO_AM_I);

  uint8_t error = Wire.endTransmission(false);

  Serial.print("Transmission error: ");
  Serial.println(error);

  uint8_t received = Wire.requestFrom(ADDRESS, (uint8_t)1);

  Serial.print("Bytes received: ");
  Serial.println(received);

  if (Wire.available()) {
    uint8_t id = Wire.read();

    Serial.print("WHO_AM_I: 0x");
    if (id < 0x10) Serial.print("0");
    Serial.println(id, HEX);
  }
}

void loop() {}