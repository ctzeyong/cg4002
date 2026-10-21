/*
  Full pipeline test: MPU6050 -> ESP32 -> BLE relay -> AI -> BLE CMD -> motor.
  Open this file in its matching esp32_pipeline_test folder in Arduino IDE.
  Requires ESP32 Arduino core (2.x or 3.x) and NimBLE-Arduino 2.x.
  Wiring: SDA=21, SCL=22, MPU address=0x68; motor DRIVER input=GPIO2.
  Use the existing motor driver, not a motor wired directly to a GPIO.

  PLAYER_ID defaults to 2, matching Comms/firebeetle/firebeetle.ino.
  BLE UUIDs, security, MTU, packet types and CRC match that sketch.
  This test implements SENSOR and result CMD/ECHO only, not file transfer.
  Disable the relay's keyboard dummy CMD generator for this test.

  SENSOR: exactly 26 bytes, all multibyte values little-endian:
    0       type=0x01
    1       sequence modulo 256 (advances even when a sample cannot be delivered)
    2..13   ax,ay,az,gx,gy,gz: six signed int16 raw counts (+/-4g, +/-500deg/s)
    14..15  end_of_gesture: uint16, 1 on ONE completion sample, otherwise 0
    16..19  timestamp_ms: uint32 ESP sample time since boot (wraps ~49.7 days)
    20..23  gesture_id: uint32, increments at start; 0 before first gesture
    24..25  CRC-16/CCITT-FALSE over bytes 0..23.
  The existing Python decoder calls bytes 14..23 five "reserved" int16 fields.
  Reinterpret those words as unsigned when reconstructing timestamp and ID.
  Preserve ESP timestamp separately from the relay's current arrival timestamp.

  Acquire at 500 Hz for the unchanged detector. Send every fifth RAW sample
  at 100 Hz, including background and the 150/200 ms end-confirmation tail.
  No averaging, amplitude normalization, or completed-gesture resampling is
  applied to transmitted counts. The AI's 150-sample window then covers 1.5 s.
  The embedded 8-tree model is PROVISIONAL and did not meet validation targets.
  Start: score >= 0.5. End: motion-energy dip held 150 ms OR score < 0.2 held
  200 ms, using the existing Segmenter. Timeout/invalid segments are ABORTED,
  logged, and receive two short pulses, but do not trigger AI with EOG=1.

  Result CMD: 26-byte packet, type=0x03, command sequence in byte 1:
    field[0] = 0x4701 GOOD_SYMBOL, or 0x4702 INVALID_SYMBOL
    field[1], field[2] = low/high 16-bit words of the gesture_id being answered
    field[3..10] = 0; standard CRC.
  Accept results only for a completed gesture in this BLE streaming session.
  Last 16 completed gestures are tracked. First accepted result wins;
  duplicates do not vibrate again. Commands from an old connection are ignored.
  ECHO (0x04) acknowledges the command, preserving opcode/gesture_id, replacing
  field[3] with status: 0=queued, 1=duplicate, 2=unknown/stale gesture,
  3=motor queue full (retry), 4=malformed command. CRC is recalculated.
  This ECHO is an acknowledgement, NOT an AI inference trigger.

  Motor: start=1 short, end=2 short, good=1 long, invalid=3 long.
  Short=150 ms duty 150; long=500 ms duty 135; inter-pulse/pattern gap=500 ms.
  Patterns queue without delay() or pausing the IMU. Motor vibration may affect
  the measured signal; validate this on the assembled hardware.

  Integration still required outside this file:
    - decode EOG/timestamp/gesture_id in Comms;
    - feed real AI on SENSOR, not the baseline dummy ECHO trigger;
    - return explicit GOOD/INVALID results (AI currently defaults to no rejection);
    - route results to the correct player's BLE CMD queue. Configuration B
      currently discards TCP downlink messages.
  Receiver must reset its AI window on reconnect or sensor sequence gaps.
  Notifications are not application acknowledgements: log/test packet loss.
  TX queue retries failed notify() calls for at most 500 ms. Overflow/expiry
  discards the backlog, invalidates pending results, and logs lost EOG counts.
  No stale samples, queued motor patterns or results are replayed on reconnect.

  Model/frontend/segmenter snapshot:
    Hardware/Data Collection/esp32_continuous_pipeline_test/
  BLE definitions:
    Comms/firebeetle/firebeetle.ino
*/
#include <Arduino.h>
#include <Wire.h>
#include <NimBLEDevice.h>
#include <esp_timer.h>
#include <esp_arduino_version.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstring>

#ifndef PLAYER_ID
#define PLAYER_ID 1
#endif
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

// BEGIN MODEL SNAPSHOT
// PROVISIONAL pipeline-test model; day 3 validation targets were not met.
// Exported gesture-packet-v4 model. No Bluetooth code.
constexpr int MODEL_RAW_HZ = 500;
constexpr bool MODEL_READY = true;
constexpr char MODEL_ID[] = "342c4355152c26f4";
struct TreeNode { int feature, left, right; float threshold, score; };
static const TreeNode TREE_0[] = {
{38,1,16,1.453119144e-02f,4.976822734e-01f},
{41,2,9,-2.164854854e-01f,1.932276040e-01f},
{34,3,6,-2.749373950e-02f,5.331118703e-01f},
{10,4,5,6.435419898e-03f,6.742449999e-01f},
{-2,-1,-1,-2.000000000e+00f,2.816666663e-01f},
{-2,-1,-1,-2.000000000e+00f,7.746058702e-01f},
{6,7,8,-1.028064042e-01f,2.678571343e-01f},
{-2,-1,-1,-2.000000000e+00f,7.309486717e-02f},
{-2,-1,-1,-2.000000000e+00f,4.032432437e-01f},
{3,10,13,7.084076758e-03f,1.593995243e-01f},
{35,11,12,2.152987123e-01f,7.653061301e-02f},
{-2,-1,-1,-2.000000000e+00f,5.676152557e-02f},
{-2,-1,-1,-2.000000000e+00f,5.264270902e-01f},
{35,14,15,7.504121959e-02f,3.991589546e-01f},
{-2,-1,-1,-2.000000000e+00f,3.324827850e-01f},
{-2,-1,-1,-2.000000000e+00f,5.361824632e-01f},
{38,17,24,3.199178353e-02f,7.595757246e-01f},
{25,18,21,-2.656738758e-01f,6.340975761e-01f},
{1,19,20,5.956596509e-02f,7.209089994e-01f},
{-2,-1,-1,-2.000000000e+00f,5.496678948e-01f},
{-2,-1,-1,-2.000000000e+00f,8.459106088e-01f},
{14,22,23,-4.639632106e-01f,5.364800692e-01f},
{-2,-1,-1,-2.000000000e+00f,7.894586921e-01f},
{-2,-1,-1,-2.000000000e+00f,4.563903809e-01f},
{8,25,28,9.041819721e-02f,9.038333297e-01f},
{0,26,27,-3.655779958e-01f,5.555555820e-01f},
{-2,-1,-1,-2.000000000e+00f,1.551094949e-01f},
{-2,-1,-1,-2.000000000e+00f,6.699687243e-01f},
{10,29,30,2.340656333e-02f,9.388771057e-01f},
{-2,-1,-1,-2.000000000e+00f,8.147540689e-01f},
{-2,-1,-1,-2.000000000e+00f,9.565197229e-01f},
};
static const TreeNode TREE_1[] = {
{8,1,16,6.490522623e-02f,5.023455620e-01f},
{36,2,9,6.923623383e-02f,2.157112807e-01f},
{17,3,6,4.996462259e-03f,1.088087037e-01f},
{3,4,5,4.711071961e-03f,4.559536278e-02f},
{-2,-1,-1,-2.000000000e+00f,2.621510625e-02f},
{-2,-1,-1,-2.000000000e+00f,1.479910165e-01f},
{21,7,8,-3.130927384e-01f,2.340810746e-01f},
{-2,-1,-1,-2.000000000e+00f,3.669344485e-01f},
{-2,-1,-1,-2.000000000e+00f,1.761247963e-01f},
{17,10,13,4.281929228e-03f,4.336059391e-01f},
{1,11,12,3.606645390e-02f,1.451552510e-01f},
{-2,-1,-1,-2.000000000e+00f,6.771397591e-02f},
{-2,-1,-1,-2.000000000e+00f,3.180169165e-01f},
{14,14,15,-4.032287300e-01f,4.820587039e-01f},
{-2,-1,-1,-2.000000000e+00f,6.326383948e-01f},
{-2,-1,-1,-2.000000000e+00f,3.651484847e-01f},
{1,17,24,7.438102365e-02f,8.172779679e-01f},
{20,18,21,-4.691607654e-01f,6.675705910e-01f},
{36,19,20,1.295899451e-01f,8.250358701e-01f},
{-2,-1,-1,-2.000000000e+00f,6.883468628e-01f},
{-2,-1,-1,-2.000000000e+00f,8.842878342e-01f},
{9,22,23,5.206420422e-01f,6.151537895e-01f},
{-2,-1,-1,-2.000000000e+00f,5.340166092e-01f},
{-2,-1,-1,-2.000000000e+00f,8.896861076e-01f},
{14,25,28,-4.052602351e-01f,9.091394544e-01f},
{38,26,27,2.873219363e-02f,9.511730075e-01f},
{-2,-1,-1,-2.000000000e+00f,8.940082788e-01f},
{-2,-1,-1,-2.000000000e+00f,9.701820612e-01f},
{3,29,30,1.629722863e-02f,8.436897993e-01f},
{-2,-1,-1,-2.000000000e+00f,7.109304667e-01f},
{-2,-1,-1,-2.000000000e+00f,8.919540048e-01f},
};
static const TreeNode TREE_2[] = {
{37,1,16,3.295816779e-01f,5.020859838e-01f},
{2,2,9,1.170837507e-01f,2.139295191e-01f},
{41,3,6,-3.159120679e-01f,8.545625955e-02f},
{13,4,5,-9.016784281e-02f,6.378865838e-01f},
{-2,-1,-1,-2.000000000e+00f,3.678160906e-01f},
{-2,-1,-1,-2.000000000e+00f,8.574766517e-01f},
{9,7,8,1.409240663e-01f,7.365290821e-02f},
{-2,-1,-1,-2.000000000e+00f,4.664489627e-02f},
{-2,-1,-1,-2.000000000e+00f,2.396222651e-01f},
{37,10,13,2.133984566e-01f,5.037091374e-01f},
{8,11,12,7.185093313e-02f,4.166878462e-01f},
{-2,-1,-1,-2.000000000e+00f,3.616247773e-01f},
{-2,-1,-1,-2.000000000e+00f,6.418875456e-01f},
{7,14,15,-2.469950467e-01f,5.836444497e-01f},
{-2,-1,-1,-2.000000000e+00f,2.945499122e-01f},
{-2,-1,-1,-2.000000000e+00f,6.516789198e-01f},
{0,17,24,-4.053009152e-01f,7.860943079e-01f},
{3,18,21,2.350085042e-02f,2.506183088e-01f},
{17,19,20,8.602588437e-03f,1.474767327e-01f},
{-2,-1,-1,-2.000000000e+00f,3.304113448e-02f},
{-2,-1,-1,-2.000000000e+00f,4.516128898e-01f},
{15,22,23,4.766089097e-02f,7.974026203e-01f},
{-2,-1,-1,-2.000000000e+00f,0.000000000e+00f},
{-2,-1,-1,-2.000000000e+00f,8.721590638e-01f},
{37,25,28,6.259602904e-01f,8.111258745e-01f},
{3,26,27,8.685091510e-03f,6.662659645e-01f},
{-2,-1,-1,-2.000000000e+00f,4.669513702e-01f},
{-2,-1,-1,-2.000000000e+00f,7.323605418e-01f},
{9,29,30,4.246765077e-01f,9.146321416e-01f},
{-2,-1,-1,-2.000000000e+00f,7.933070660e-01f},
{-2,-1,-1,-2.000000000e+00f,9.591707587e-01f},
};
static const TreeNode TREE_3[] = {
{8,1,16,6.562510878e-02f,4.998887479e-01f},
{1,2,9,4.241361469e-02f,2.180108875e-01f},
{35,3,6,1.785687208e-01f,1.027314663e-01f},
{17,4,5,5.874738563e-03f,8.564692736e-02f},
{-2,-1,-1,-2.000000000e+00f,4.158551246e-02f},
{-2,-1,-1,-2.000000000e+00f,2.072021067e-01f},
{5,7,8,-2.809753455e-02f,3.851575553e-01f},
{-2,-1,-1,-2.000000000e+00f,2.264631093e-01f},
{-2,-1,-1,-2.000000000e+00f,5.369018912e-01f},
{0,10,13,-4.114864469e-01f,5.513162613e-01f},
{10,11,12,1.026674453e-02f,7.737397403e-02f},
{-2,-1,-1,-2.000000000e+00f,1.692047343e-02f},
{-2,-1,-1,-2.000000000e+00f,2.137404531e-01f},
{7,14,15,-2.682542205e-01f,5.806709528e-01f},
{-2,-1,-1,-2.000000000e+00f,3.304572999e-01f},
{-2,-1,-1,-2.000000000e+00f,6.313078403e-01f},
{37,17,24,5.374045968e-01f,8.147152066e-01f},
{13,18,21,-1.050170958e-01f,6.557837725e-01f},
{21,19,20,-2.488506734e-01f,5.432312489e-01f},
{-2,-1,-1,-2.000000000e+00f,6.543269157e-01f},
{-2,-1,-1,-2.000000000e+00f,3.760250807e-01f},
{16,22,23,3.055664003e-01f,7.998521328e-01f},
{-2,-1,-1,-2.000000000e+00f,7.549773455e-01f},
{-2,-1,-1,-2.000000000e+00f,8.747122884e-01f},
{12,25,28,4.525085539e-02f,9.053121209e-01f},
{38,26,27,4.474335164e-02f,8.651431203e-01f},
{-2,-1,-1,-2.000000000e+00f,8.044703007e-01f},
{-2,-1,-1,-2.000000000e+00f,9.374656081e-01f},
{17,29,30,4.204690922e-03f,9.690688848e-01f},
{-2,-1,-1,-2.000000000e+00f,0.000000000e+00f},
{-2,-1,-1,-2.000000000e+00f,9.697646499e-01f},
};
static const TreeNode TREE_4[] = {
{1,1,16,4.311306402e-02f,5.011774302e-01f},
{10,2,9,7.552995812e-03f,1.829716861e-01f},
{32,3,6,2.756641209e-01f,5.921052769e-02f},
{3,4,5,4.325287882e-03f,5.479237810e-02f},
{-2,-1,-1,-2.000000000e+00f,2.839251980e-02f},
{-2,-1,-1,-2.000000000e+00f,1.260270774e-01f},
{6,7,8,5.432739109e-02f,5.880149603e-01f},
{-2,-1,-1,-2.000000000e+00f,3.571428657e-01f},
{-2,-1,-1,-2.000000000e+00f,6.939890981e-01f},
{8,10,13,8.630977571e-02f,3.972808719e-01f},
{12,11,12,-1.527856588e-01f,2.871086299e-01f},
{-2,-1,-1,-2.000000000e+00f,2.037647069e-01f},
{-2,-1,-1,-2.000000000e+00f,3.636232615e-01f},
{4,14,15,2.465289235e-01f,6.746692061e-01f},
{-2,-1,-1,-2.000000000e+00f,7.082486153e-01f},
{-2,-1,-1,-2.000000000e+00f,5.028901696e-01f},
{38,17,24,3.087633476e-02f,7.848012447e-01f},
{1,18,21,8.360981196e-02f,6.754909754e-01f},
{37,19,20,2.439206094e-01f,5.746706128e-01f},
{-2,-1,-1,-2.000000000e+00f,4.243156314e-01f},
{-2,-1,-1,-2.000000000e+00f,6.355925202e-01f},
{20,22,23,-3.555957377e-01f,8.084341288e-01f},
{-2,-1,-1,-2.000000000e+00f,8.848265409e-01f},
{-2,-1,-1,-2.000000000e+00f,6.872525811e-01f},
{1,25,28,1.190227717e-01f,9.086624980e-01f},
{9,26,27,3.079833984e-01f,8.441270590e-01f},
{-2,-1,-1,-2.000000000e+00f,5.009310842e-01f},
{-2,-1,-1,-2.000000000e+00f,9.013559818e-01f},
{35,29,30,8.099120855e-01f,9.557035565e-01f},
{-2,-1,-1,-2.000000000e+00f,9.585887790e-01f},
{-2,-1,-1,-2.000000000e+00f,6.153846383e-01f},
};
static const TreeNode TREE_5[] = {
{38,1,16,1.466070022e-02f,5.000278354e-01f},
{10,2,9,6.605478004e-03f,1.944538951e-01f},
{37,3,6,2.301801443e-01f,6.483411044e-02f},
{17,4,5,3.424070543e-03f,4.885016009e-02f},
{-2,-1,-1,-2.000000000e+00f,1.655591838e-02f},
{-2,-1,-1,-2.000000000e+00f,1.067892537e-01f},
{12,7,8,-2.586315870e-01f,2.365048528e-01f},
{-2,-1,-1,-2.000000000e+00f,5.088495463e-02f},
{-2,-1,-1,-2.000000000e+00f,2.760244906e-01f},
{1,10,13,4.748875648e-02f,3.928607702e-01f},
{20,11,12,-4.641143680e-01f,2.778722644e-01f},
{-2,-1,-1,-2.000000000e+00f,4.351807237e-01f},
{-2,-1,-1,-2.000000000e+00f,2.478128672e-01f},
{37,14,15,1.577771008e-01f,6.110622287e-01f},
{-2,-1,-1,-2.000000000e+00f,4.744568467e-01f},
{-2,-1,-1,-2.000000000e+00f,6.565617323e-01f},
{36,17,24,1.981647313e-01f,7.638857961e-01f},
{15,18,21,1.075775921e-01f,6.156591773e-01f},
{26,19,20,-2.746619582e-01f,5.549984574e-01f},
{-2,-1,-1,-2.000000000e+00f,6.426948309e-01f},
{-2,-1,-1,-2.000000000e+00f,4.607874453e-01f},
{30,22,23,7.195236683e-01f,7.833403945e-01f},
{-2,-1,-1,-2.000000000e+00f,7.048434615e-01f},
{-2,-1,-1,-2.000000000e+00f,8.713257313e-01f},
{6,25,28,-4.040802121e-01f,8.900722861e-01f},
{0,26,27,-4.096485078e-01f,5.788452029e-01f},
{-2,-1,-1,-2.000000000e+00f,2.119914293e-01f},
{-2,-1,-1,-2.000000000e+00f,8.828749061e-01f},
{2,29,30,2.444030792e-01f,9.120355844e-01f},
{-2,-1,-1,-2.000000000e+00f,8.193809986e-01f},
{-2,-1,-1,-2.000000000e+00f,9.462970495e-01f},
};
static const TreeNode TREE_6[] = {
{10,1,16,9.145802818e-03f,4.977286160e-01f},
{1,2,9,4.288367182e-02f,1.505013704e-01f},
{10,3,6,6.095098332e-03f,8.029986918e-02f},
{15,4,5,1.039738357e-01f,3.965405747e-02f},
{-2,-1,-1,-2.000000000e+00f,3.769233450e-02f},
{-2,-1,-1,-2.000000000e+00f,5.130434632e-01f},
{39,7,8,2.041795254e-01f,2.049983442e-01f},
{-2,-1,-1,-2.000000000e+00f,1.667718738e-01f},
{-2,-1,-1,-2.000000000e+00f,4.655765891e-01f},
{19,10,13,-3.822125494e-01f,5.416163802e-01f},
{14,11,12,-3.941361606e-01f,7.587609291e-01f},
{-2,-1,-1,-2.000000000e+00f,7.824793458e-01f},
{-2,-1,-1,-2.000000000e+00f,3.391812742e-01f},
{29,14,15,2.428646386e-01f,3.396391273e-01f},
{-2,-1,-1,-2.000000000e+00f,3.125766814e-01f},
{-2,-1,-1,-2.000000000e+00f,8.409090638e-01f},
{37,17,24,4.463511407e-01f,7.331331968e-01f},
{2,18,21,1.682556123e-01f,5.136823058e-01f},
{13,19,20,-6.531249732e-02f,3.401900232e-01f},
{-2,-1,-1,-2.000000000e+00f,2.768754959e-01f},
{-2,-1,-1,-2.000000000e+00f,4.995134473e-01f},
{2,22,23,4.095580876e-01f,6.618608832e-01f},
{-2,-1,-1,-2.000000000e+00f,6.037622690e-01f},
{-2,-1,-1,-2.000000000e+00f,8.769914508e-01f},
{3,25,28,1.470084395e-02f,8.598738909e-01f},
{0,26,27,-4.435888827e-01f,7.504110932e-01f},
{-2,-1,-1,-2.000000000e+00f,5.307262391e-02f},
{-2,-1,-1,-2.000000000e+00f,7.687284350e-01f},
{8,29,30,1.197489500e-01f,9.170839190e-01f},
{-2,-1,-1,-2.000000000e+00f,7.669867873e-01f},
{-2,-1,-1,-2.000000000e+00f,9.560796618e-01f},
};
static const TreeNode TREE_7[] = {
{2,1,16,1.442077756e-01f,4.980901778e-01f},
{8,2,9,6.976222247e-02f,1.688366532e-01f},
{10,3,6,6.199698895e-03f,1.024817228e-01f},
{14,4,5,-4.763209820e-01f,4.214030132e-02f},
{-2,-1,-1,-2.000000000e+00f,4.242424369e-01f},
{-2,-1,-1,-2.000000000e+00f,3.670581430e-02f},
{32,7,8,3.937141895e-01f,2.182806730e-01f},
{-2,-1,-1,-2.000000000e+00f,2.066282183e-01f},
{-2,-1,-1,-2.000000000e+00f,7.391304374e-01f},
{12,10,13,-1.156341583e-01f,5.917791724e-01f},
{38,11,12,2.911545150e-02f,4.766405523e-01f},
{-2,-1,-1,-2.000000000e+00f,3.960361481e-01f},
{-2,-1,-1,-2.000000000e+00f,7.569528222e-01f},
{41,14,15,2.077734321e-01f,7.322134376e-01f},
{-2,-1,-1,-2.000000000e+00f,6.634460688e-01f},
{-2,-1,-1,-2.000000000e+00f,8.414322138e-01f},
{1,17,24,9.118059278e-02f,7.793574333e-01f},
{14,18,21,-4.033447504e-01f,6.490992308e-01f},
{19,19,20,-4.825463891e-01f,7.607471347e-01f},
{-2,-1,-1,-2.000000000e+00f,8.279914260e-01f},
{-2,-1,-1,-2.000000000e+00f,7.127983570e-01f},
{36,22,23,1.305399984e-01f,5.418091416e-01f},
{-2,-1,-1,-2.000000000e+00f,4.100867510e-01f},
{-2,-1,-1,-2.000000000e+00f,6.655615568e-01f},
{8,25,28,9.851338714e-02f,8.964065313e-01f},
{19,26,27,-3.465118408e-01f,7.577075362e-01f},
{-2,-1,-1,-2.000000000e+00f,8.591108322e-01f},
{-2,-1,-1,-2.000000000e+00f,5.971579552e-01f},
{36,29,30,2.583213747e-01f,9.439137578e-01f},
{-2,-1,-1,-2.000000000e+00f,8.988682628e-01f},
{-2,-1,-1,-2.000000000e+00f,9.696885943e-01f},
};
constexpr int TREE_COUNT = 8;
static const TreeNode* const TREES[] = {TREE_0, TREE_1, TREE_2, TREE_3, TREE_4, TREE_5, TREE_6, TREE_7};
constexpr float START_THRESHOLD = 5.000000000e-01f;
constexpr float STOP_THRESHOLD = 2.000000000e-01f;
constexpr long long STOP_HOLD_US = 200000;


// BEGIN DETECTOR SNAPSHOT
#include <cmath>
#include <cstdint>
#include <cstring>

// Portable detector only. No Bluetooth, packet serialization or transport queue.
namespace gesture {
constexpr int RAW_HZ=500, RAW_PERIOD_US=2000;
constexpr int GROUP=5, WINDOW=30, HOP=5, BLOCK=10, ENERGY_WINDOW=5, N_FEATURES=42;
static_assert(MODEL_RAW_HZ==RAW_HZ,"Retrain and re-export the model for 500 Hz");
enum Action { NONE=0, START=1, STOP=2, GAP=3 };

struct Result {
  bool gap=false, sampleReady=false, decisionReady=false, useful=false;
  int64_t sampleTimeUs=0;
  float sample[6]={}, score=0, energy=0;
  Action action=NONE;
};

class Frontend {
  float raw[WINDOW][6]={}, pending[6]={};
  int groupCount=0;
  int head=-1, count=0, hop=0;
  bool previous=false;
  uint32_t previousSeq=0;
  int64_t previousTime=0;
public:
  void reset() { head=-1; count=0; hop=0; previous=false; groupCount=0; std::memset(pending,0,sizeof(pending)); }
  void summarize(float* out) const {
    for(int c=0;c<6;++c) {
      float sum=0, square=0, lo=1e30f, hi=-1e30f, difference=0, last=0, block[3]={};
      for(int j=0;j<WINDOW;++j) {
        float v=raw[(head+1+j)%WINDOW][c]/(c<3 ? 2.f : 250.f);
        sum+=v; square+=v*v; if(v<lo) lo=v; if(v>hi) hi=v;
        if(j>0) difference+=std::fabs(v-last);
        last=v; block[j/BLOCK]+=v;
      }
      const float mean=sum/WINDOW, variance=square/WINDOW-mean*mean;
      out[c*7]=mean;
      out[c*7+1]=std::sqrt(variance>0 ? variance : 0);
      out[c*7+2]=hi-lo;
      out[c*7+3]=difference/(WINDOW-1);
      for(int j=0;j<3;++j) out[c*7+4+j]=block[j]/BLOCK;
    }
  }
  Result push(uint32_t seq, int64_t time, const float* x, float* features) {
    Result r;
    r.gap=previous && (seq!=previousSeq+1 || time-previousTime!=RAW_PERIOD_US);
    bool valid=true;
    for(int c=0;c<6;++c) if(!std::isfinite(x[c])) valid=false;
    if(r.gap || !valid) reset();
    previous=true; previousSeq=seq; previousTime=time;
    if(!valid) { r.gap=true; return r; }
    for(int c=0;c<6;++c) pending[c]+=x[c];
    if(++groupCount<GROUP) return r;
    groupCount=0;
    head=(head+1)%WINDOW;
    for(int c=0;c<6;++c) { raw[head][c]=pending[c]/GROUP; pending[c]=0; }
    if(count<WINDOW) ++count;
    r.sampleReady=true; r.sampleTimeUs=time-(GROUP-1)*RAW_PERIOD_US/2;
    for(int c=0;c<6;++c) r.sample[c]=raw[head][c];
    if(count==WINDOW) {
      if(hop==0) {
        summarize(features); r.decisionReady=true;
        for(int c=0;c<6;++c) {
          float sum=0, square=0;
          for(int j=0;j<ENERGY_WINDOW;++j) { float v=raw[(head-j+WINDOW)%WINDOW][c]; sum+=v; square+=v*v; }
          if(c<3) { float variance=square/ENERGY_WINDOW-(sum/ENERGY_WINDOW)*(sum/ENERGY_WINDOW); r.energy+=(variance>0 ? variance : 0)/.01f; }
          else r.energy+=square/ENERGY_WINDOW/400;
        }
      }
      hop=(hop+1)%HOP;
    }
    return r;
  }
};

inline float probability(const float* x) {
  float sum=0;
  for(int t=0;t<TREE_COUNT;++t) {
    const TreeNode* nodes=TREES[t];
    int i=0;
    while(nodes[i].left!=-1) i=x[nodes[i].feature]<=nodes[i].threshold ? nodes[i].left : nodes[i].right;
    sum+=nodes[i].score;
  }
  return sum/TREE_COUNT;
}

class Gate {
  int64_t lowSince=-1;
public:
  bool active=false;
  void reset() { active=false; lowSince=-1; }
  Action step(float score, int64_t time) {
    if(!std::isfinite(score) || score<0 || score>1) { lowSince=-1; return NONE; }
    if(!active) {
      if(score>=START_THRESHOLD) { active=true; return START; }
    } else if(score<STOP_THRESHOLD) {
      if(lowSince<0) lowSince=time;
      if(time-lowSince>=STOP_HOLD_US) { reset(); return STOP; }
    } else lowSince=-1;
    return NONE;
  }
};

class Detector {
  Frontend front;
  Gate gate;
public:
  void reset() { front.reset(); gate.reset(); }
  Result push(uint32_t seq, int64_t time, const float* x) {
    float features[N_FEATURES];
    Result r=front.push(seq,time,x,features);
    if(r.gap) { gate.reset(); r.action=GAP; }
    if(r.decisionReady && MODEL_READY) {
      r.score=probability(features);
      r.action=gate.step(r.score,time); // Legacy binary state; use Segmenter for individual symbols.
    } else if(!MODEL_READY) r.decisionReady=false;
    r.useful=gate.active;
    return r;
  }
};
}


// BEGIN SEGMENTER SNAPSHOT

namespace gesture {
constexpr int64_t PRE_US=300000, PAUSE_US=150000, MIN_US=250000, MAX_US=6000000;
constexpr float DIP_RATIO=.20f, ENERGY_FLOOR=.05f;
enum SegmentAction { SEG_NONE=0, SEG_START=1, SEG_COMPLETE=2, SEG_REJECT=3 };
enum EndReason { MOTION_DIP=1, NONGESTURE=2, TIMEOUT=3 };
struct SegmentEvent {
  SegmentAction kind=SEG_NONE;
  int64_t startUs=0, endUs=0, triggerUs=0, emittedUs=0;
  EndReason reason=MOTION_DIP;
};

class Segmenter {
  int64_t start=0, trigger=0, dipSince=-1, lowSince=-1, previousEnd=-1;
  float energies[10]={}, rearmLevel=ENERGY_FLOOR;
  int energyHead=0, energyCount=0, resume=0;
  bool locked=false;
  bool timeoutLock=false;
  int64_t unlockSince=-1;
public:
  bool active=false;
  void reset() {
    active=false; locked=false; start=trigger=0; dipSince=lowSince=previousEnd=-1;
    rearmLevel=ENERGY_FLOOR; energyHead=energyCount=resume=0;
    timeoutLock=false; unlockSince=-1;
  }
  SegmentEvent step(float score, float energy, int64_t time, int64_t availableStart=0) {
    SegmentEvent e;
    if(!std::isfinite(score) || !std::isfinite(energy)) { dipSince=lowSince=-1; return e; }
    if(!active) {
      if(timeoutLock) {
        if(score<STOP_THRESHOLD || energy<=rearmLevel) {
          if(unlockSince<0) unlockSince=time;
          if(time-unlockSince>=PAUSE_US) timeoutLock=false;
        } else unlockSince=-1;
        return e;
      }
      if(locked) {
        if(score<STOP_THRESHOLD) locked=false;
        else if(energy>rearmLevel*2) { if(++resume>=2) locked=false; }
        else resume=0;
        if(locked) return e;
      }
      if(score>=START_THRESHOLD) {
        active=true; start=time-PRE_US;
        if(start<availableStart) start=availableStart;
        if(start<previousEnd) start=previousEnd;
        trigger=time; energyCount=1; energyHead=1; energies[0]=energy;
        e.kind=SEG_START; e.startUs=start; e.triggerUs=trigger; e.emittedUs=time;
      }
      return e;
    }
    float peak=energyCount ? energies[0] : energy;
    for(int i=1;i<energyCount;++i) if(energies[i]>peak) peak=energies[i];
    energies[energyHead]=energy; energyHead=(energyHead+1)%10;
    if(energyCount<10) ++energyCount;
    float threshold=peak*DIP_RATIO; if(threshold<ENERGY_FLOOR) threshold=ENERGY_FLOOR;
    if(dipSince<0) { if(energy<=threshold) { dipSince=time; rearmLevel=threshold; } }
    else if(energy>rearmLevel) dipSince=-1;
    if(score<STOP_THRESHOLD) { if(lowSince<0) lowSince=time; }
    else lowSince=-1;
    int64_t end=-1;
    if(dipSince>=0 && time-dipSince>=PAUSE_US) { end=dipSince+RAW_PERIOD_US; e.reason=MOTION_DIP; }
    else if(lowSince>=0 && time-lowSince>=STOP_HOLD_US) { end=lowSince+RAW_PERIOD_US; e.reason=NONGESTURE; }
    else if(time-start>=MAX_US) e.reason=TIMEOUT;
    else return e;
    active=false; locked=true; resume=0; dipSince=lowSince=-1;
    previousEnd=end>=0 ? end : time+RAW_PERIOD_US;
    if(e.reason==TIMEOUT) { timeoutLock=true; unlockSince=-1; rearmLevel=threshold; }
    e.startUs=start; e.endUs=end; e.triggerUs=trigger; e.emittedUs=time;
    e.kind=end>=0 && end-start>=MIN_US && end-start<=MAX_US ? SEG_COMPLETE : SEG_REJECT;
    return e;
  }
};

} // namespace gesture

// BEGIN PIPELINE INTEGRATION
namespace pipeline {
constexpr uint8_t MPU=0x68, SDA_PIN=21, SCL_PIN=22, MOTOR_PIN=2;
constexpr uint8_t TYPE_SENSOR=0x01, TYPE_CMD=0x03, TYPE_ECHO=0x04;
constexpr size_t PACKET_LEN=26, CRC_OFFSET=24;
constexpr uint16_t GOOD_SYMBOL=0x4701, INVALID_SYMBOL=0x4702;
constexpr uint16_t PREFERRED_MTU=128, FAST_CONN_INTERVAL=12, CONN_TIMEOUT=200;
constexpr uint32_t SHORT_MS=150, LONG_MS=500, MOTOR_GAP_MS=500;
constexpr uint8_t SHORT_DUTY=180, LONG_DUTY=170, PWM_CHANNEL=0;
constexpr int TX_CAPACITY=64, RESULT_CAPACITY=16;
constexpr uint32_t TX_MAX_AGE_MS=500;

// All functions are defined inline in classes to avoid Arduino's automatic
// prototype generation placing custom types before their declarations.
struct Protocol {
  static uint16_t read16(const uint8_t* p) {
    return uint16_t(p[0]) | (uint16_t(p[1])<<8);
  }
  static uint32_t read32(const uint8_t* p) {
    return uint32_t(read16(p)) | (uint32_t(read16(p+2))<<16);
  }
  static void put16(uint8_t* p, uint16_t v) { p[0]=uint8_t(v); p[1]=uint8_t(v>>8); }
  static void put32(uint8_t* p, uint32_t v) {
    put16(p,uint16_t(v)); put16(p+2,uint16_t(v>>16));
  }
  static uint16_t crc(const uint8_t* p, size_t n) {
    uint16_t c=0xffff;
    for(size_t i=0;i<n;++i) {
      c^=uint16_t(p[i])<<8;
      for(int b=0;b<8;++b) c=(c&0x8000) ? uint16_t((c<<1)^0x1021) : uint16_t(c<<1);
    }
    return c;
  }
  static void seal(uint8_t* p) { put16(p+CRC_OFFSET,crc(p,CRC_OFFSET)); }
  static bool valid(const uint8_t* p) { return read16(p+CRC_OFFSET)==crc(p,CRC_OFFSET); }
};

struct Pattern { uint8_t pulses, duty; uint32_t duration; };
class Motor {
  Pattern queue[16]={}, current={};
  int head=0, count=0;
  bool on=false, waiting=false;
  uint32_t deadline=0;
public:
  static void duty(uint8_t value) {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcWrite(MOTOR_PIN,value);
#else
    ledcWrite(PWM_CHANNEL,value);
#endif
  }
  bool begin() {
#if ESP_ARDUINO_VERSION_MAJOR >= 3
    if(!ledcAttach(MOTOR_PIN,20000,8)) return false;
#else
    if(ledcSetup(PWM_CHANNEL,20000,8)==0) return false;
    ledcAttachPin(MOTOR_PIN,PWM_CHANNEL);
#endif
    reset(); return true;
  }
  void reset() { duty(0); head=count=0; on=waiting=false; current={}; }
  bool enqueue(uint8_t pulses, bool isLong) {
    if(count==16) return false;
    queue[(head+count)%16]={pulses,isLong ? LONG_DUTY : SHORT_DUTY,
                          isLong ? LONG_MS : SHORT_MS};
    ++count; return true;
  }
  void update(uint32_t now) {
    if(on) {
      if(int32_t(now-deadline)<0) return;
      duty(0); on=false; --current.pulses;
      waiting=true; deadline=now+MOTOR_GAP_MS;
    }
    if(waiting) {
      if(int32_t(now-deadline)<0) return;
      waiting=false;
    }
    if(!current.pulses) {
      if(!count) return;
      current=queue[head]; head=(head+1)%16; --count;
    }
    duty(current.duty); on=true; deadline=now+current.duration;
  }
};

struct Command { uint32_t session; uint8_t data[PACKET_LEN]; };
struct Packet { uint8_t data[PACKET_LEN]; uint32_t queuedMs; };
struct ResultSlot { uint32_t id=0; bool answered=false; };
QueueHandle_t commands=nullptr;
std::atomic<uint32_t> session{0};
std::atomic<uint16_t> connection{0xffff};
std::atomic<bool> subscribed{false};
std::atomic<uint32_t> commandDrops{0};

class ServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer*, NimBLEConnInfo& info) override {
    subscribed.store(false);
    connection.store(info.getConnHandle());
    session.fetch_add(1);
  }
  void onDisconnect(NimBLEServer*, NimBLEConnInfo&, int) override {
    connection.store(0xffff); subscribed.store(false); session.fetch_add(1);
    NimBLEDevice::startAdvertising();
  }
};
class SensorCallbacks : public NimBLECharacteristicCallbacks {
  void onSubscribe(NimBLECharacteristic*, NimBLEConnInfo&, uint16_t value) override {
    subscribed.store((value&1)!=0);
  }
};
class CommandCallbacks : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* chr, NimBLEConnInfo& info) override {
    const uint32_t epoch=session.load();
    const NimBLEAttValue value=chr->getValue();
    if(!info.isEncrypted() || info.getConnHandle()!=connection.load() ||
       value.length()!=PACKET_LEN || value[0]!=TYPE_CMD ||
       !Protocol::valid(value.data())) {
      commandDrops.fetch_add(1); return;
    }
    Command cmd; cmd.session=epoch;
    std::memcpy(cmd.data,value.data(),PACKET_LEN);
    if(xQueueSend(commands,&cmd,0)!=pdTRUE) commandDrops.fetch_add(1);
  }
};

class Firmware {
  NimBLEServer* server=nullptr;
  NimBLECharacteristic* sensor=nullptr;
  gesture::Detector detector;
  gesture::Segmenter segmenter;
  Motor motor;
  Packet tx[TX_CAPACITY]={};
  int txHead=0, txCount=0;
  ResultSlot results[RESULT_CAPACITY]={};
  int resultNext=0;
  uint32_t seenSession=0, gestureId=0, rawSeq=0, encryptedAt=0;
  uint8_t packetSeq=0, decimation=0;
  bool streaming=false, encrypted=false, paramsRequested=false, haveClock=false;
  int64_t sampleTime=0, lastSampleTime=-1, lastProgressUs=0;
  uint32_t fifoGaps=0, txDrops=0, eogDrops=0, notifyFailures=0, motorDrops=0;
  uint32_t lastReport=0;

  void fatal(const char* reason) {
    motor.reset(); Serial.println(reason);
    while(true) delay(1000);
  }
  bool writeReg(uint8_t reg, uint8_t value) {
    Wire.beginTransmission(MPU); Wire.write(reg); Wire.write(value);
    return Wire.endTransmission(true)==0;
  }
  bool readBytes(uint8_t reg, uint8_t* out, uint8_t size) {
    Wire.beginTransmission(MPU); Wire.write(reg);
    if(Wire.endTransmission(false)!=0 ||
       Wire.requestFrom(MPU,size,uint8_t(true))!=size) return false;
    for(int i=0;i<size;++i) out[i]=Wire.read();
    return true;
  }
  void clearResults() {
    for(auto& r:results) r={};
    resultNext=0;
  }
  void discardTx() {
    while(txCount) {
      const auto& p=tx[txHead];
      if(p.data[0]==TYPE_SENSOR && Protocol::read16(p.data+14)==1) ++eogDrops;
      txHead=(txHead+1)%TX_CAPACITY; --txCount; ++txDrops;
    }
    txHead=0;
  }
  void resetFifo() {
    if(!writeReg(0x23,0) || !writeReg(0x6A,0x04) ||
       !writeReg(0x6A,0x40) || !writeReg(0x23,0x78)) fatal("ERROR,FIFO_RESET");
    haveClock=false; decimation=0; ++rawSeq;
    detector.reset(); segmenter.reset();
    lastProgressUs=esp_timer_get_time();
  }
  void gap(const char* reason) {
    ++fifoGaps; ++packetSeq; // visible sequence discontinuity on the receiver
    discardTx(); clearResults(); resetFifo();
    Serial.printf("GAP,%s,count=%lu\n",reason,(unsigned long)fifoGaps);
  }
  void localPulse(uint8_t count) {
    if(!motor.enqueue(count,false)) ++motorDrops;
  }
  // Bounded queues keep BLE congestion from blocking sensor acquisition.
  bool enqueue(const uint8_t* bytes) {
    if(txCount==TX_CAPACITY) {
      discardTx(); clearResults(); ++packetSeq;
      Serial.println("GAP,TX_OVERFLOW");
      // Drop the new packet too: a result slot for its EOG is no longer valid.
      ++txDrops;
      if(bytes[0]==TYPE_SENSOR && Protocol::read16(bytes+14)) ++eogDrops;
      return false;
    }
    auto& p=tx[(txHead+txCount)%TX_CAPACITY];
    std::memcpy(p.data,bytes,PACKET_LEN); p.queuedMs=millis(); ++txCount;
    return true;
  }
  void transmit() {
    // Maximum four notifications per iteration, then service the FIFO again.
    for(int i=0;i<4 && txCount && streaming;++i) {
      if(session.load()!=seenSession || connection.load()==0xffff) return;
      auto& p=tx[txHead];
      if(millis()-p.queuedMs>TX_MAX_AGE_MS) {
        discardTx(); clearResults(); ++packetSeq;
        Serial.println("GAP,TX_EXPIRED"); return;
      }
      sensor->setValue(p.data,PACKET_LEN);
      if(!sensor->notify(p.data,PACKET_LEN)) { ++notifyFailures; return; }
      txHead=(txHead+1)%TX_CAPACITY; --txCount;
    }
  }
  void updateConnection(uint32_t now) {
    const uint32_t epoch=session.load();
    const uint16_t handle=connection.load();
    if(epoch!=seenSession) {
      seenSession=epoch; streaming=encrypted=paramsRequested=false;
      discardTx(); clearResults(); motor.reset(); resetFifo();
      // Do not reset gestureId: prevents old result IDs matching after reconnect.
      Serial.printf("BLE,SESSION,%lu,handle=%u\n",(unsigned long)epoch,handle);
    }
    if(handle==0xffff) return;
    if(!encrypted && server->getPeerInfoByHandle(handle).isEncrypted()) {
      encrypted=true; encryptedAt=now;
    }
    if(encrypted && !paramsRequested && now-encryptedAt>=1000) {
      paramsRequested=true;
      server->updateConnParams(handle,FAST_CONN_INTERVAL,FAST_CONN_INTERVAL,0,CONN_TIMEOUT);
    }
    const bool ready=encrypted && subscribed.load() &&
                     server->getPeerMTU(handle)>=PACKET_LEN+3;
    if(ready!=streaming) {
      streaming=ready; discardTx(); clearResults(); resetFifo();
      ++packetSeq;
      Serial.printf("BLE,STREAMING,%d\n",int(streaming));
    }
  }
  void processCommand() {
    Command cmd;
    if(xQueueReceive(commands,&cmd,0)!=pdTRUE) return;
    if(cmd.session!=seenSession || session.load()!=seenSession || !streaming) return;
    const uint16_t opcode=Protocol::read16(cmd.data+2);
    const uint32_t id=Protocol::read32(cmd.data+4);
    uint16_t status=0;
    bool malformed=opcode!=GOOD_SYMBOL && opcode!=INVALID_SYMBOL;
    for(int i=8;i<24;++i) if(cmd.data[i]) malformed=true;
    ResultSlot* slot=nullptr;
    for(auto& r:results) if(id && r.id==id) { slot=&r; break; }
    if(malformed) status=4;
    else if(!slot) status=2;
    else if(slot->answered) status=1;
    else if(!motor.enqueue(opcode==GOOD_SYMBOL ? 1 : 3,true)) { status=3; ++motorDrops; }
    else slot->answered=true;
    cmd.data[0]=TYPE_ECHO;
    Protocol::put16(cmd.data+8,status); Protocol::seal(cmd.data);
    enqueue(cmd.data);
    Serial.printf("RESULT,id=%lu,opcode=%04x,status=%u\n",(unsigned long)id,opcode,status);
  }
  void processSample(const int16_t* counts) {
    float x[6];
    for(int c=0;c<6;++c) x[c]=counts[c]/(c<3 ? 8192.f : 65.5f);
    const auto r=detector.push(rawSeq++,sampleTime,x);
    bool ended=false;
    if(r.decisionReady) {
      const auto event=segmenter.step(r.score,r.energy,sampleTime);
      if(event.kind==gesture::SEG_START) {
        if(++gestureId==0) ++gestureId;
        localPulse(1);
        Serial.printf("GESTURE,START,id=%lu,score=%.3f\n",(unsigned long)gestureId,r.score);
      } else if(event.kind==gesture::SEG_COMPLETE || event.kind==gesture::SEG_REJECT) {
        localPulse(2); ended=event.kind==gesture::SEG_COMPLETE;
        Serial.printf("GESTURE,%s,id=%lu,reason=%d\n",ended ? "END" : "ABORT",
                      (unsigned long)gestureId,int(event.reason));
      }
    }
    // Detector decisions are aligned to every fifth raw sample.
    if(++decimation<5) return;
    decimation=0;
    uint8_t packet[PACKET_LEN]={TYPE_SENSOR,packetSeq++};
    for(int c=0;c<6;++c) Protocol::put16(packet+2+2*c,uint16_t(counts[c]));
    Protocol::put16(packet+14,ended ? 1 : 0);
    Protocol::put32(packet+16,uint32_t(sampleTime/1000));
    Protocol::put32(packet+20,gestureId);
    Protocol::seal(packet);
    if(streaming) {
      if(ended) {
        results[resultNext].id=gestureId; results[resultNext].answered=false;
        resultNext=(resultNext+1)%RESULT_CAPACITY;
      }
      enqueue(packet);
    }
  }
  void acquire() {
    uint8_t status, bytes[2];
    if(!readBytes(0x3A,&status,1) || !readBytes(0x72,bytes,2)) { gap("I2C_READ"); return; }
    const int count=(int(bytes[0])<<8)|bytes[1];
    if((status&0x10) || count>=1024) { gap("FIFO_OVERFLOW"); return; }
    const int frames=count/12;
    if(!frames) {
      if(esp_timer_get_time()-lastProgressUs>100000) gap("NO_SAMPLES");
      return;
    }
    if(!haveClock) {
      sampleTime=esp_timer_get_time()-int64_t(frames-1)*gesture::RAW_PERIOD_US;
      if(sampleTime<=lastSampleTime) sampleTime=lastSampleTime+gesture::RAW_PERIOD_US;
      haveClock=true;
    }
    for(int k=0;k<frames;++k) {
      uint8_t raw[12]; int16_t counts[6];
      if(!readBytes(0x74,raw,12)) { gap("FIFO_READ"); return; }
      for(int c=0;c<6;++c) counts[c]=int16_t((uint16_t(raw[2*c])<<8)|raw[2*c+1]);
      processSample(counts);
      lastSampleTime=sampleTime; sampleTime+=gesture::RAW_PERIOD_US;
      lastProgressUs=esp_timer_get_time();
      motor.update(millis());
    }
  }
public:
  void begin() {
    Serial.begin(115200);
    if(!motor.begin()) fatal("ERROR,MOTOR_PWM");
    commands=xQueueCreate(16,sizeof(Command));
    if(!commands) fatal("ERROR,COMMAND_QUEUE");
    Wire.begin(SDA_PIN,SCL_PIN,400000); Wire.setTimeOut(10);
    uint8_t who=0;
    if(!readBytes(0x75,&who,1) || who!=0x68 || !writeReg(0x6B,1)) fatal("ERROR,MPU_NOT_FOUND");
    delay(100);
    if(!MODEL_READY || !writeReg(0x1A,3) || !writeReg(0x19,1) ||
       !writeReg(0x1C,8) || !writeReg(0x1B,8) || !writeReg(0x38,0x10)) fatal("ERROR,IMU_CONFIG");
    NimBLEDevice::init(DEVICE_NAME);
    NimBLEDevice::setMTU(PREFERRED_MTU);
    NimBLEDevice::setSecurityAuth(true,false,true);
    NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);
    server=NimBLEDevice::createServer();
    server->setCallbacks(new ServerCallbacks());
    auto* service=server->createService(SERVICE_UUID);
    sensor=service->createCharacteristic(SENSOR_CHAR_UUID,
        NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::NOTIFY | NIMBLE_PROPERTY::READ_ENC);
    sensor->setCallbacks(new SensorCallbacks());
    auto* cmd=service->createCharacteristic(CMD_CHAR_UUID,
        NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_ENC);
    cmd->setCallbacks(new CommandCallbacks());
    service->start();
    auto* adv=NimBLEDevice::getAdvertising();
    adv->addServiceUUID(SERVICE_UUID); adv->start();
    resetFifo();
    Serial.printf("READY,%s,raw_hz=500,stream_hz=100,model=%s,PROVISIONAL\n",DEVICE_NAME,MODEL_ID);
  }
  void tick() {
    const uint32_t now=millis();
    motor.update(now); updateConnection(now);
    acquire(); processCommand(); transmit(); motor.update(millis());
    if(now-lastReport>=10000) {
      lastReport=now;
      Serial.printf("STATUS,gaps=%lu,tx_drops=%lu,eog_drops=%lu,notify_failures=%lu,cmd_drops=%lu,motor_drops=%lu\n",
        (unsigned long)fifoGaps,(unsigned long)txDrops,(unsigned long)eogDrops,
        (unsigned long)notifyFailures,(unsigned long)commandDrops.load(),(unsigned long)motorDrops);
    }
    delay(1);
  }
};
Firmware app;
} // namespace pipeline

void setup() { pipeline::app.begin(); }
void loop() { pipeline::app.tick(); }

