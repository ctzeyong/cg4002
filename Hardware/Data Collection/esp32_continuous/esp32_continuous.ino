#include <Arduino.h>
#include <Wire.h>
#include <new>
#include "esp_timer.h"
#include "gesture_core.h"
#include "segments.h"

// USB recording is implemented. Bluetooth / gameplay transmission is NOT.
constexpr bool RECORD_MODE=true;
constexpr uint8_t MPU=0x68;
constexpr uint8_t MOTOR_PWM_PIN=2;
constexpr int FRAME_BYTES=12;  // FIFO: ax ay az gx gy gz, signed 16-bit.
gesture::Detector detector;
gesture::Segmenter segmenter;
// ESP32 has a smaller static-DRAM region than total RAM. Allocate gameplay
// storage from the heap at startup; recording mode needs no capture ring.
gesture::Capture* capture=nullptr;
constexpr int OUTPUT_STEPS=100; // Configure 32..512; this is a length, NOT a sample rate.
uint32_t sequence=0, fifoGaps=0, readErrors=0, saturatedSamples=0, maxDetectorUs=0;
int64_t sampleTime=0, lastSampleTime=-1, lastProgressUs=0;
bool haveClock=false;

bool writeReg(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(MPU); Wire.write(reg); Wire.write(value);
  return Wire.endTransmission(true)==0;
}
bool readBytes(uint8_t reg, uint8_t* data, uint8_t count) {
  Wire.beginTransmission(MPU); Wire.write(reg);
  if(Wire.endTransmission(false)!=0) return false;
  if(Wire.requestFrom(MPU,count,(uint8_t)true)!=count) return false;
  for(int i=0;i<count;++i) data[i]=Wire.read();
  return true;
}
bool restartFifo() {
  // Stop/reset FIFO, then enable acceleration + all three gyro axes.
  bool ok=writeReg(0x23,0) && writeReg(0x6A,0x04) &&
          writeReg(0x6A,0x40) && writeReg(0x23,0x78);
  haveClock=false;
  detector.reset();
  segmenter.reset();
  if(capture) capture->reset();
  ++sequence;  // Explicit discontinuity even when the number lost is unknown.
  lastProgressUs=esp_timer_get_time();
  return ok;
}
void fatal(const char* message) {
  Serial.println(message);
  while(true) delay(1000);
}
void reportGap(const char* reason) {
  ++fifoGaps;
  Serial.printf("GAP,%s,%lu\n",reason,(unsigned long)sequence);
  // An incomplete symbol is discarded; never bridge a sensor gap.
  if(!restartFifo()) fatal("ERROR,FIFO_RESET_FAILED");
}

void setup() {
  if(RECORD_MODE) {
    // Zero PWM duty: keep the motor off throughout data collection.
    pinMode(MOTOR_PWM_PIN,OUTPUT);
    digitalWrite(MOTOR_PWM_PIN,LOW);
  }
  Serial.begin(921600); delay(500);
  if(!RECORD_MODE) {
    capture=new(std::nothrow) gesture::Capture;
    if(!capture) fatal("ERROR,CAPTURE_MEMORY");
  }
  Wire.begin(21,22,400000); Wire.setTimeOut(10);
  uint8_t who;
  if(!readBytes(0x75,&who,1) || who!=0x68 || !writeReg(0x6B,1)) fatal("ERROR,MPU_NOT_FOUND");
  delay(100);
  // 1 kHz / (1+1) = 500 Hz for all six axes. DLPF ~=44 Hz accel / 42 Hz gyro.
  if(!writeReg(0x1A,3) || !writeReg(0x19,1) || !writeReg(0x1C,8) ||
     !writeReg(0x1B,8) || !writeReg(0x38,0x10) || !restartFifo()) fatal("ERROR,IMU_CONFIG");
  if(!RECORD_MODE && !MODEL_READY) fatal("ERROR,TRAIN_SELECT_EXPORT_FIRST");
  Serial.printf("READY,gesture-packet-v4,raw_hz=500,output_steps=%d,dlpf=3,model=%s\n",OUTPUT_STEPS,MODEL_ID);
}

void loop() {
  // Conversion is spread across loop iterations. Continue draining the sensor FIFO.
  if(!RECORD_MODE) {
    capture->service();
    if(capture->ready) {
      const auto& g=capture->output;
      Serial.printf("GESTURE_READY,%lld,%lld,%d,%d,%d\n",(long long)g.startUs,
        (long long)g.endUs,g.rawCount,g.steps,int(g.reason));
      // Integration point: g.frames contains the complete steps x 6 tensor.
      // Diagnostics consume it here. Comms must copy/use it before calling release().
      // No Bluetooth, byte packing, network sending, or transmission queue is implemented.
      capture->release();
    }
  }
  uint8_t status, bytes[2];
  if(!readBytes(0x3A,&status,1) || !readBytes(0x72,bytes,2)) {
    ++readErrors; reportGap("I2C_READ"); delay(5); return;
  }
  const int count=(int(bytes[0])<<8)|bytes[1];
  if((status&0x10) || count>=1024) { reportGap("FIFO_OVERFLOW"); return; }
  const int frames=count/FRAME_BYTES;
  if(!frames) {
    if(esp_timer_get_time()-lastProgressUs>100000) reportGap("NO_SAMPLES");
    delay(1); return;
  }
  if(!haveClock) {
    // Anchor once per continuous segment. USB/task jitter does not change sample intervals.
    sampleTime=esp_timer_get_time()-int64_t(frames-1)*gesture::RAW_PERIOD_US;
    if(sampleTime<=lastSampleTime) sampleTime=lastSampleTime+gesture::RAW_PERIOD_US;
    haveClock=true;
  }
  for(int k=0;k<frames;++k) {
    uint8_t raw[FRAME_BYTES];
    if(!readBytes(0x74,raw,FRAME_BYTES)) { ++readErrors; reportGap("FIFO_READ"); return; }
    float x[6]; int16_t counts[6]; bool clipped=false;
    for(int c=0;c<6;++c) {
      int16_t v=static_cast<int16_t>((uint16_t(raw[2*c])<<8)|raw[2*c+1]);
      counts[c]=v;
      x[c]=v/(c<3 ? 8192.f : 65.5f);
      if(v>=32700 || v<=-32700) clipped=true;
    }
    if(clipped) ++saturatedSamples; // Keep samples; clipping is reported, never hidden.
    if(!RECORD_MODE) capture->push(sampleTime,counts);
    int64_t began=esp_timer_get_time();
    gesture::Result result=detector.push(sequence,sampleTime,x);
    uint32_t elapsed=static_cast<uint32_t>(esp_timer_get_time()-began);
    if(elapsed>maxDetectorUs) maxDetectorUs=elapsed;
    if(RECORD_MODE) {
      // 500 Hz reduces USB text traffic; the laptop checks continuity and reports loss.
      Serial.printf("RAW500,%lu,%lld,%d,%d,%d,%d,%d,%d\n",
        (unsigned long)sequence,(long long)sampleTime,counts[0],counts[1],counts[2],counts[3],counts[4],counts[5]);
    } else if(result.decisionReady) {
      auto event=segmenter.step(result.score,result.energy,sampleTime,capture->oldestTime());
      if(event.kind==gesture::SEG_COMPLETE) capture->complete(event,OUTPUT_STEPS);
      Serial.printf("DECISION,%lld,%.6f,%.6f,%d\n",(long long)sampleTime,result.score,result.energy,int(event.kind));
    }
    lastSampleTime=sampleTime; sampleTime+=gesture::RAW_PERIOD_US; ++sequence;
    lastProgressUs=esp_timer_get_time();
  }
  static uint32_t lastReport=0;
  if(millis()-lastReport>=10000) {
    lastReport=millis();
    Serial.printf("STATUS,gaps=%lu,read_errors=%lu,clipped=%lu,max_detector_us=%lu,conversion_drops=%lu\n",
      (unsigned long)fifoGaps,(unsigned long)readErrors,(unsigned long)saturatedSamples,(unsigned long)maxDetectorUs,(unsigned long)(capture ? capture->rejected : 0));
  }
}
