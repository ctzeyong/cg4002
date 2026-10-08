const int MOTOR_PIN = 2;     // FireBeetle D8 = GPIO5
const int PWM_CHANNEL = 0;
const int PWM_FREQUENCY = 20000;
const int PWM_RESOLUTION = 8; // Duty-cycle range: 0–255
const int SHORT_PWM_DUTY_CYCLE = 150;
const int LONG_PWM_DUTY_CYCLE = SHORT_PWM_DUTY_CYCLE - 15;

const unsigned long SHORT_VIBRATION_MS = 150;
const unsigned long LONG_VIBRATION_MS = 500;
const unsigned long PAUSE_BETWEEN_VIBRATIONS_MS = 500;

bool vibration_active = false;
unsigned long vibration_end_time = 0;
unsigned long next_vibration_time = 0;
bool play_short_next = true;

void setup() {
  Serial.begin(115200);

  ledcSetup(PWM_CHANNEL, PWM_FREQUENCY, PWM_RESOLUTION);
  ledcAttachPin(MOTOR_PIN, PWM_CHANNEL);

  ledcWrite(PWM_CHANNEL, 0);
}

void start_vibration(unsigned long duration_ms, int duty_cycle,
                     const char *label) {
  Serial.println(label);
  ledcWrite(PWM_CHANNEL, duty_cycle);

  vibration_active = true;
  vibration_end_time = millis() + duration_ms;
}

void short_vibration() {
  start_vibration(SHORT_VIBRATION_MS, SHORT_PWM_DUTY_CYCLE,
                  "Short vibration");
}

void long_vibration() {
  start_vibration(LONG_VIBRATION_MS, LONG_PWM_DUTY_CYCLE,
                  "Long vibration");
}

void update_vibration() {
  if (vibration_active &&
      static_cast<long>(millis() - vibration_end_time) >= 0) {
    ledcWrite(PWM_CHANNEL, 0);
    vibration_active = false;
    next_vibration_time = millis() + PAUSE_BETWEEN_VIBRATIONS_MS;
  }
}

void loop() {
  update_vibration();

  if (!vibration_active &&
      static_cast<long>(millis() - next_vibration_time) >= 0) {
    if (play_short_next) {
      short_vibration();
    } else {
      long_vibration();
    }

    play_short_next = !play_short_next;
  }
}
