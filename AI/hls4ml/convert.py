"""Convert a trained gesture model (.h5) to an hls4ml Vitis HLS project and check it against training.

Runs in the WSL hls4ml environment (Vitis HLS 2024.1), not on Windows or the board.

    python convert.py --model mage_noise_robust_s2.h5 \
        --npz test_windows_mage_noise_robust.npz --out ~/mage_noise_robust

  --model  the trained .h5, from AI/train/results/models/
  --npz    the exact training test windows + reference outputs, made from AI/ by
               python -m train.export_test_windows --data <dataset> --noise \
                   --model train/results/models/<tag>_s<seed>.h5 \
                   --out train/results/test_windows_<tag>.npz
  --out    the new hls4ml project folder (keep it in the WSL home folder: builds are slow on /mnt/c)

It also copies templates/ into the project (the AXI-stream wrapper cnn_axi.cpp, its testbench
cnn_axi_test.cpp, and build_prj.tcl / build_opt.tcl, which build cnn_axi as the top), with N_T and
N_OUT set from the model. Then, in --out:  vitis_hls -f build_prj.tcl

Three checks, printed at the end:
  (1) stripped Keras vs the .h5's saved outputs  -> dropout removal changed nothing  (expect 0)
  (2) HLS C-model vs Keras, window by window     -> same logits, argmax, event decision
  (3) accuracy per test set                      -> Keras == training exactly, HLS ~= Keras
"""
import argparse
import math
import os
import re
import shutil

import numpy as np, tensorflow as tf, hls4ml
import matplotlib; matplotlib.use("Agg")
import hls4ml.model.profiling as profiling
from qkeras.utils import load_qmodel
from tensorflow.keras import Input
from tensorflow.keras.models import Model
from sklearn.metrics import confusion_matrix

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(HERE, "templates")
PART = "xczu3eg-sbva484-1-i"                    # Ultra96-V2

# ---------------- fixed-point precisions per layer ----------------
F6  = 'ap_fixed<16,6,AP_RND_CONV,AP_SAT>'
F7  = 'ap_fixed<16,7,AP_RND_CONV,AP_SAT>'
U6  = 'ap_ufixed<16,6,AP_RND_CONV,AP_SAT>'
ACC = 'ap_fixed<24,12,AP_RND_CONV,AP_SAT>'      # wide accumulator (guard bits); proven on the board
RECO = {   # layer names from AI/train/model.py; re-check tables (C)/(D) after an architecture change
    'conv1': {'result': F7, 'accum': ACC},
    'conv2': {'result': F7, 'accum': ACC},
    'conv3': {'result': F6, 'accum': ACC},
    'bn1':   {'result': F6}, 'bn2': {'result': F6}, 'bn3': {'result': F6},
    'relu1': {'result': U6}, 'relu2': {'result': U6}, 'relu3': {'result': U6},
    'pool1': {'result': U6}, 'pool2': {'result': U6},
    'gpool': {'result': F6},                    # maxpool accum left at AUTO (it's a max, not a sum)
    'dense': {'result': F6, 'accum': ACC},      # result -> network OUTPUT = <16,6> (logits = outb / 1024)
    'dense_q': {'result': F6},
}


class InputLayerCompat(tf.keras.layers.InputLayer):
    """The .h5 was saved by a newer Keras (TF 2.21) that writes an `optional` key into
    InputLayer's config; older Keras (this hls4ml env) rejects it. It is always False here
    and does nothing, so drop it."""
    def __init__(self, *args, optional=None, **kwargs):
        super().__init__(*args, **kwargs)


def strip_dropout(m):
    """Dropout-free model that REUSES the trained layers (exact weights/quantizers).
    Also returns a trace model over every kept layer, so profiling does NOT rely on
    get_ymodel_keras (which breaks on reused layers -> 'graph disconnected')."""
    inp = Input(shape=m.input_shape[1:], name="imu_window")
    x = inp
    taps, names = [], []
    for layer in m.layers:
        cls = layer.__class__.__name__
        if isinstance(layer, tf.keras.layers.InputLayer) or "dropout" in layer.name.lower() or "Dropout" in cls:
            continue
        x = layer(x)
        taps.append(x); names.append(layer.name)
    infer = Model(inp, x, name="infer")
    trace = Model(inp, taps, name="trace")      # one output per kept layer
    return infer, trace, names


def ibits(x):                                   # signed integer bits to hold +-x without overflow
    return 1 if x <= 0 else int(math.ceil(math.log2(x + 1e-12))) + 1


def softmax(z):
    e = np.exp(z - z.max(1, keepdims=True)); return e / e.sum(1, keepdims=True)


def install_templates(out_dir, n_t, n_out):
    """Copy templates/ into the project, overwriting hls4ml's build_prj.tcl / build_opt.tcl,
    with the window length and class count set in the C++ wrapper and testbench."""
    for name in sorted(os.listdir(TEMPLATES)):
        src, dst = os.path.join(TEMPLATES, name), os.path.join(out_dir, name)
        if name.endswith(".cpp"):
            text = open(src).read()
            text = re.sub(r"(#define\s+N_T\s+)\d+", rf"\g<1>{n_t}", text)
            text = re.sub(r"(#define\s+N_OUT\s+)\d+", rf"\g<1>{n_out}", text)
            open(dst, "w").write(text)
        else:
            shutil.copyfile(src, dst)
        print(f"  {name} -> {dst}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model", required=True, help="trained .h5")
    ap.add_argument("--npz", required=True, help="test windows from train.export_test_windows")
    ap.add_argument("--out", required=True, help="new hls4ml project folder")
    ap.add_argument("--reuse-factor", type=int, default=4096,
                    help="raise to trade DSPs for latency if resources are tight (default 4096)")
    ap.add_argument("--threshold", type=float, default=0.7,
                    help="comms CONF_THRESHOLD (AI/ultra96/wk10_integration.py), for the event check")
    ap.add_argument("--tb-per-set", type=int, default=7,
                    help="golden-testbench windows taken from each test set (default 7)")
    ap.add_argument("--trace", action="store_true", help="hls4ml Trace on every layer")
    ap.add_argument("--clock-period", type=float, default=10, help="ns (default 10 = 100 MHz)")
    ap.add_argument("--no-templates", action="store_true", help="don't copy templates/ into --out")
    args = ap.parse_args()

    out_dir = os.path.abspath(os.path.expanduser(args.out))
    profile_dir = os.path.join(out_dir, "profile_run")

    # ---------------- load model, strip Dropout for HLS ----------------
    model = load_qmodel(args.model, custom_objects={"InputLayer": InputLayerCompat}, compile=False)
    model, trace_model, trace_names = strip_dropout(model)
    model.summary()

    # ---------------- EXACT training test windows (150 x 6) + reference outputs ----------------
    d = np.load(args.npz)                       # no fallback: windows must come from the pipeline
    X_te = np.ascontiguousarray(d["X"].astype("float32"))   # already on the board's int16/1024 input grid
    y_te, S, ref = d["y"], d["set"], d["logits_ref"]
    names = [str(n) for n in d["names"]]
    N_OUT = int(model.output_shape[-1])
    assert N_OUT == len(names) == ref.shape[1], f"model has {N_OUT} outputs, npz has {len(names)} classes"
    NOISE_ID = names.index("noise") if "noise" in names else None
    print(f"[test data] {X_te.shape} from {args.npz}; classes {names}; "
          + ", ".join(f"{s} {int((S == s).sum())}" for s in np.unique(S)))

    # ---------------- hls4ml config ----------------
    cfg = hls4ml.utils.config_from_keras_model(model, granularity='name', backend='Vitis')
    cfg['Model']['ReuseFactor'] = args.reuse_factor
    cfg['Model']['Trace'] = args.trace
    cfg['Model']['Strategy'] = 'Resource'       # prevent excessive loop unrolling
    for layer in cfg['LayerName'].values():
        layer['ReuseFactor'] = args.reuse_factor
        layer['Trace'] = args.trace

    for name, layer in cfg['LayerName'].items():
        base = name[:-7] if name.endswith('_linear') else name
        if base in RECO:
            prec = layer.setdefault('Precision', {})
            for k, v in RECO[base].items():
                if k == 'result' or k in prec:  # always set result (also on the _linear twin); accum only where it exists
                    prec[k] = v
    # the board feeds int16 = round(x * 1024) -> the input is ap_fixed<16,6> (10 fraction bits, +-32)
    cfg['LayerName'].setdefault('imu_window', {}).setdefault('Precision', {})['result'] = F6

    hls_model = hls4ml.converters.convert_from_keras_model(
        model, hls_config=cfg, backend='Vitis', part=PART,
        output_dir=out_dir, io_type='io_stream', project_name='cnn', clock_period=args.clock_period)
    hls_model.compile()
    os.makedirs(profile_dir, exist_ok=True)

    # ---------------- per-layer profiling ----------------
    step = max(1, len(X_te) // 1000)            # ~1000 windows spread over ALL test sets
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
    if not isinstance(acts, list):              # single-tap safety
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
                path = os.path.join(profile_dir, nm + ".png")
                fig.savefig(path, dpi=150, bbox_inches="tight")
                print("saved " + path)
    except Exception as e:
        print(f"[skipped figures] profiling.numerical failed ({type(e).__name__}); "
              f"the numeric tables (A)-(D) already give every per-layer range.")

    # ---------------- verification: outputs, decisions, accuracy ----------------
    def no_event(z):
        """The comms rule: no game event if the top class is noise or its probability < threshold."""
        p = softmax(z)
        out = p.max(1) < args.threshold
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
    print(f"event / no-event agree : {(no_event(y_keras) == no_event(y_hls)).mean():.4f}  "
          f"(threshold {args.threshold})")
    bad = np.flatnonzero(pk != ph)
    for i in bad[:10]:
        print(f"  window {i:5d} [{S[i]}] true {names[y_te[i]]:9s} keras {names[pk[i]]:9s} hls {names[ph[i]]:9s} "
              f"max|diff| {d_kh[i].max():.4f}")

    print("\n=== (3) accuracy per test set (Keras must equal training; HLS should match Keras) ===")
    print(f"{'set':9s} {'windows':>8s} {'Keras':>8s} {'HLS':>8s}")
    for s in np.unique(S):
        m = S == s
        print(f"{s:9s} {int(m.sum()):8d} {(pk[m] == y_te[m]).mean():8.4f} {(ph[m] == y_te[m]).mean():8.4f}")

    print("\nHLS confusion matrix (rows = true, cols = predicted):", names)
    print(confusion_matrix(y_te, ph, labels=range(N_OUT)))

    # ---------------- golden testbench (150x6 -> 900 values per row, N_OUT outputs per row) ----------------
    tb_idx = np.concatenate([np.flatnonzero(S == s)[:args.tb_per_set] for s in np.unique(S)])
    X_tb = np.ascontiguousarray(X_te[tb_idx])
    gold = np.asarray(hls_model.predict(X_tb)).reshape(len(tb_idx), -1)
    tb = os.path.join(out_dir, "tb_data"); os.makedirs(tb, exist_ok=True)
    np.savetxt(os.path.join(tb, "tb_input.txt"),  X_tb.reshape(len(tb_idx), -1), fmt="%.9g")
    np.savetxt(os.path.join(tb, "tb_output.txt"), gold, fmt="%.9g")
    print(f"\nWrote {len(tb_idx)} testbench windows ({args.tb_per_set} per set; "
          f"{X_tb.shape[1] * X_tb.shape[2]} inputs, {N_OUT} outputs each) to {tb}/")

    # ---------------- AXI wrapper + build scripts ----------------
    if not args.no_templates:
        print("\nCopying templates/ into the project:")
        install_templates(out_dir, n_t=int(model.input_shape[1]), n_out=N_OUT)
        print(f"\nNext: cd {out_dir} && vitis_hls -f build_prj.tcl")


if __name__ == "__main__":
    main()
