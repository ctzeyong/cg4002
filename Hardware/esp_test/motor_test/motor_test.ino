const int MOTOR_PIN = 2;     // FireBeetle D2 = GPIO25
const int PWM_CHANNEL = 0;
const int PWM_FREQUENCY = 20000;
const int PWM_RESOLUTION = 8; // Duty-cycle range: 0–255

void setup() {
  Serial.begin(115200);

  ledcSetup(PWM_CHANNEL, PWM_FREQUENCY, PWM_RESOLUTION);
  ledcAttachPin(MOTOR_PIN, PWM_CHANNEL);

  ledcWrite(PWM_CHANNEL, 0);
}

void loop() {
  // Gradually increase motor strength
  Serial.println("On");
  for (int duty = 100; duty <= 200; duty += 5) {
    ledcWrite(PWM_CHANNEL, duty);

    Serial.print("PWM duty: ");
    Serial.println(duty);

    delay(500);
  }

  delay(1000);

  // Turn motor off
  ledcWrite(PWM_CHANNEL, 0);
  Serial.println("Off");
  delay(2000);
}