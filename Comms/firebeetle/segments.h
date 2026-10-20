#pragma once
#include "gesture_core.h"

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

// Same Hann-windowed sinc kernel as Python. Reader returns a scaled channel value.
// Pure preprocessing: no serialization, packet framing, Bluetooth or network code.
template<class Reader>
inline void resampleFrame(Reader read, int count, int steps, int j, float* out) {
  const double pos=double(j)*(count-1)/(steps-1);
  for(int c=0;c<6;++c) out[c]=0;
  if(steps>=count) {
    int lo=int(std::floor(pos)), hi=lo+1; if(hi>=count) hi=count-1;
    const double f=pos-lo;
    for(int c=0;c<6;++c) out[c]=float((1-f)*read(lo,c)+f*read(hi,c));
    return;
  }
  constexpr double kernelPi=3.14159265358979323846;
  const double ratio=double(count-1)/(steps-1), cutoff=.45/ratio;
  const int radius=int(std::ceil(4*ratio));
  double sum=0, values[6]={};
  for(int i=int(std::ceil(pos-radius)); i<=int(std::floor(pos+radius)); ++i) {
    const double d=i-pos, angle=2*kernelPi*cutoff*d;
    const double sinc=std::fabs(angle)<1e-12 ? 1 : std::sin(angle)/angle;
    const double w=sinc*(.5+.5*std::cos(kernelPi*d/radius));
    const int k=i<0 ? 0 : (i>=count ? count-1 : i);
    sum+=w;
    for(int c=0;c<6;++c) values[c]+=w*read(k,c);
  }
  for(int c=0;c<6;++c) out[c]=float(values[c]/sum);
}

constexpr int CAPTURE_CAPACITY=4096, MAX_STEPS=512, DEFAULT_STEPS=100;
struct NormalizedGesture {
  int steps=0, rawCount=0;
  int64_t startUs=0, endUs=0, triggerUs=0, detectedUs=0;
  EndReason reason=MOTION_DIP;
  float frames[MAX_STEPS][6]={};
};

class Capture {
  int16_t raw[CAPTURE_CAPACITY][6]={}; // 48 KiB, retains 8.192 seconds at 500 Hz.
  int64_t base=0, total=0, jobFirst=0;
  int frame=0;
  bool pending=false;
public:
  NormalizedGesture output;
  bool ready=false;
  uint32_t rejected=0;
  void reset() { if(pending || ready) ++rejected; total=0; pending=ready=false; }
  int64_t oldestTime() const { return base+(total>CAPTURE_CAPACITY ? total-CAPTURE_CAPACITY : 0)*RAW_PERIOD_US; }
  void push(int64_t time, const int16_t* x) {
    if(total && time!=base+total*RAW_PERIOD_US) reset();
    if(!total) base=time;
    for(int c=0;c<6;++c) raw[total%CAPTURE_CAPACITY][c]=x[c];
    ++total;
  }
  bool complete(const SegmentEvent& e, int steps=DEFAULT_STEPS) {
    if(pending || ready || steps<32 || steps>MAX_STEPS || e.kind!=SEG_COMPLETE ||
       e.startUs<oldestTime() || e.endUs>base+total*RAW_PERIOD_US || e.endUs-e.startUs<MIN_US ||
       e.endUs-e.startUs>MAX_US || (e.startUs-base)%RAW_PERIOD_US || (e.endUs-base)%RAW_PERIOD_US) {
      ++rejected; return false;
    }
    output.steps=steps; output.startUs=e.startUs; output.endUs=e.endUs;
    output.triggerUs=e.triggerUs; output.detectedUs=e.emittedUs; output.reason=e.reason;
    output.rawCount=int((e.endUs-e.startUs)/RAW_PERIOD_US);
    jobFirst=(e.startUs-base)/RAW_PERIOD_US; frame=0; pending=true;
    return true;
  }
  // Call between FIFO drains, once per loop. Produces ONE frame per call.
  void service() {
    if(!pending) return;
    if(total-jobFirst>CAPTURE_CAPACITY) { pending=false; ++rejected; return; }
    auto reader=[this](int i,int c) -> double {
      return raw[(jobFirst+i)%CAPTURE_CAPACITY][c]/(c<3 ? 8192.0 : 65.5);
    };
    resampleFrame(reader,output.rawCount,output.steps,frame,output.frames[frame]);
    if(++frame==output.steps) { pending=false; ready=true; }
  }
  // Consumer must copy/use the completed tensor before release; this is NOT a sender.
  void release() { ready=false; }
};
}
