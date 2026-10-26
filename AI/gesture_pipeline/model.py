"""THE model architecture, owned here (a deliberate copy of baseline/gesture_real_baseline_v3.py's
QCNN, kept standalone so the pipeline references nothing outside this folder).

build_model(channels, cfg) is the single definition used by every experiment; `channels`
is 6 (raw) or 10 (raw + engineered feature channels). Quantizers match the board's
ap_fixed<16,6> I/O so a trained .h5 maps straight to hls4ml.
"""
import tensorflow as tf
from qkeras import QConv1D, QDense, QActivation
from qkeras.quantizers import quantized_bits, quantized_relu

from .config import WIN, N_CLASSES, ModelConfig

# quantizers: 8-bit weights, ap_fixed<16,6> input/output, 16-bit ReLU activations
_KQ = quantized_bits(8, 2, alpha=1)
_BQ = quantized_bits(8, 2, alpha=1)
_IN_Q = quantized_bits(16, 6, alpha=1)
_ACT_Q = quantized_relu(16, 6)


def _conv_block(x, filters, i, cfg: ModelConfig, pool: bool):
    x = QConv1D(filters, cfg.ksize, padding="valid", use_bias=False,
                kernel_quantizer=_KQ, name=f"conv{i}")(x)
    x = tf.keras.layers.BatchNormalization(name=f"bn{i}")(x)
    x = QActivation(_ACT_Q, name=f"relu{i}")(x)
    if cfg.dropout > 0:
        x = tf.keras.layers.SpatialDropout1D(cfg.dropout, name=f"drop{i}")(x)
    if pool:
        x = tf.keras.layers.MaxPooling1D(2, name=f"pool{i}")(x)
    return x


def build_model(channels: int, cfg: ModelConfig = ModelConfig(), n_classes: int = N_CLASSES):
    """Conv blocks -> global pool -> dense head to n_classes logits (7 gestures, or 8 with the
    noise class). Identical for 6- and 10-channel inputs (only the input width changes).
    BatchNorm folds into the conv at inference (use_bias=False), so it is free on the board."""
    inp = tf.keras.Input(shape=(WIN, channels), name="imu_window")
    x = QActivation(_IN_Q, name="in_quant")(inp)
    x = _conv_block(x, cfg.f1, 1, cfg, pool=True)
    x = _conv_block(x, cfg.f2, 2, cfg, pool=True)
    x = _conv_block(x, cfg.f3, 3, cfg, pool=(cfg.f4 > 0))
    if cfg.f4 > 0:
        x = _conv_block(x, cfg.f4, 4, cfg, pool=False)
    if cfg.pool == "avg":
        x = tf.keras.layers.GlobalAveragePooling1D(name="gpool")(x)
    elif cfg.pool == "both":
        x = tf.keras.layers.Concatenate(name="gpool")(
            [tf.keras.layers.GlobalMaxPooling1D()(x), tf.keras.layers.GlobalAveragePooling1D()(x)])
    else:
        x = tf.keras.layers.GlobalMaxPooling1D(name="gpool")(x)
    logits = QDense(n_classes, kernel_quantizer=_KQ, bias_quantizer=_BQ, name="dense")(x)
    logits = QActivation(_IN_Q, name="dense_q")(logits)
    return tf.keras.Model(inp, logits, name=f"qcnn_{channels}ch")
