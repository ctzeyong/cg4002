"""Local Keras (.h5) inference for the per-sample gesture path.

Lets the comms pipeline run end-to-end on a laptop with a trained Keras model instead
of the Ultra96 bitstream. Drop-in for wk10_integration.GestureClassifier: same
feed()/infer()/infer_window()/reset() API, so the comms `AI` adapter uses it unchanged.
Selected by the board client's `--ai-h5 PATH` flag.

Non-destructive: wk10_integration.py is imported and reused, never modified. Only the
compute backend is swapped (pynq -> Keras); the ring buffer, decode, end-of-gesture
logic, softmax and CONF_THRESHOLD are all inherited.

Works for a 6-channel model directly; for a 10-channel (feature) model it appends the
4 derived channels (must match run_features.add_features).
"""
import os
os.environ.setdefault('TF_USE_LEGACY_KERAS', '1')
os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '3')
import pathlib
import sys

import numpy as np

try:
    import wk10_integration as wk
except ImportError:                                  # in the repo, add this folder to the path
    sys.path.append(str(pathlib.Path(__file__).resolve().parent))
    import wk10_integration as wk

# re-exports the comms `AI` adapter (gesture.py) needs from the "module"
IMUSample = wk.IMUSample
GestureResult = wk.GestureResult
ModelConfig = wk.ModelConfig
ai_event_from_result = wk.ai_event_from_result

# class index (sorted gesture names used in training) -> game-facing name
NEW_LABEL_MAP = {0: 'warrior', 1: 'mage', 2: 'fireball', 3: 'rogue',
                 4: 'lightning', 5: 'hero', 6: 'shield'}


def _add_features(w):
    """6-ch (T,6) SI window -> 10-ch, matching run_features.add_features (per-step grads)."""
    a, g = w[:, 0:3], w[:, 3:6]
    a_mag = np.linalg.norm(a, axis=-1, keepdims=True)
    g_mag = np.linalg.norm(g, axis=-1, keepdims=True)
    a_jerk = np.linalg.norm(np.gradient(a, axis=0), axis=-1, keepdims=True)
    g_ang = np.linalg.norm(np.gradient(g, axis=0), axis=-1, keepdims=True)
    return np.clip(np.concatenate([w, a_mag, g_mag, a_jerk, g_ang], axis=-1), -63.9, 63.9)


class _KerasBackend:
    """Mirror of wk10_integration._BoardBackend, but runs a Keras .h5 locally.
    Owns the int16 inb/outb buffers the base classifier fills and reads."""

    def __init__(self, h5, config):
        from qkeras.utils import load_qmodel        # lazy: only when a Keras model is used
        self.model = load_qmodel(str(h5))
        self.config = config
        self.in_ch = int(self.model.input_shape[-1])          # 6 or 10
        self.inb = np.zeros((config.win * config.channels,), dtype=np.int16)   # 150*6
        self.outb = np.zeros((config.n_classes,), dtype=np.int16)

    def run(self):
        # inb = to_fixed(SI) = round(SI * scale); recover the SI window the model trained on
        w = np.asarray(self.inb, dtype=np.float64).reshape(self.config.win, self.config.channels)
        w = w / self.config.scale
        if self.in_ch != self.config.channels:
            w = _add_features(w)                              # 6 -> 10 for a feature model
        logits = self.model.predict(w[None].astype('float32'), verbose=0)[0]
        q = np.round(np.asarray(logits, dtype=np.float64) * (1 << self.config.out_frac))
        self.outb[:] = np.clip(q, -32768, 32767).astype(np.int16)
        return np.asarray(self.outb)


class GestureClassifier(wk.GestureClassifier):
    """wk10 GestureClassifier with the pynq backend swapped for a local Keras model."""

    def __init__(self, h5, config=None):
        self.config = config or wk.ModelConfig(label_map=dict(NEW_LABEL_MAP))
        self._backend = _KerasBackend(h5, self.config)
        self.inb = self._backend.inb
        self.outb = self._backend.outb
        self.bufs = {}
        self.last_result = None
        # no _assert_parity: there is no bitstream / label_map.json alongside a .h5
