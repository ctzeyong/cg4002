import numpy as np, tensorflow as tf, hls4ml
import os                                                    
import pandas as pd
import matplotlib
matplotlib.use("Agg")                       # save to file, no interactive window
import matplotlib.pyplot as plt
import hls4ml.model.profiling as profiling
os.makedirs("profile_run", exist_ok=True)
from qkeras.utils import load_qmodel
# from qkeras.utils import _add_supported_quantized_objects

# co = {}
# _add_supported_quantized_objects(co)
# model = tf.keras.models.load_model("gesture_min_q.keras", custom_objects=co)


model = load_qmodel("gesture_min_q.h5")

KEEP = ['0', '1', '2', '3', '4', '5', '6']
idx  = {c: i for i, c in enumerate(KEEP)}
out_dir = 'fixed_cnn_10th_oct'
is_trace_on = False

def load(split):
    df = pd.read_csv(f"gestures_{split}.csv")
    X = df.drop(columns=["label"]).values.reshape(-1, 100, 6).astype("float32")
    y = np.array([idx[str(v)] for v in df["label"].values])
    return X, y

X_te, y_te = load("test")
X_te = np.ascontiguousarray(X_te)   
# model = tf.keras.models.load_model("gesture_min_q.keras")
y_keras = model.predict(X_te, verbose=0) 

# cfg = hls4ml.utils.config_from_keras_model(model, granularity='name', backend='Vitis',
#                                            default_precision='ap_fixed<20,8>')

cfg = hls4ml.utils.config_from_keras_model(model, granularity='name', backend='Vitis',
                                           )

print(list(cfg['LayerName'].keys()))           # confirm the layer names first


cfg['Model']['ReuseFactor'] = 16
cfg['Model']['Trace'] = is_trace_on
for layer in cfg['LayerName'].values():
    layer['ReuseFactor'] = 16

# def prec(name):
#     return cfg['LayerName'][name].setdefault('Precision', {})

for layer in cfg['LayerName'].values():
    layer['ReuseFactor'] = 16
    layer['Trace'] = is_trace_on 
    prec = layer.setdefault('Precision', {})
    if 'result' in prec:
        prec['result'] = 'ap_fixed<16,6,AP_RND_CONV,AP_SAT>'
    if 'accum' in prec:
        prec['accum'] = 'ap_fixed<24,12,AP_RND_CONV,AP_SAT>'

# # final classifier dense on Resource strategy (matches original 'dense')
# cfg['LayerName']['dense2']['Strategy'] = 'Resource'    

print(list(cfg['LayerName'].keys()))   
for name, lcfg in cfg['LayerName'].items():
    print(name, lcfg.get('Precision', {}))


# old config
# cfg['Model']['ReuseFactor'] = 16
# for layer in cfg['LayerName'].values():
#     layer['ReuseFactor'] = 16
# # cfg['LayerName']['dense2_softmax']['Implementation'] = 'latency'                  # the fix
# cfg['LayerName']['imu_window'].setdefault('Precision', {})['result']     = 'ap_fixed<16,6>'  # keep I/O 16-bit
# cfg['LayerName']['dense2'].setdefault('Precision', {})['result'] = 'ap_fixed<16,6>'

hls_model = hls4ml.converters.convert_from_keras_model(
    model, hls_config=cfg, backend='Vitis',
    part='xczu3eg-sbva484-1-i', output_dir=out_dir,
    io_type='io_stream', project_name='cnn', clock_period=10,)

hls_model.compile()      # host smoke test only (hls_model.predict) — no HLS synth

N_TB   = 20                                                   # cosim is slow, keep small
X_tb   = np.ascontiguousarray(X_te[:N_TB])
gold   = hls_model.predict(X_tb).reshape(N_TB, -1)            # bit-exact C model = golden
tb_dir = os.path.join(out_dir, "tb_data")
os.makedirs(tb_dir, exist_ok=True)
np.savetxt(os.path.join(tb_dir, "tb_input.txt"),  X_tb.reshape(N_TB, -1), fmt="%.9g")
np.savetxt(os.path.join(tb_dir, "tb_output.txt"), gold, fmt="%.9g")
print(f"Wrote {N_TB} samples to {tb_dir}/tb_input.txt and tb_output.txt")

# ---- accuracy check ----
y_hls = hls_model.predict(X_te).reshape(len(X_te), -1)
pred_keras = y_keras.argmax(axis=1)
pred_hls   = y_hls.argmax(axis=1)

print(f"Keras test acc : {(pred_keras == y_te).mean():.4f}")
print(f"HLS test acc   : {(pred_hls == y_te).mean():.4f}")
print(f"Keras/HLS agree: {(pred_keras == pred_hls).mean():.4f}")
print(f"Max |logit diff|: {np.abs(y_keras - y_hls).max():.4f}")

from sklearn.metrics import confusion_matrix
print(confusion_matrix(y_te, pred_hls))

# ---------- profile ----------
if is_trace_on:
    X_prof = np.ascontiguousarray(X_te[:1000])          # a representative sample
    figs = profiling.numerical(model=model, hls_model=hls_model, X=X_prof)

    # numerical() returns a tuple of figures; save whichever exist
    names = ["weights_keras", "weights_hls", "activations_keras", "activations_hls"]
    for fig, name in zip(figs, names):
        if fig is not None:
            fig.savefig(f"profile_run/profile_{name}.png", dpi=150, bbox_inches="tight")
            print(f"saved profile_run/profile_{name}.png")
