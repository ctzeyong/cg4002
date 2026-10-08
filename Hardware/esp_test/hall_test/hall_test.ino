const int inputPin_D = 25;
const int inputPin_A = 34;

void setup() {
  Serial.begin(115200);
  pinMode(inputPin_D, INPUT_PULLUP);
  analogReadResolution(12);  // 0–4095
}

void loop() {
  int pinState = digitalRead(inputPin_D);
  Serial.print("Digital: ");
  Serial.print(pinState);
  Serial.print(" Analog: ");
  int adcValue = analogRead(inputPin_A);
  Serial.println(adcValue);
  delay(100);
}