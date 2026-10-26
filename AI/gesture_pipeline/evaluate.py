"""Scoring: overall accuracy, per-class recall, and the confusion matrix / worst pairs.
Pure numpy on model predictions; no training concerns here."""
import numpy as np


def predict(model, X):
    return model.predict(X, verbose=0).argmax(1)


def accuracy(model, X, y):
    return float((predict(model, X) == y).mean())


def per_class(preds, y, names):
    """preds: (n,) or stacked (seeds, n). Returns {name: recall} seed-averaged."""
    preds = np.atleast_2d(preds)
    out = {}
    for c, name in enumerate(names):
        m = y == c
        out[name] = float(np.mean([(p[m] == c).mean() for p in preds])) if m.any() else None
    return out


def confusion(preds, y, names):
    """Row-normalised confusion (rows=true), pooled over the rows of `preds`, + worst pairs."""
    preds = np.atleast_2d(preds)
    n = len(names)
    cm = np.zeros((n, n))
    for p in preds:
        for t, pr in zip(y, p):
            cm[t, pr] += 1
    row = cm / cm.sum(1, keepdims=True)
    pairs = sorted(((row[i, j], names[i], names[j]) for i in range(n) for j in range(n)
                    if i != j and cm[i, j] > 0), reverse=True)
    return row, pairs


def format_confusion(row, names):
    lines = ["true\\pred   " + " ".join(f"{x[:6]:>7s}" for x in names)]
    for i, name in enumerate(names):
        lines.append(f"{name:10s} " + " ".join(f"{row[i, j]:7.3f}" for j in range(len(names))))
    return "\n".join(lines)


def by_position(preds, y, pos):
    """Accuracy at each sliding-test position (data.build_slide_test: 0 = head cut ... last =
    tail cut)."""
    return {int(k): float((preds[pos == k] == y[pos == k]).mean()) for k in np.unique(pos)}


def offset_sweep(model, stream, shifts):
    """Accuracy with every test gesture pinned at each fixed shift from the centred position
    (augment.ref_offset).
    Robustness to a mistimed end-of-gesture. Ported from gesture_real_baseline_v3.eval_offsets."""
    out = {}
    for s in shifts:
        X = np.stack([stream.window(i, shift=s) for i in range(len(stream))]).astype("float32")
        out[s] = float((predict(model, X) == stream.y).mean())
    return out


def visible_sweep(model, stream, lengths, where="head"):
    """Accuracy vs how many of the gesture's steps are actually in the window (0 = pure
    filler -> should sit near chance ~1/n_classes). The leakage probe, ported from
    gesture_real_baseline_v3.eval_visible_lengths."""
    out = {}
    for n in lengths:
        X = np.stack([stream.fragment(i, n, where) for i in range(len(stream))]).astype("float32")
        out[n] = float((predict(model, X) == stream.y).mean())
    return out
