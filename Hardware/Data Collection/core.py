"""Transport-independent detector: 500 Hz capture, 100 Hz internal features.

No Bluetooth, packet sending or automatic bias subtraction. Matches C++.
"""
from collections import deque
from pathlib import Path
import csv
import json
import numpy as np

SCHEMA = 'gesture-packet-v4'
GESTURES = ('circle', 'arc', 'infinity', 'caret', 'z', 'triangle', 'star', 'lightning', 'fireball', 'tank', 'chop', 'jabs', 'noise')
CHANNELS = ('ax_g', 'ay_g', 'az_g', 'gx_dps', 'gy_dps', 'gz_dps')
RAW_HZ = 500
RAW_PERIOD_US, FEATURE_PERIOD_US = 2000, 10000
GROUP, WINDOW, HOP, N_FEATURES = 5, 30, 5, 42
BLOCK, ENERGY_WINDOW = 10, 5
DEFAULTS = dict(start_threshold=0.4, stop_threshold=0.2, stop_hold_us=200000)
SCALE = np.array([2, 2, 2, 250, 250, 250], dtype=np.float32)


def validate_config(config):
    if set(config) != set(DEFAULTS):
        raise ValueError('Unexpected gate configuration keys')
    if not 0 <= config['stop_threshold'] < config['start_threshold'] <= 1:
        raise ValueError('Need 0 <= stop threshold < start threshold <= 1')
    hold = config['stop_hold_us']
    if isinstance(hold, bool) or not isinstance(hold, int) or hold < 0:
        raise ValueError('stop_hold_us must be a nonnegative integer')
    return config


def recording_metadata(path):
    meta = json.loads(Path(path).with_suffix('.meta.json').read_text())
    if meta.get('schema') != SCHEMA or meta.get('raw_hz') != RAW_HZ or meta.get('dlpf') != 3:
        raise ValueError(f'{path}: requires new 500 Hz, DLPF=3, {SCHEMA} recordings; recollect older data')
    return meta


def load_csv(path):
    with open(path, newline='') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f'Empty recording: {path}')
    seq = np.array([int(r['sequence']) for r in rows], dtype=np.int64)
    times = np.array([int(r['esp_time_us']) for r in rows], dtype=np.int64)
    x = np.array([[float(r[c]) for c in CHANNELS] for r in rows], dtype=np.float32)
    if not np.isfinite(x).all() or np.any(np.diff(times) <= 0) or np.any(np.diff(seq) <= 0):
        raise ValueError('Invalid readings or device restart; use a separate file per boot')
    return seq, times, x


def labels_for(path, times):
    doc = json.loads(Path(path).read_text())
    if not doc.get('reviewed', False):
        raise ValueError(f'Annotations have not been reviewed: {path}')
    intervals = sorted(doc['intervals'], key=lambda s: s['start_us'])
    last = -1
    for s in intervals:
        a, b = int(s['start_us']), int(s['end_us'])
        if s['label'] not in (*GESTURES, 'ignore') or a < times[0] or b > times[-1]+RAW_PERIOD_US or b <= a:
            raise ValueError('Invalid annotation label, duration or boundary')
        if a < last:
            raise ValueError('Annotations overlap')
        last = b
    return intervals


def binary_labels(times, intervals):
    """Label the newest sample, not the majority of the 0.3 s history window."""
    result = np.zeros(len(times), dtype=np.int64)
    for s in intervals:
        result[(times >= s['start_us']) & (times < s['end_us'])] = -1 if s['label']=='ignore' else 1
    return result


def summarize(window):
    """Per axis: mean, std, range, mean |difference|, three 0.1 s block means."""
    a = np.asarray(window, dtype=np.float32) / SCALE
    if a.shape != (WINDOW, 6):
        raise ValueError('Features need exactly 30 six-axis samples')
    mean = a.mean(axis=0)
    std = np.sqrt(np.maximum(0, (a*a).mean(axis=0)-mean*mean))
    features = np.stack([mean, std, a.max(0)-a.min(0), np.abs(np.diff(a, axis=0)).mean(0),
                         a[:BLOCK].mean(0), a[BLOCK:2*BLOCK].mean(0), a[2*BLOCK:].mean(0)], axis=1)
    return features.ravel().astype(np.float32)


class Frontend:
    """Sensor-grid timestamps. Never average across missing samples."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.previous = None
        self.group = []
        self.window = deque(maxlen=WINDOW)
        self.count = 0

    def push(self, seq, time_us, x):
        x = np.asarray(x, dtype=np.float32)
        gap = self.previous is not None and (
            seq != self.previous[0]+1 or time_us-self.previous[1] != RAW_PERIOD_US)
        valid = x.shape == (6,) and np.isfinite(x).all()
        if gap or not valid:
            self.reset()
        self.previous = (seq, time_us)
        result = dict(gap=bool(gap or not valid), sample=None, features=None)
        if not valid:
            return result
        self.group.append(x.copy())
        if len(self.group) < GROUP:
            return result
        x = np.mean(self.group, axis=0, dtype=np.float32)
        self.group.clear()
        self.window.append(x.copy())
        self.count += 1
        result['sample'] = (time_us-(GROUP-1)*RAW_PERIOD_US//2, x)
        if self.count >= WINDOW and (self.count-WINDOW) % HOP == 0:
            result['features'] = summarize(self.window)
            recent = np.asarray(self.window, dtype=np.float32)[-ENERGY_WINDOW:]
            result['energy'] = float(np.sum(np.var(recent[:, :3], axis=0))/.01 +
                                     np.mean(np.sum(recent[:, 3:]**2, axis=1))/400)
        return result


class Gate:
    """Stop after 200 ms elapsed low-score time; an uncertain score keeps going."""
    def __init__(self, config=None):
        self.config = validate_config(dict(DEFAULTS, **(config or {})))
        self.reset()

    def reset(self):
        self.active = False
        self.low_since = None

    def step(self, score, time_us):
        if not np.isfinite(score) or not 0 <= score <= 1:
            self.low_since = None
            return None
        if not self.active:
            if score >= self.config['start_threshold']:
                self.active = True
                return 'START'
        elif score < self.config['stop_threshold']:
            if self.low_since is None:
                self.low_since = time_us
            if time_us-self.low_since >= self.config['stop_hold_us']:
                self.reset()
                return 'STOP'
        else:
            self.low_since = None
        return None


def validate_model(model):
    if model.get('schema') != SCHEMA or model.get('n_features') != N_FEATURES:
        raise ValueError(f'Wrong model format; retrain/export for {SCHEMA} at 500 Hz')
    if not 1 <= len(model['trees']) <= 8:
        raise ValueError('Expected one to eight trees')
    for tree in model['trees']:
        n = len(tree['feature'])
        if not 1 <= n <= 31 or any(len(tree[k]) != n for k in ('left', 'right', 'threshold', 'probability')):
            raise ValueError('Invalid tree arrays or size')
        def visit(i, depth, seen):
            if i in seen or not 0 <= i < n or depth > 4:
                raise ValueError('Invalid/cyclic tree or depth exceeds four')
            seen.add(i)
            if not np.isfinite(tree['probability'][i]) or not 0 <= tree['probability'][i] <= 1:
                raise ValueError('Invalid leaf score')
            if tree['left'][i] == -1 and tree['right'][i] == -1:
                return
            if not 0 <= tree['feature'][i] < N_FEATURES or not np.isfinite(tree['threshold'][i]):
                raise ValueError('Invalid split')
            visit(tree['left'][i], depth+1, seen)
            visit(tree['right'][i], depth+1, seen)
        seen = set()
        visit(0, 0, seen)
        if len(seen) != n:
            raise ValueError('Unreachable tree nodes')
    return model


def load_model(path):
    return validate_model(json.loads(Path(path).read_text()))


def probability(features, model):
    result = 0.
    for tree in model['trees']:
        i = 0
        while tree['left'][i] != -1:
            i = tree['left'][i] if features[tree['feature'][i]] <= tree['threshold'][i] else tree['right'][i]
        result += tree['probability'][i]
    return result/len(model['trees'])


def replay(seq, times, x, model, config=None):
    """Completed symbol candidates only; incomplete tails are rejected at EOF."""
    from segments import Segmenter
    front, segmenter = Frontend(), Segmenter(config)
    decisions, transitions, intervals, output_times = [], [], [], []
    completed = []
    segment_start = int(times[0])
    for s, t, row in zip(seq, times, x):
        s, t = int(s), int(t)
        result = front.push(s, t, row)
        if result['gap']:
            transitions.append(dict(kind='GAP', time_us=t))
            segmenter.reset()
            segment_start = t
        if result['sample'] is not None:
            output_times.append(result['sample'][0])
        if result['features'] is not None:
            score = probability(result['features'], model)
            decisions.append((t, score))
            event = segmenter.step(score, r_energy=result['energy'], time_us=t, available_start=segment_start)
            if event:
                transitions.append(event)
                if event['kind'] == 'COMPLETE':
                    intervals.append((event['start_us'], event['end_us']))
                    completed.append(event)
    active_at_end = segmenter.active
    return dict(decisions=decisions, transitions=transitions, intervals=intervals,
                active_at_end=active_at_end, output_times=output_times, completed=completed)
