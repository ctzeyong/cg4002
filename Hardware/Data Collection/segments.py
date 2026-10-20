"""One-symbol candidate segmentation and deterministic N-step resampling.

No transport or packet serialization. End times are exclusive throughout.
"""
from collections import deque
import math
import numpy as np
from core import DEFAULTS, validate_config, RAW_PERIOD_US

PRE_US = 300000
PAUSE_US = 150000
MIN_US = 250000
MAX_US = 6000000
DIP_RATIO = .20
ENERGY_FLOOR = .05
MIN_STEPS, MAX_STEPS = 32, 512
PROCESSING_ID = 'hann-sinc4-v1'


class Segmenter:
    def __init__(self, config=None):
        self.cfg = validate_config(dict(DEFAULTS, **(config or {})))
        self.reset()

    def reset(self):
        self.active = False
        self.start = self.trigger = 0
        self.dip_since = self.low_since = None
        self.energies = deque(maxlen=10)
        self.locked = False
        self.rearm_level = ENERGY_FLOOR
        self.resume = 0
        self.previous_end = -1
        self.timeout_lock = False
        self.unlock_since = None

    def step(self, score, r_energy, time_us, available_start=0):
        if not np.isfinite(score) or not np.isfinite(r_energy):
            self.dip_since = self.low_since = None
            return None
        if not self.active:
            if self.timeout_lock:
                if score < self.cfg['stop_threshold'] or r_energy <= self.rearm_level:
                    if self.unlock_since is None: self.unlock_since = time_us
                    if time_us-self.unlock_since >= PAUSE_US: self.timeout_lock = False
                else:
                    self.unlock_since = None
                return None
            if self.locked:
                if score < self.cfg['stop_threshold']:
                    self.locked = False
                elif r_energy > self.rearm_level*2:
                    self.resume += 1
                    if self.resume >= 2:
                        self.locked = False
                else:
                    self.resume = 0
                if self.locked:
                    return None
            if score >= self.cfg['start_threshold']:
                self.active = True
                self.start = max(available_start, self.previous_end, time_us-PRE_US)
                self.trigger = time_us
                self.energies.clear()
                self.energies.append(r_energy)
                return dict(kind='START', time_us=time_us, start_us=self.start, score=score)
            return None
        peak = max(self.energies, default=r_energy)
        self.energies.append(r_energy)
        threshold = max(ENERGY_FLOOR, peak*DIP_RATIO)
        # The reference is frozen when a dip begins, so it cannot decay away mid-confirmation.
        if self.dip_since is None:
            if r_energy <= threshold:
                self.dip_since = time_us
                self.rearm_level = threshold
        elif r_energy > self.rearm_level:
            self.dip_since = None
        if score < self.cfg['stop_threshold']:
            if self.low_since is None:
                self.low_since = time_us
        else:
            self.low_since = None
        end, reason = None, None
        if self.dip_since is not None and time_us-self.dip_since >= PAUSE_US:
            end, reason = self.dip_since+RAW_PERIOD_US, 'motion_dip'
        elif self.low_since is not None and time_us-self.low_since >= self.cfg['stop_hold_us']:
            end, reason = self.low_since+RAW_PERIOD_US, 'nongesture'
        elif time_us-self.start >= MAX_US:
            reason = 'timeout'
        if reason is None:
            return None
        self.active = False
        self.locked = True
        self.resume = 0
        self.dip_since = self.low_since = None
        self.previous_end = end if end is not None else time_us+RAW_PERIOD_US
        if reason == 'timeout':
            self.timeout_lock = True
            self.unlock_since = None
            self.rearm_level = threshold
        if end is None or not MIN_US <= end-self.start <= MAX_US:
            return dict(kind='REJECT', time_us=time_us, reason=reason)
        return dict(kind='COMPLETE', time_us=time_us, start_us=self.start, end_us=end,
                    trigger_us=self.trigger, reason=reason,
                    pretrigger_us=self.trigger-self.start)


def resample_steps(raw, steps=100):
    """Lossy time normalization, not byte compression. Same kernel as C++.

    Downsample with normalized Hann-windowed sinc interpolation and edge replication.
    Cutoff is 90% of target Nyquist, support +/-4 input/output ratios.
    Upsampling uses linear interpolation and adds no measured information.
    """
    raw = np.asarray(raw, dtype=np.float64)
    if raw.ndim != 2 or raw.shape[1] != 6 or len(raw) < 2 or not np.isfinite(raw).all():
        raise ValueError('Need at least two finite six-channel samples')
    if isinstance(steps, bool) or not isinstance(steps, (int, np.integer)) or not MIN_STEPS <= steps <= MAX_STEPS:
        raise ValueError('Output steps must be an integer from 32 to 512')
    positions = np.linspace(0, len(raw)-1, steps)
    if steps >= len(raw):
        return np.stack([np.interp(positions, np.arange(len(raw)), raw[:, c]) for c in range(6)],axis=1).astype('float32')
    ratio = (len(raw)-1)/(steps-1)
    radius = math.ceil(4*ratio)
    cutoff = .45/ratio
    output = np.empty((steps,6), dtype=np.float32)
    for j, pos in enumerate(positions):
        indices = np.arange(math.ceil(pos-radius), math.floor(pos+radius)+1)
        distance = indices-pos
        weights = np.sinc(2*cutoff*distance)*(.5+.5*np.cos(np.pi*distance/radius))
        weights /= weights.sum()
        output[j] = weights @ raw[np.clip(indices, 0, len(raw)-1)]
    return output
