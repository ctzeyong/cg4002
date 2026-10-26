"""On-the-fly augmentation, all in one place so what a run applies is legible.

Three layers, applied in this order to each gesture:
  1. signal augs (toggled by AugConfig): boundary -> rotate -> amp -> mag-warp
     -> time-warp + resample to GEST -> bias -> jitter
  2. feature channels (only when channels == 10): raw 6 + |accel|, |gyro|, |d accel|, |d gyro|
  3. window synthesis (always on): lay the gesture into a WIN-step filler window at a
     random offset  (the detection-in-window domain randomisation)

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
    """Variable-length SI gesture -> a GEST-step gesture, augmented if requested."""
    if not augment:
        return _timewarp_resample(si, GEST, rng, cfg, warp=False).astype(np.float32)
    g = _apply_signal(si, rng, cfg)
    g = _timewarp_resample(g, GEST, rng, cfg, warp=True)
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
        k = int(rng.integers(5, 25))
        start = int(rng.integers(0, len(g) - k + 1))
        take = min(k, length - pos)
        out[pos:pos + take] = g[start:start + take]
        pos += take
    out += rng.standard_normal(out.shape).astype(np.float32) * (noise_frac * ch_std)
    return out


def make_window(gesture, pool, rng, ch_std, cfg: AugConfig, offset=None):
    """Lay a GEST-step gesture into a WIN-step filler window at a random (or given) offset."""
    win = build_filler(pool, rng, WIN, ch_std, cfg.noise_frac)
    if offset is None:
        offset = int(rng.integers(-CROP, (WIN - GEST) + CROP + 1))
    lo, hi = max(offset, 0), min(offset + GEST, WIN)
    if hi > lo:
        win[lo:hi] = gesture[lo - offset:hi - offset]
    return win


def make_fragment(gesture, pool, rng, ch_std, cfg: AugConfig, vis, where="head"):
    """Window with exactly `vis` of the gesture's GEST steps visible (0..GEST), at a random
    position, the rest filler. `where` = head|tail|mid. vis=0 -> pure filler (leakage probe)."""
    win = build_filler(pool, rng, WIN, ch_std, cfg.noise_frac)
    if vis > 0:
        if where == "tail":
            frag = gesture[GEST - vis:]
        elif where == "mid":
            s = (GEST - vis) // 2
            frag = gesture[s:s + vis]
        else:
            frag = gesture[:vis]
        start = int(rng.integers(0, WIN - vis + 1))
        win[start:start + vis] = frag
    return win
