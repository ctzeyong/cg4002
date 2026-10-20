"""Manual video/sensor alignment with optional linear drift correction."""
from dataclasses import dataclass
import math


@dataclass
class VideoSync:
    # video_seconds = offset + rate * sensor_seconds_from_first_sample
    offset: float = 0.
    rate: float = 1.

    def __post_init__(self):
        if not math.isfinite(self.offset) or not math.isfinite(self.rate) or self.rate <= 0:
            raise ValueError('Alignment must have a finite offset and positive rate')

    def video_time(self, sensor_seconds): return self.offset+self.rate*sensor_seconds
    def sensor_time(self, video_seconds): return (video_seconds-self.offset)/self.rate

    @classmethod
    def from_points(cls, points):
        if len(points) not in (1,2): raise ValueError('Use one or two synchronization points')
        s0,v0=points[0]
        if len(points)==1: return cls(v0-s0)
        s1,v1=points[1]
        if s1<=s0 or v1<=v0: raise ValueError('Second synchronization point must be later in both timelines')
        rate=(v1-v0)/(s1-s0)
        return cls(v0-rate*s0,rate)


class VideoReader:
    def __init__(self,path):
        try: import cv2
        except ImportError as e: raise RuntimeError('Video needs opencv-python; install requirements.txt') from e
        self.cv2=cv2
        self.capture=cv2.VideoCapture(str(path))
        self.fps=self.capture.get(cv2.CAP_PROP_FPS)
        self.count=int(self.capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if not self.capture.isOpened() or not math.isfinite(self.fps) or self.fps<=0 or self.count<1:
            self.capture.release()
            raise ValueError('Cannot decode video or read its frame rate; try a constant-frame-rate MP4')
        self.duration=(self.count-1)/self.fps

    def frame(self,seconds):
        index=min(self.count-1,max(0,round(seconds*self.fps)))
        self.capture.set(self.cv2.CAP_PROP_POS_FRAMES,index)
        ok,frame=self.capture.read()
        if not ok: raise ValueError(f'Could not decode video frame {index}')
        h,w=frame.shape[:2]
        if w>800: frame=self.cv2.resize(frame,(800,round(h*800/w)))
        return self.cv2.cvtColor(frame,self.cv2.COLOR_BGR2RGB),index/self.fps

    def close(self): self.capture.release()
