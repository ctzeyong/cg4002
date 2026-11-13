#include <iostream>
#include <iomanip>
#include <cassert>
#include "../stream_detector.h"

static void checks() {
    gesture::Frontend front;
    float f[42];
    int16_t x[6]={-32768,32767,-32768,32767,-32768,32767};
    for(int i=0;i<30;++i) {
        auto r=front.push(i,int64_t(i)*10000,x,f);
        assert(r.decisionReady==(i==29));
    }
    for(int c=0;c<6;++c) { assert(f[c*7]==x[c]); assert(f[c*7+1]==0); }
    auto gap=front.push(31,310000,x,f);
    assert(gap.gap && !gap.decisionReady);
    front.reset();
    for(int i=0;i<30;++i) {
        for(int c=0;c<6;++c) x[c]=i%2 ? 32767 : -32768;
        front.push(i,int64_t(i)*10000,x,f);
    }
    for(int c=0;c<6;++c) {
        assert(std::isfinite(f[c*7+1])); assert(f[c*7+2]==65535);
        assert(f[c*7+3]==65535);
    }
    gesture::StreamDetector stream;
    uint32_t now=0xfffffff0u;
    for(int i=0;i<30;++i,now+=10) assert(stream.push(x,true,now).detector.decisionReady==(i==29));
    auto bad=stream.push(x,false,now); assert(bad.reset && bad.flag==GESTURE_NONE);
    assert(!stream.push(x,true,now+10).detector.decisionReady);
    assert(stream.push(x,true,now+40).reset);
}

int main() {
    checks();
    gesture::StreamDetector stream;
    gesture::Frontend front;
    uint32_t now, previous=0, seq=0;
    int valid;
    bool had=false;
    int64_t time=0;
    int16_t x[6];
    std::cout << std::setprecision(10);
    while(std::cin >> now >> valid) {
        for(int c=0;c<6;++c) { int v; std::cin >> v; x[c]=int16_t(v); }
        auto r=stream.push(x,valid!=0,now);
        if(!valid || (had && uint32_t(now-previous)>=20)) {
            front.reset(); seq=0; time=0; had=false;
        }
        float f[42]={};
        if(valid) {
            front.push(seq++,time,x,f); time+=10000;
            previous=now; had=true;
        }
        std::cout << int(r.flag) << ' ' << int(r.event.kind) << ' ' << r.detector.decisionReady
                  << ' ' << r.detector.score << ' ' << r.detector.energy;
        for(float value:f) std::cout << ' ' << value;
        std::cout << '\n';
    }
}
