"""On-the-fly augmentation, all in one place so what a run applies is legible.

Three layers, applied in this order to each gesture:
  1. signal augs (toggled by AugConfig): boundary -> rotate -> amp -> mag-warp
     -> time-warp + resample to GEST -> bias -> jitter
  2. feature channels (only when channels == 10): raw 6 + |accel|, |gyro|, |d accel|, |d gyro|
  3. window synthesis (always on): lay the gesture into a WIN-step filler window at a
     random offset  (the detection-in-window domain randomisation)

AugConfig.resample picks the gesture length. True: every gesture is resampled to GEST steps
and placed at a random offset. False (native): the gesture keeps its 100 Hz length (time-warp
only varies its speed) and slides anywhere across the window, from slide_max_out of it cut off
the left edge to the same cut off the right edge (slide_range), filler on both sides.

Everything operates on SI units (m/s^2, rad/s); accel = cols 0:3, gyro = cols 3:6.
"""
import math

import numpy as np

from .config import WIN, GEST, CROP, AugConfig

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


def _apply_signal(si, rng, cfg: AugConfig):
    """Augment a variable-length SI gesture (all but the final time-warp+resample)."""
    x = si.copy()
    if cfg.boundary and rng.random() < cfg.bnd_p and len(x) > 10:
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
    if cfg.bias and rng.random() < cfg.bias_p:
        g = g + rng.normal(0, cfg.bias_frac * ch_std, size=(1, NCH_RAW))
    if cfg.jitter and rng.random() < cfg.jit_p:
        g = g + rng.normal(0, cfg.jit_frac * ch_std, size=g.shape)
    return g.astype(np.float32)


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
def build_filler(pool, rng, length, ch_std, noise_frac):
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


def make_window(gesture, pool, rng, ch_std, cfg: AugConfig, offset=None):
    """Lay a gesture into a WIN-step filler window at a random (or given) start offset."""
    n = len(gesture)
    win = build_filler(pool, rng, WIN, ch_std, cfg.noise_frac)
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


def make_fragment(gesture, pool, rng, ch_std, cfg: AugConfig, vis, where="head"):
    """Window with exactly `vis` of the gesture's steps visible (capped at its length), at a
    random position, the rest filler. `where` = head|tail|mid. vis=0 -> pure filler (leakage probe)."""
    win = build_filler(pool, rng, WIN, ch_std, cfg.noise_frac)
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
