"""Optional live preprocessing for the --resample path.

Turns a detected gesture's raw int16 counts into the exact representation the
model was trained on, so a live window equals a training example built from the
same samples. Enabled only by ``tcp_client_mqtt.py --resample``; ``gesture.py``
imports this module lazily, so the default pipeline never loads numpy through
here. Delete this file and the resample branch in ``gesture.py`` to remove the
feature.

Two functions are copied UNCHANGED from the hardware data-collection tool so the
numbers match byte-for-byte; keep them identical and let the parity test in
``laptop/tests/test_gesture.py`` guard against drift:

  * ``resample_steps``  <- Hardware/Data Collection/segments.py    (``hann-sinc4-v1``)
  * ``quantize_counts`` <- Hardware/Data Collection/cloud_export.py (``imu-counts-int16-v1``)

The training chain for one gesture is::

    raw (g, deg/s) -> resample_steps(.., GEST) -> quantize_counts (int16 counts)
                   -> decode to m/s^2 & rad/s -> centre in a WIN-step window

The live firmware sends counts, so ``gesture_window`` divides them back to
g/deg-s first, then runs the identical chain.
"""
import math

import numpy as np

# --- model window geometry (matches AI/board/wk10_integration.py) -----------
WIN = 150            # model window, in steps
GEST = 100           # gesture length after resampling, in steps
CHANNELS = 6
PAD = (WIN - GEST) // 2   # 25: centre the gesture (offset 0 = the training-like case)

# counts <-> units contract (firebeetle/imu.h and AI/board/wk10_integration.py)
COUNT_SCALES = np.array([8192, 8192, 8192, 65.5, 65.5, 65.5], dtype=np.float64)
ACCEL_TO_M_S2 = 0.001197100830078125       # = 9.80665 / 8192
GYRO_TO_RAD_S = 0.00026646248122050834     # = (pi/180) / 65.5

# --- vendored from Hardware/Data Collection (keep byte-identical) -----------
MIN_STEPS, MAX_STEPS = 32, 512
PROCESSING_ID = 'hann-sinc4-v1'            # segments.PROCESSING_ID
ENCODING_ID = 'imu-counts-int16-v1'        # cloud_export.ENCODING_ID


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


def quantize_counts(frames):
    """Quantize AFTER resampling: nearest, ties away from zero, then saturate.

    Scale in float64 to make the rounding contract explicit for transport code.
    Returned arrays can be serialized in row-major order as little-endian int16.
    """
    values = np.asarray(frames, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 6 or not np.isfinite(values).all():
        raise ValueError('Expected finite frames with shape (steps, 6)')
    counts = values * COUNT_SCALES
    rounded = np.copysign(np.floor(np.abs(counts) + 0.5), counts)
    clipped_values = int(np.count_nonzero((rounded < -32768) | (rounded > 32767)))
    return np.clip(rounded, -32768, 32767).astype('<i2'), clipped_values


# --- comms glue -------------------------------------------------------------
def gesture_window(counts_span):
    """Turn a detected gesture's raw counts into the model's (WIN, CHANNELS) window.

    ``counts_span`` is a sequence of six-channel int16 count rows
    ``[ax, ay, az, gx, gy, gz]`` -- the flagged span, in packet order. Returns
    ``(window, clipped)`` where ``window`` is float32 physical units (m/s^2, rad/s)
    with the resampled gesture centred in ``WIN`` steps of zero filler, and
    ``clipped`` is how many resampled counts saturated int16.

    Reproduces training exactly: counts -> g/deg-s, ``resample_steps`` to ``GEST``,
    ``quantize_counts`` to int16, decode to physical units (the same float64->float32
    decode ``wk10_integration`` applies), then centre at offset 0.
    """
    gdps = np.asarray(counts_span, dtype=np.float64) / COUNT_SCALES   # counts -> g, deg/s
    counts, clipped = quantize_counts(resample_steps(gdps, GEST))     # GEST x 6 int16 counts
    c = counts.astype(np.float64)
    phys = np.empty((GEST, CHANNELS), dtype=np.float32)
    phys[:, 0:3] = c[:, 0:3] * ACCEL_TO_M_S2     # -> m/s^2
    phys[:, 3:6] = c[:, 3:6] * GYRO_TO_RAD_S     # -> rad/s
    window = np.zeros((WIN, CHANNELS), dtype=np.float32)
    window[PAD:PAD + GEST] = phys
    return window, clipped
