#pragma once
#include <cmath>
#include <cstdint>
#include <cstring>
#include "model_data.h"

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
