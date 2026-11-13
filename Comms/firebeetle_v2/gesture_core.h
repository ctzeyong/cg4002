#pragma once
#include <cmath>
#include <cstdint>
#include <cstring>
#include "model_data.h"

// Portable detector only. No Bluetooth, packet serialization or transport queue.
namespace gesture {
constexpr int RAW_HZ=500, RAW_PERIOD_US=2000;
// RAW_* remain the sensor/capture grid; classification uses selected BLE samples.
constexpr int SAMPLE_HZ=100, SAMPLE_PERIOD_US=10000;
constexpr int WINDOW=30, HOP=5, BLOCK=10, ENERGY_WINDOW=5, N_FEATURES=42;
static_assert(MODEL_SAMPLE_HZ==SAMPLE_HZ,"Retrain for 100 Hz selected raw counts");
static_assert(MODEL_COUNTS_INT16,"Model must use raw count features");
enum Action { NONE=0, START=1, STOP=2, GAP=3 };

struct Result {
  bool gap=false, sampleReady=false, decisionReady=false, useful=false;
  int64_t sampleTimeUs=0;
  float sample[6]={}, score=0, energy=0;
  Action action=NONE;
};

class Frontend {
  int16_t raw[WINDOW][6]={};
  int head=-1, count=0, hop=0;
  bool previous=false;
  uint32_t previousSeq=0;
  int64_t previousTime=0;
public:
  void reset() { head=-1; count=0; hop=0; previous=false; }
  void summarize(float* out) const {
    for(int c=0;c<6;++c) {
      // Double accumulators represent these integer sums exactly, including int16 extremes.
      double sum=0, square=0, lo=1e30, hi=-1e30, difference=0, last=0, block[3]={};
      for(int j=0;j<WINDOW;++j) {
        double v=raw[(head+1+j)%WINDOW][c];
        sum+=v; square+=v*v; if(v<lo) lo=v; if(v>hi) hi=v;
        if(j>0) difference+=std::fabs(v-last);
        last=v; block[j/BLOCK]+=v;
      }
      const double mean=sum/WINDOW, variance=square/WINDOW-mean*mean;
      out[c*7]=mean;
      out[c*7+1]=std::sqrt(variance>0 ? variance : 0);
      out[c*7+2]=hi-lo;
      out[c*7+3]=difference/(WINDOW-1);
      for(int j=0;j<3;++j) out[c*7+4+j]=block[j]/BLOCK;
    }
  }
  Result push(uint32_t seq, int64_t time, const int16_t* x, float* features) {
    Result r;
    r.gap=previous && (seq!=previousSeq+1 || time-previousTime!=SAMPLE_PERIOD_US);
    if(r.gap) reset();
    previous=true; previousSeq=seq; previousTime=time;
    head=(head+1)%WINDOW;
    for(int c=0;c<6;++c) raw[head][c]=x[c];
    if(count<WINDOW) ++count;
    r.sampleReady=true; r.sampleTimeUs=time;
    for(int c=0;c<6;++c) r.sample[c]=raw[head][c];
    if(count==WINDOW) {
      if(hop==0) {
        summarize(features); r.decisionReady=true;
        for(int c=0;c<6;++c) {
          double sum=0, square=0;
          for(int j=0;j<ENERGY_WINDOW;++j) { double v=raw[(head-j+WINDOW)%WINDOW][c]; sum+=v; square+=v*v; }
          // Preserve the segmenter's dimensionless energy without converting each sample.
          if(c<3) { double variance=square/ENERGY_WINDOW-(sum/ENERGY_WINDOW)*(sum/ENERGY_WINDOW); r.energy+=float((variance>0 ? variance : 0)/(8192.0*8192.0*.01)); }
          else r.energy+=float(square/ENERGY_WINDOW/(65.5*65.5*400));
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
  Result push(uint32_t seq, int64_t time, const int16_t* x) {
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
