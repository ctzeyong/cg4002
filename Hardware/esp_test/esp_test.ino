// FireBeetle ESP32 DFR0478 onboard LED blink test

const int LED_PIN = 2;

void setup() {
  pinMode(LED_PIN, OUTPUT);
}

void loop() {
  digitalWrite(LED_PIN, HIGH);
  delay(5000);

  digitalWrite(LED_PIN, LOW);
  delay(2000);
}