// Optional fallback for SCALE_DRIVER=serial: an Arduino reads the HX711 with exact
// timing and prints one RAW count per line over USB. Tare/calibration stay on the Pi.
// Library: "HX711" by Bogdan Necula (Arduino Library Manager). Not tested in this repo.
#include "HX711.h"
const int DT_PIN = 3, SCK_PIN = 2;
HX711 scale;

void setup() {
  Serial.begin(57600);          // match SCALE_SERIAL_BAUD
  scale.begin(DT_PIN, SCK_PIN); // channel A, gain 128
}

void loop() {
  if (scale.is_ready()) Serial.println(scale.read());
  delay(100);
}
