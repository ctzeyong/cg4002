#pragma once
#include "gesture_core.h"
#include "segments.h"
#include "imu.h"

// Called exactly once per packet, with the reading already selected by imuRead().
// Wall time detects interruptions; nominal time avoids treating ordinary loop jitter
// as a sensor gap. No dependency on BLE, Arduino, or motor handling.
namespace gesture {
struct StreamResult {
    GestureFlag flag=GESTURE_NONE;
    Result detector;
    SegmentEvent event;
    bool reset=false;
};
class StreamDetector {
    Detector detector;
    Segmenter segmenter;
    uint32_t previousMs=0, seq=0;
    int64_t timeUs=0;
    bool previous=false;
    GestureFlag lastFlag=GESTURE_NONE;
public:
    void reset() {
        detector.reset(); segmenter.reset(); previous=false;
        seq=0; timeUs=0; lastFlag=GESTURE_NONE;
    }
    StreamResult push(const int16_t sample[6], bool valid, uint32_t nowMs) {
        StreamResult r;
        // The sender restarts its 10 ms grid after a hold-up of two packet periods.
        if(!valid || (previous && uint32_t(nowMs-previousMs)>=20)) {
            reset(); r.reset=true;
        }
        if(!valid) return r;
        previous=true; previousMs=nowMs;
        r.detector=detector.push(seq++,timeUs,sample);
        if(r.detector.decisionReady)
            r.event=segmenter.step(r.detector.score,r.detector.energy,timeUs,0);
        if(segmenter.active) r.flag=GESTURE_ACTIVE;
        else if(r.event.kind==SEG_REJECT && lastFlag==GESTURE_ACTIVE)
            r.flag=GESTURE_REJECTED;
        lastFlag=r.flag;
        timeUs+=SAMPLE_PERIOD_US;
        return r;
    }
};
}
