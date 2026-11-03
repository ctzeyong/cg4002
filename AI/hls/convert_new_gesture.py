"""Convert the new gesture model (.h5) to an hls4ml project and check it against training.

Needs, in the working folder:
  MODEL     the trained .h5 (gesture_pipeline/results/models/new_lean_v2_noise_s2.h5)
  TEST_NPZ  the exact training test windows + reference outputs, made by
            (from AI/) python -m gesture_pipeline.export_test_windows --data new_gesture_100hz_v2 --noise \
                --model gesture_pipeline/results/models/new_lean_v2_noise_s2.h5 \
                --out gesture_pipeline/results/test_windows_v2_noise.npz

Three checks, printed at the end:
  (1) stripped Keras vs the .h5's saved outputs  -> dropout removal changed nothing  (expect 0)
  (2) HLS C-model vs Keras, window by window     -> same logits, argmax, event decision
  (3) accuracy per test set                      -> Keras == training exactly, HLS ~= Keras
"""
import os, math
import numpy as np, tensorflow as tf, hls4ml
import matplotlib; matplotlib.use("Agg")
import hls4ml.model.profiling as profiling
from qkeras.utils import load_qmodel
from tensorflow.keras import Input
from tensorflow.keras.models import Model
from sklearn.metrics import confusion_matrix

# ---------------- run settings ----------------
MODEL        = "new_gesture_w_noise.h5"       # = new_lean_v2_noise_s2.h5 (16/32/32 QCNN, 8 classes)
OUT_DIR      = "new_gesture"                  # new hls4ml project folder (keeps baseline_improved intact)
TEST_NPZ     = "test_windows_v2_noise.npz"    # from gesture_pipeline.export_test_windows (required)
REUSE_FACTOR = 4096                           # raise this to trade DSPs for latency if resources are tight
IS_TRACE_ON  = False
THRESHOLD    = 0.7                            # comms CONF_THRESHOLD (AI/board/wk10_integration.py)
# per-set accuracy of this model in training (gesture_pipeline/results/run_new_lean_v2_noise.json, seed 2);
# noise = 1 - false_accept. Keras on TEST_NPZ must reproduce these exactly.
TARGET       = {"jitter": 0.9399, "slide_bg": 0.9803, "noise": 0.9767}
N_TB_PER_SET = 7                              # golden-testbench windows taken from each test set
os.makedirs("profile_run", exist_ok=True)

# ---------------- load model, strip Dropout for HLS ----------------
class InputLayerCompat(tf.keras.layers.InputLayer):
    """The .h5 was saved by a newer Keras (TF 2.21) that writes an `optional` key into
    InputLayer's config; older Keras (this hls4ml env) rejects it. It is always False here
    and does nothing, so drop it."""
    def __init__(self, *args, optional=None, **kwargs):
        super().__init__(*args, **kwargs)

model = load_qmodel(MODEL, custom_objects={"InputLayer": InputLayerCompat}, compile=False)

def strip_dropout(m):
    """Dropout-free model that REUSES the trained layers (exact weights/quantizers).
    Also returns a trace model over every kept layer, so profiling does NOT rely on
    get_ymodel_keras (which breaks on reused layers -> 'graph disconnected')."""
    inp = Input(shape=m.input_shape[1:], name="imu_window")
    x = inp
    taps, names = [], []
    for layer in m.layers:
        cls = layer.__class__.__name__
        # isinstance, not the class name: the loaded input layer is an InputLayerCompat
        if isinstance(layer, tf.keras.layers.InputLayer) or "dropout" in layer.name.lower() or "Dropout" in cls:
            continue
        x = layer(x)
        taps.append(x); names.append(layer.name)
    infer = Model(inp, x, name="infer")
    trace = Model(inp, taps, name="trace")      # one output per kept layer
    return infer, trace, names

model, trace_model, trace_names = strip_dropout(model)
model.summary()

# ---------------- EXACT training test windows (150 x 6) + reference outputs ----------------
d = np.load(TEST_NPZ)                           # no fallback: windows must come from the pipeline
X_te = np.ascontiguousarray(d["X"].astype("float32"))   # already on the board's int16/1024 input grid
y_te, S, ref = d["y"], d["set"], d["logits_ref"]
names = [str(n) for n in d["names"]]
N_OUT = int(model.output_shape[-1])
assert N_OUT == len(names) == ref.shape[1], f"model has {N_OUT} outputs, npz has {len(names)} classes"
NOISE_ID = names.index("noise") if "noise" in names else None
print(f"[test data] {X_te.shape} from {TEST_NPZ}; classes {names}; "
      + ", ".join(f"{s} {int((S == s).sum())}" for s in np.unique(S)))

# ---------------- hls4ml config ----------------
cfg = hls4ml.utils.config_from_keras_model(model, granularity='name', backend='Vitis')
cfg['Model']['ReuseFactor'] = REUSE_FACTOR
cfg['Model']['Trace'] = IS_TRACE_ON
cfg['Model']['Strategy'] = 'Resource'           # prevent excessive loop unrolling
for layer in cfg['LayerName'].values():
    layer['ReuseFactor'] = REUSE_FACTOR
    layer['Trace'] = IS_TRACE_ON

F6  = 'ap_fixed<16,6,AP_RND_CONV,AP_SAT>'
F7  = 'ap_fixed<16,7,AP_RND_CONV,AP_SAT>'
U6  = 'ap_ufixed<16,6,AP_RND_CONV,AP_SAT>'
ACC = 'ap_fixed<24,12,AP_RND_CONV,AP_SAT>'      # wide accumulator (guard bits); your proven board value
RECO = {   # same architecture/layer names as gesture_real_baseline_v3; re-check tables (C)/(D) below
    'conv1': {'result': F7, 'accum': ACC},
    'conv2': {'result': F7, 'accum': ACC},
    'conv3': {'result': F6, 'accum': ACC},
    'bn1':   {'result': F6}, 'bn2': {'result': F6}, 'bn3': {'result': F6},
    'relu1': {'result': U6}, 'relu2': {'result': U6}, 'relu3': {'result': U6},
    'pool1': {'result': U6}, 'pool2': {'result': U6},
    'gpool': {'result': F6},                    # maxpool accum left at AUTO (it's a max, not a sum)
    'dense': {'result': F6, 'accum': ACC},      # result -> network OUTPUT = <16,6> (logits = outb / 1024)
}
for name, layer in cfg['LayerName'].items():
    base = name[:-7] if name.endswith('_linear') else name
    if base in RECO:
        prec = layer.setdefault('Precision', {})
        for k, v in RECO[base].items():
            if k == 'result' or k in prec:      # always set result (also on the _linear twin); accum only where it exists
                prec[k] = v
# the board feeds int16 = round(x * 1024) -> the input is ap_fixed<16,6> (10 fraction bits, +-32)
cfg['LayerName'].setdefault('imu_window', {}).setdefault('Precision', {})['result'] = F6

hls_model = hls4ml.converters.convert_from_keras_model(
    model, hls_config=cfg, backend='Vitis', part='xczu3eg-sbva484-1-i',
    output_dir=OUT_DIR, io_type='io_stream', project_name='cnn', clock_period=10)
hls_model.compile()

# ---------------- per-layer profiling ----------------
def ibits(x):                                   # signed integer bits to hold +-x without overflow
    return 1 if x <= 0 else int(math.ceil(math.log2(x + 1e-12))) + 1

step = max(1, len(X_te) // 1000)                # ~1000 windows spread over ALL test sets
Xprof = np.ascontiguousarray(X_te[::step])

print("\n=== (A) hls4ml precisions per layer ===")
for name, lc in cfg['LayerName'].items():
    prec = lc.get('Precision', {})
    if prec:
        print(f"{name:18s} " + "  ".join(f"{k}={v}" for k, v in prec.items()))

print("\n=== (B) weights & biases: observed range + suggested int bits ===")
print(f"{'layer':18s} {'param':16s} {'min':>12s} {'max':>12s} {'|max|':>12s} {'int_bits':>9s}")
for L in model.layers:
    for w in L.weights:
        v = w.numpy(); a = float(np.abs(v).max())
        pname = w.name.split('/')[-1].split(':')[0]
        print(f"{L.name:18s} {pname:16s} {v.min():12.5f} {v.max():12.5f} {a:12.5f} {ibits(a):9d}")

print("\n=== (C) activation/result ranges (Keras trace); conv/dense result == accumulator ===")
acts = trace_model.predict(Xprof, verbose=0)
if not isinstance(acts, list):                  # single-tap safety
    acts = [acts]
ktrace = {n: a for n, a in zip(trace_names, acts)}
print(f"{'layer':18s} {'min':>12s} {'max':>12s} {'|max|':>12s} {'p99.9|.|':>12s} {'int_bits':>9s}")
for name, arr in ktrace.items():
    a = np.asarray(arr).ravel(); am = np.abs(a)
    print(f"{name:18s} {a.min():12.5f} {a.max():12.5f} {am.max():12.5f} "
          f"{np.percentile(am, 99.9):12.5f} {ibits(float(am.max())):9d}")

print("\n=== (D) Keras vs HLS fixed-point per layer (catches saturation) ===")
_, htrace = hls_model.trace(Xprof)
print(f"{'layer':18s} {'keras|max|':>11s} {'hls|max|':>11s} {'max|diff|':>11s}")
for lname in htrace:
    if lname not in ktrace:
        continue
    k = np.asarray(ktrace[lname]).ravel(); h = np.asarray(htrace[lname]).ravel()
    n = min(k.size, h.size)
    print(f"{lname:18s} {np.abs(k[:n]).max():11.4f} {np.abs(h[:n]).max():11.4f} "
          f"{np.abs(k[:n] - h[:n]).max():11.4f}")

print("\n=== (E) distribution figures ===")
try:
    figs = profiling.numerical(model=model, hls_model=hls_model, X=Xprof)
    for fig, nm in zip(figs, ["weights_keras", "weights_hls", "activations_keras", "activations_hls"]):
        if fig is not None:
            fig.savefig(f"profile_run/{nm}.png", dpi=150, bbox_inches="tight")
            print("saved profile_run/" + nm + ".png")
except Exception as e:
    print(f"[skipped figures] profiling.numerical failed ({type(e).__name__}); "
          f"the numeric tables (A)-(D) already give every per-layer range.")

# ---------------- verification: outputs, decisions, accuracy ----------------
def softmax(z):
    e = np.exp(z - z.max(1, keepdims=True)); return e / e.sum(1, keepdims=True)

def no_event(z):
    """The comms rule: no game event if the top class is noise or its probability < THRESHOLD."""
    p = softmax(z)
    out = p.max(1) < THRESHOLD
    if NOISE_ID is not None:
        out |= p.argmax(1) == NOISE_ID
    return out

y_keras = model.predict(X_te, verbose=0).reshape(len(X_te), -1)
y_hls   = np.asarray(hls_model.predict(X_te)).reshape(len(X_te), -1)
pk, ph  = y_keras.argmax(1), y_hls.argmax(1)
d_kr = np.abs(y_keras - ref)
d_kh = np.abs(y_keras - y_hls)

print("\n=== (1) stripped Keras vs the .h5's saved outputs (expect 0) ===")
print(f"max |logit diff| {d_kr.max():.6f}")

print("\n=== (2) HLS C-model vs Keras, same windows ===")
print(f"logit |diff|: max {d_kh.max():.4f}  mean {d_kh.mean():.4f}  p99 {np.percentile(d_kh, 99):.4f}")
print(f"argmax agree           : {(pk == ph).mean():.4f}  ({int((pk != ph).sum())} windows differ)")
print(f"event / no-event agree : {(no_event(y_keras) == no_event(y_hls)).mean():.4f}  (threshold {THRESHOLD})")
bad = np.flatnonzero(pk != ph)
for i in bad[:10]:
    print(f"  window {i:5d} [{S[i]}] true {names[y_te[i]]:9s} keras {names[pk[i]]:9s} hls {names[ph[i]]:9s} "
          f"max|diff| {d_kh[i].max():.4f}")

print("\n=== (3) accuracy per test set (Keras must equal training; HLS should match Keras) ===")
print(f"{'set':9s} {'windows':>8s} {'training':>9s} {'Keras':>8s} {'HLS':>8s}")
for s in TARGET:
    m = S == s
    if not m.any():
        continue
    print(f"{s:9s} {int(m.sum()):8d} {TARGET[s]:9.4f} {(pk[m] == y_te[m]).mean():8.4f} {(ph[m] == y_te[m]).mean():8.4f}")
print(f"{'all':9s} {len(y_te):8d} {'':9s} {(pk == y_te).mean():8.4f} {(ph == y_te).mean():8.4f}")

print("\nHLS confusion matrix (rows = true, cols = predicted):", names)
print(confusion_matrix(y_te, ph, labels=range(N_OUT)))

# ---------------- golden testbench (150x6 -> 900 values per row, N_OUT outputs per row) ----------------
tb_idx = np.concatenate([np.flatnonzero(S == s)[:N_TB_PER_SET] for s in np.unique(S)])
X_tb = np.ascontiguousarray(X_te[tb_idx])
gold = np.asarray(hls_model.predict(X_tb)).reshape(len(tb_idx), -1)
tb = os.path.join(OUT_DIR, "tb_data"); os.makedirs(tb, exist_ok=True)
np.savetxt(os.path.join(tb, "tb_input.txt"),  X_tb.reshape(len(tb_idx), -1), fmt="%.9g")
np.savetxt(os.path.join(tb, "tb_output.txt"), gold, fmt="%.9g")
print(f"\nWrote {len(tb_idx)} testbench windows ({N_TB_PER_SET} per set; 900 inputs, {N_OUT} outputs each) to {tb}/")
