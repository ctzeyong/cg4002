"""On-the-fly augmentation, all in one place so what a run applies is legible.

Three layers, applied in this order to each gesture:
  1. signal augs (toggled by AugConfig): boundary -> rotate -> amp -> mag-warp
     -> time-warp + resample to GEST -> bias -> jitter
  2. feature channels (only when channels == 10): raw 6 + |accel|, |gyro|, |d accel|, |d gyro|
  3. window synthesis (always on): lay the gesture into a WIN-step filler window at a
     random offset  (the detection-in-window domain randomisation). The filler is chunks of
     other gestures, or (AugConfig.filler="background") a slice of real recorded background

AugConfig.resample picks the gesture length. True: every gesture is resampled to GEST steps
and placed at a random offset. False (native): the gesture keeps its 100 Hz length (time-warp
only varies its speed) and slides anywhere across the window, from slide_max_out of it cut off
the left edge to the same cut off the right edge (slide_range), filler on both sides.

noise_window builds the 8th-class (DataConfig.noise) windows: real background only, no gesture.

Everything operates on SI units (m/s^2, rad/s); accel = cols 0:3, gyro = cols 3:6.
"""
import math

import numpy as np

from .config import WIN, GEST, CROP, NOISE_SPAN, AugConfig

NCH_RAW = 6


# --------------------------------------------------------------------------- signal augs
def _rot_matrix(rng, max_deg):
    ax, ay, az = np.radians(rng.uniform(-max_deg, max_deg, size=3))
    cx, sx = math.cos(ax), math.sin(ax); cy, sy = math.cos(ay), math.sin(ay)
    cz, sz = math.cos(az), math.sin(az)
    rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return rz @ ry @ rx


def _curve(rng, n, knots, sigma, center=0.0):
    kx = np.linspace(0, 1, knots)
    kv = rng.normal(center, sigma, size=knots)
    return np.interp(np.linspace(0, 1, n), kx, kv)


def _apply_signal(si, rng, cfg: AugConfig, trim=True):
    """Augment a variable-length SI gesture (all but the final time-warp+resample).
    trim=False skips the boundary trim (a noise window keeps its length)."""
    x = si.copy()
    if trim and cfg.boundary and rng.random() < cfg.bnd_p and len(x) > 10:
        a = int(len(x) * rng.uniform(0, cfg.bnd_max_frac))
        b = int(len(x) * rng.uniform(0, cfg.bnd_max_frac))
        if len(x) - a - b >= 5:
            x = x[a:len(x) - b]
    if cfg.rotate and rng.random() < cfg.rot_p:
        r = _rot_matrix(rng, cfg.rot_max_deg)
        x[:, 0:3] = x[:, 0:3] @ r.T
        x[:, 3:6] = x[:, 3:6] @ r.T
    if cfg.amp and rng.random() < cfg.amp_p:
        x[:, 0:3] *= rng.uniform(cfg.amp_lo, cfg.amp_hi)
        x[:, 3:6] *= rng.uniform(cfg.amp_lo, cfg.amp_hi)
    if cfg.magwarp and rng.random() < cfg.mw_p:
        for c in range(NCH_RAW):
            x[:, c] *= (1.0 + _curve(rng, len(x), cfg.mw_knots, cfg.mw_sigma))
    return x


def _timewarp_resample(x, out_len, rng, cfg: AugConfig, warp):
    """Resample (T,6) -> (out_len,6); non-uniform (local speed variation) if warp else uniform."""
    t = len(x)
    if warp and cfg.timewarp:
        speed = np.exp(_curve(rng, out_len, cfg.tw_knots, cfg.tw_sigma))
        pos = np.cumsum(speed); pos = (pos - pos[0]) / (pos[-1] - pos[0]) * (t - 1)
    else:
        pos = np.linspace(0, t - 1, out_len)
    src = np.arange(t)
    return np.stack([np.interp(pos, src, x[:, c]) for c in range(NCH_RAW)], axis=1)


def to_gesture(si, rng, cfg: AugConfig, ch_std, augment):
    """Variable-length SI gesture -> a GEST-step gesture (cfg.resample) or a native-length
    one, augmented if requested."""
    if not augment:
        if not cfg.resample:
            return si.astype(np.float32)
        return _timewarp_resample(si, GEST, rng, cfg, warp=False).astype(np.float32)
    g = _apply_signal(si, rng, cfg)
    if cfg.resample:
        g = _timewarp_resample(g, GEST, rng, cfg, warp=True)
    elif cfg.timewarp:
        n = max(2, round(len(g) * rng.uniform(cfg.speed_lo, cfg.speed_hi)))
        g = _timewarp_resample(g, n, rng, cfg, warp=True)
    return _apply_offsets(g, rng, cfg, ch_std).astype(np.float32)


def _apply_offsets(g, rng, cfg: AugConfig, ch_std):
    """The last signal augs: constant bias, then per-sample jitter (both scaled by ch_std)."""
    if cfg.bias and rng.random() < cfg.bias_p:
        g = g + rng.normal(0, cfg.bias_frac * ch_std, size=(1, NCH_RAW))
    if cfg.jitter and rng.random() < cfg.jit_p:
        g = g + rng.normal(0, cfg.jit_frac * ch_std, size=g.shape)
    return g


# --------------------------------------------------------------------------- noise class
def _bg_slice(bg, rng, n):
    """A random contiguous n-step slice of one background block (a block >= n steps long;
    n shrinks to the longest block if none is)."""
    fits = [b for b in bg if len(b) >= n]
    if not fits:
        fits = [max(bg, key=len)]
        n = len(fits[0])
    b = fits[rng.integers(len(fits))]
    start = int(rng.integers(0, len(b) - n + 1))
    return np.asarray(b[start:start + n], np.float64)


def noise_window(bg, pool, rng, ch_std, cfg: AugConfig, augment):
    """A WIN-step window with no gesture in it, for the noise class, made of real background
    (`bg` = that split's background blocks) and augmented like a gesture when requested.

    Native: one contiguous slice (time-warp varies its speed, then it is resampled back to WIN),
    so there are no seams. Resampled: a NOISE_SPAN-length background span goes through exactly
    the gesture path (to_gesture -> GEST steps -> make_window on background filler), as the live
    --resample path would present a flagged non-gesture."""
    if cfg.resample:
        span = _bg_slice(bg, rng, int(rng.integers(NOISE_SPAN[0], NOISE_SPAN[1] + 1)))
        return make_window(to_gesture(span, rng, cfg, ch_std, augment), pool, rng, ch_std, cfg, None, bg)
    n = round(WIN * rng.uniform(cfg.speed_lo, cfg.speed_hi)) if augment and cfg.timewarp else WIN
    x = _bg_slice(bg, rng, n)
    if augment:
        x = _apply_signal(x, rng, cfg, trim=False)
        if cfg.timewarp:
            x = _timewarp_resample(x, WIN, rng, cfg, warp=True)
        x = _apply_offsets(x, rng, cfg, ch_std)
    x = _timewarp_resample(x, WIN, rng, cfg, warp=False) if len(x) != WIN else x
    x = x + rng.standard_normal(x.shape) * (cfg.noise_frac * ch_std)      # as the filler gets
    return x.astype(np.float32)


# --------------------------------------------------------------------------- feature channels
def add_features(w):
    """(T,6) -> (T,10): raw + accel mag, gyro mag, |d accel| (jerk), |d gyro| (corners).
    Per-step gradients keep magnitudes in the ap_fixed<16,6> range; clip as a guard."""
    a, g = w[:, 0:3], w[:, 3:6]
    a_mag = np.linalg.norm(a, axis=-1, keepdims=True)
    g_mag = np.linalg.norm(g, axis=-1, keepdims=True)
    a_jerk = np.linalg.norm(np.gradient(a, axis=0), axis=-1, keepdims=True)
    g_ang = np.linalg.norm(np.gradient(g, axis=0), axis=-1, keepdims=True)
    out = np.concatenate([w, a_mag, g_mag, a_jerk, g_ang], axis=-1)
    return np.clip(out, -63.9, 63.9).astype(np.float32)


# --------------------------------------------------------------------------- window synthesis
def build_filler(pool, rng, length, ch_std, noise_frac, bg=None):
    """`length` steps of filler + noise_frac Gaussian noise. With `bg` (background blocks, each
    >= WIN steps): one contiguous slice of real recorded background; else 5-24-step chunks of
    the pool's gestures."""
    if bg is not None:
        b = bg[rng.integers(len(bg))]
        start = int(rng.integers(0, len(b) - length + 1))
        out = np.array(b[start:start + length], np.float32)
        out += rng.standard_normal(out.shape).astype(np.float32) * (noise_frac * ch_std)
        return out
    out = np.empty((length, NCH_RAW), np.float32)
    pos = 0
    while pos < length:
        g = pool[rng.integers(len(pool))]
        k = min(int(rng.integers(5, 25)), len(g))      # a native gesture may be short
        start = int(rng.integers(0, len(g) - k + 1))
        take = min(k, length - pos)
        out[pos:pos + take] = g[start:start + take]
        pos += take
    out += rng.standard_normal(out.shape).astype(np.float32) * (noise_frac * ch_std)
    return out


def ref_offset(n):
    """Start index of an n-step gesture centred in the window (cut evenly on both sides when
    longer than WIN). The ceiling position; sweeps shift from here."""
    return (WIN - n) // 2


def slide_range(n, cfg: AugConfig):
    """(first, last) start offset that keeps >= 1 - slide_max_out of an n-step gesture inside
    the window: first = head cut off the left edge, last = tail cut off the right edge. The
    visible part is capped at WIN for a gesture longer than the window can show."""
    vis = min(math.ceil((1 - cfg.slide_max_out) * n), n, WIN)
    return vis - n, WIN - vis


def make_window(gesture, pool, rng, ch_std, cfg: AugConfig, offset=None, bg=None):
    """Lay a gesture into a WIN-step filler window at a random (or given) start offset.
    `bg` = real-background filler blocks (see build_filler)."""
    n = len(gesture)
    win = build_filler(pool, rng, WIN, ch_std, cfg.noise_frac, bg)
    if offset is None:
        if cfg.resample:
            offset = int(rng.integers(-CROP, (WIN - GEST) + CROP + 1))
        else:
            first, last = slide_range(n, cfg)
            offset = int(rng.integers(first, last + 1))
    lo, hi = max(offset, 0), min(offset + n, WIN)
    if hi > lo:
        win[lo:hi] = gesture[lo - offset:hi - offset]
    return win


def make_fragment(gesture, pool, rng, ch_std, cfg: AugConfig, vis, where="head", bg=None):
    """Window with exactly `vis` of the gesture's steps visible (capped at its length), at a
    random position, the rest filler. `where` = head|tail|mid. vis=0 -> pure filler (leakage probe)."""
    win = build_filler(pool, rng, WIN, ch_std, cfg.noise_frac, bg)
    n = len(gesture)
    vis = min(vis, n, WIN)
    if vis > 0:
        if where == "tail":
            frag = gesture[n - vis:]
        elif where == "mid":
            s = (n - vis) // 2
            frag = gesture[s:s + vis]
        else:
            frag = gesture[:vis]
        start = int(rng.integers(0, WIN - vis + 1))
        win[start:start + vis] = frag
    return win
