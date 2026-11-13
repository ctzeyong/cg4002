"""Reproducible BLE-count binary detector training. Writes only beside this script.

python pipeline.py --sessions ../../../sessions/ble
Recorded participant IDs have fixed splits. Legacy real-name aliases are unknown.
Never uses recorded gesture_flag. Use --legacy-sessions for 500 Hz input.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import numpy as np
from sklearn.tree import DecisionTreeClassifier
from sklearn.ensemble import RandomForestClassifier

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'training'
GESTURES = ('circle', 'star', 'lightning', 'fireball', 'tank', 'chop', 'jabs')
CHANNELS = ('ax_g', 'ay_g', 'az_g', 'gx_dps', 'gy_dps', 'gz_dps')
SCALE = np.array([8192]*3 + [65.5]*3)
SPLITS = {'jermaine': 'train', 'brandon': 'train', 'yuwen': 'train',
          'juven': 'validation', 'ty': 'test',
          'p1': 'train', 'p2': 'train', 'john': 'train'}


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, allow_nan=False)+'\n')


def features(windows):
    """Integer sums are exact in double; final features match C++ float rounding."""
    w = windows.astype(np.float64)
    mean = w.mean(axis=1)
    variance = np.maximum(0, (w*w).mean(axis=1)-mean*mean)
    return np.stack([mean, np.sqrt(variance), np.ptp(w, axis=1),
                     np.abs(np.diff(w, axis=1)).mean(axis=1),
                     w[:, :10].mean(axis=1), w[:, 10:20].mean(axis=1),
                     w[:, 20:].mean(axis=1)], axis=2).reshape(-1, 42).astype('float32')


def energy(windows):
    w = windows[:, -5:].astype(np.float64)
    m = w.mean(axis=1)
    sq = (w*w).mean(axis=1)
    terms = np.concatenate([np.maximum(0, sq[:, :3]-m[:, :3]**2)/(8192**2*.01),
                            sq[:, 3:]/(65.5**2*400)], axis=1).astype('float32')
    # Explicit float additions, same order as firmware.
    result = np.zeros(len(w), dtype='float32')
    for c in range(6):
        result += terms[:, c]
    return result


def select_500hz(seq, time, valid, ignored):
    """Select the fifth reading on each fixed 5-sample grid; never average.

    Mark any group crossing a raw gap invalid, even if the missing reading
    would not itself have been selected. Preserve ignore intervals similarly.
    """
    indices = np.flatnonzero((seq-seq[0]) % 5 == 4)
    if not len(indices):
        return indices, valid[:0], ignored[:0]
    discontinuity = np.r_[False, (np.diff(seq) != 1) | (np.diff(time) != 2000)]
    bad = np.r_[0, np.cumsum(~valid | discontinuity)]
    skip = np.r_[0, np.cumsum(ignored)]
    starts = np.r_[0, indices[:-1]+1]
    return indices, bad[indices+1] == bad[starts], skip[indices+1] != skip[starts]


def load_sessions(directory, legacy_directory=None):
    result, audit = [], []
    paths = [(p, 100) for p in sorted(directory.glob('*.csv'))]
    if legacy_directory is not None:
        paths += [(p, 500) for p in sorted(legacy_directory.glob('*.csv'))]
    digests = set()
    for path, hz in paths:
        item = {'csv': path.name if hz == 100 else '500hz/'+path.name,
                'source_hz': hz, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        audit.append(item)
        meta_path, label_path = path.with_suffix('.meta.json'), path.with_suffix('.labels.json')
        if not meta_path.exists() or not label_path.exists():
            item['excluded'] = 'missing metadata or annotations'; continue
        meta, labels = json.loads(meta_path.read_text()), json.loads(label_path.read_text())
        item['metadata_sha256'] = hashlib.sha256(meta_path.read_bytes()).hexdigest()
        item['labels_sha256'] = hashlib.sha256(label_path.read_bytes()).hexdigest()
        if not labels.get('reviewed'):
            item['excluded'] = 'unreviewed'; continue
        if ((meta.get('raw_hz'), meta.get('dlpf'), meta.get('accel_range_g'),
             meta.get('gyro_range_dps')) != (hz, 3, 4, 500)
                or (hz == 100 and meta.get('transport') != 'ble')):
            raise ValueError(f'Wrong acquisition contract: {path}')
        with path.open(newline='') as f:
            rows = list(csv.DictReader(f))
        if not rows:
            item['excluded'] = 'empty'; continue
        if item['sha256'] in digests:
            raise ValueError(f'Duplicate source recording: {path}')
        digests.add(item['sha256'])
        participant = meta['participant']
        if participant not in SPLITS:
            raise ValueError(f'Assign a participant split explicitly: {participant}')
        seq = np.array([int(r['sequence']) for r in rows], dtype='int64')
        time = np.array([int(r['esp_time_us']) for r in rows], dtype='int64')
        if np.any(np.diff(time) <= 0) or np.any(np.diff(seq) <= 0):
            raise ValueError(f'Nonmonotonic recording: {path}')
        # Firmware packet sequence defines this nominal grid, not host arrival timestamps.
        period = 1000000//hz
        if np.any(np.diff(time) != np.diff(seq)*period):
            raise ValueError(f'Inconsistent source nominal grid: {path}')
        physical = np.array([[float(r[c]) for c in CHANNELS] for r in rows])
        counts = physical*SCALE
        rounded = np.rint(counts)
        if (not np.isfinite(counts).all() or np.max(np.abs(counts-rounded)) > .001
                or np.any(rounded < -32768) or np.any(rounded > 32767)):
            raise ValueError(f'Cannot reconstruct exact int16 counts: {path}')
        x = rounded.astype('int16')
        valid = np.array([int(r['imu_status']) == 1 for r in rows]) if hz == 100 else np.ones(len(rows), dtype=bool)
        y = np.zeros(len(x), dtype='int8')
        ignored = np.zeros(len(x), dtype=bool)
        intervals = sorted(labels['intervals'], key=lambda a: a['start_us'])
        last = -1
        for a in intervals:
            start, end, label = a['start_us'], a['end_us'], a['label']
            if label not in (*GESTURES, 'noise', 'ignore') or end <= start or start < last:
                raise ValueError(f'Invalid annotation: {path}: {a}')
            if start < time[0] or end > time[-1]+period:
                raise ValueError(f'Annotation outside recording: {path}: {a}')
            last = end
            mask = (time >= start) & (time < end)
            if label == 'ignore': ignored |= mask
            elif label in GESTURES: y[mask] = 1
        item['source_samples'] = len(x)
        item['source_gaps'] = int(np.sum(np.diff(seq) != 1))
        if hz == 500:
            selected, valid, ignored = select_500hz(seq, time, valid, ignored)
            if not len(selected):
                item['excluded'] = 'fewer than five source samples'; continue
            seq = (seq[selected]-seq[0])//5
            time, x, y = time[selected], x[selected], y[selected]
            item['conversion'] = 'select fifth raw reading on fixed five-sample grid; no averaging'
        # Windows never cross invalid readings, missing packets, or ignored regions.
        ok = valid & ~ignored
        breaks = np.r_[True, np.diff(seq) != 1] | ~ok | np.r_[False, ~ok[:-1]]
        starts = np.flatnonzero(breaks)
        indices = []
        for a, b in zip(starts, np.r_[starts[1:], len(x)]):
            if ok[a] and b-a >= 30:
                indices.extend(range(a+29, b, 5))
        indices = np.array(indices, dtype='int64')
        windows = x[indices[:, None] + np.arange(-29, 1)]
        f = features(windows)
        e = energy(windows)
        item.update(participant=participant, split=SPLITS[participant], samples=len(x),
                    feature_windows=len(f), positive_windows=int(y[indices].sum()),
                    invalid_samples=int((~valid).sum()), ignored_samples=int(ignored.sum()),
                    gaps=int(np.sum(np.diff(seq) != 1)),
                    max_count_reconstruction_error=float(np.max(np.abs(counts-rounded))))
        result.append(dict(item=item, seq=seq, time=time, x=x, valid=valid, ignored=ignored,
                           y=y, indices=indices, features=f, energy=e, intervals=intervals))
    save('manifest.json', {'split_participants': SPLITS,
                          'legacy_included': legacy_directory is not None, 'recordings': audit})
    for split in ('train', 'validation', 'test'):
        subset = [r for r in result if r['item']['split'] == split]
        present = {a['label'] for r in subset for a in r['intervals']}
        if not set(GESTURES) <= present:
            raise ValueError(f'{split} lacks gestures: {set(GESTURES)-present}')
        if set(np.concatenate([r['y'][r['indices']] for r in subset])) != {0, 1}:
            raise ValueError(f'{split} requires both classes')
    return result


class Segmenter:
    """Exact state-machine port of segments.h; no retrospective packet flags."""
    def __init__(self, start, stop):
        self.start_threshold, self.stop_threshold = start, stop
        self.active = self.locked = self.timeout = False
        self.start = self.trigger = self.resume = 0
        self.dip = self.low = self.previous_end = self.unlock = -1
        self.level = np.float32(.05)
        self.energies = []

    def step(self, score, en, t):
        if not self.active:
            if self.timeout:
                if score < self.stop_threshold or en <= self.level:
                    if self.unlock < 0: self.unlock = t
                    if t-self.unlock >= 150000: self.timeout = False
                else: self.unlock = -1
                return 0
            if self.locked:
                if score < self.stop_threshold: self.locked = False
                elif en > self.level*2:
                    self.resume += 1
                    if self.resume >= 2: self.locked = False
                else: self.resume = 0
                if self.locked: return 0
            if score >= self.start_threshold:
                self.active = True
                self.start = max(t-300000, 0, self.previous_end)
                self.trigger = t
                self.energies = [en]
                return 1
            return 0
        peak = max(self.energies) if self.energies else en
        self.energies = (self.energies+[en])[-10:]
        threshold = max(np.float32(peak*np.float32(.2)), np.float32(.05))
        if self.dip < 0:
            if en <= threshold: self.dip, self.level = t, threshold
        elif en > self.level: self.dip = -1
        if score < self.stop_threshold:
            if self.low < 0: self.low = t
        else: self.low = -1
        end = -1
        if self.dip >= 0 and t-self.dip >= 150000: end = self.dip+2000
        elif self.low >= 0 and t-self.low >= 200000: end = self.low+2000
        elif t-self.start >= 6000000:
            self.timeout, self.unlock, self.level = True, -1, threshold
        else: return 0
        self.active, self.locked, self.resume = False, True, 0
        self.dip = self.low = -1
        self.previous_end = end if end >= 0 else t+2000
        return 2 if end >= 0 and 250000 <= end-self.start <= 6000000 else 3


def replay(r, scores, start, stop):
    flags = np.zeros(len(r['x']), dtype='int8')
    events = np.zeros(len(flags), dtype='int8')
    lookup = {int(i): j for j, i in enumerate(r['indices'])}
    seg = Segmenter(start, stop)
    nominal, previous_flag = 0, 0
    for i in range(len(flags)):
        if (not r['valid'][i] or r['ignored'][i] or
                (i and (r['time'][i]-r['time'][i-1] >= 20000 or r['ignored'][i-1]
                        or not r['valid'][i-1]))):
            seg, nominal, previous_flag = Segmenter(start, stop), 0, 0
        if not r['valid'][i] or r['ignored'][i]: continue
        j = lookup.get(i)
        event = seg.step(scores[j], r['energy'][j], nominal) if j is not None else 0
        flag = 1 if seg.active else (2 if event == 3 and previous_flag == 1 else 0)
        flags[i], events[i] = flag, event
        previous_flag = flag
        nominal += 10000
    return flags, events


def metrics(rows, predictions, start, stop):
    tp = pos = fp = neg = false_starts = 0
    per_class = {g: {'samples': 0, 'retained': 0, 'gestures': 0, 'detected': 0,
                     'covered_80_percent': 0} for g in GESTURES}
    delays, details = [], []
    for r, scores in zip(rows, predictions):
        flags, events = replay(r, scores, start, stop)
        valid = r['valid'] & ~r['ignored']
        positive = valid & (r['y'] == 1)
        background = valid & (r['y'] == 0)
        active = flags == 1
        tp += int(np.sum(active & positive)); pos += int(positive.sum())
        fp += int(np.sum(active & background)); neg += int(background.sum())
        false_starts += int(np.sum((events == 1) & background))
        details.append({'csv': r['item']['csv'], 'positive_samples': int(positive.sum()),
                        'retained_positive': int(np.sum(active & positive)),
                        'background_samples': int(background.sum()),
                        'active_background': int(np.sum(active & background))})
        for a in r['intervals']:
            if a['label'] not in GESTURES: continue
            mask = valid & (r['time'] >= a['start_us']) & (r['time'] < a['end_us'])
            if not mask.any(): continue
            retained = int(np.sum(active & mask)); count = int(mask.sum())
            pc = per_class[a['label']]
            pc['samples'] += count; pc['retained'] += retained; pc['gestures'] += 1
            pc['detected'] += int(retained > 0)
            pc['covered_80_percent'] += int(retained/count >= .8)
            if retained:
                delays.append(max(0, int(r['time'][np.flatnonzero(active & mask)[0]])-a['start_us'])/1000)
    for pc in per_class.values():
        pc['sample_recall'] = pc['retained']/max(1, pc['samples'])
        pc['gesture_detection_rate'] = pc['detected']/max(1, pc['gestures'])
    return {'useful_sample_recall': tp/max(1, pos), 'background_active_fraction': fp/max(1, neg),
            'false_starts_per_background_minute': false_starts/max(1, neg)*6000,
            'gesture_detection_rate': sum(p['detected'] for p in per_class.values())/max(1, sum(p['gestures'] for p in per_class.values())),
            'onset_delay_p95_ms_detected_only': float(np.percentile(delays, 95)) if delays else None,
            'positive_samples': pos, 'background_samples': neg, 'per_class': per_class,
            'recordings': details}


def convert(estimator):
    trees = []
    for e in getattr(estimator, 'estimators_', [estimator]):
        t = e.tree_
        threshold = t.threshold.astype('float32')
        up = threshold.astype('float64') > t.threshold
        threshold[up] = np.nextafter(threshold[up], np.float32(-np.inf))
        v = t.value[:, 0]
        scores = (v[:, list(e.classes_).index(1)]/v.sum(axis=1)).astype('float32')
        trees.append(dict(feature=t.feature.tolist(), left=t.children_left.tolist(),
                          right=t.children_right.tolist(), threshold=threshold.tolist(), score=scores.tolist()))
    return trees


def predict(trees, x):
    result = np.zeros(len(x), dtype='float32')
    for t in trees:
        left, right = np.array(t['left']), np.array(t['right'])
        feature, threshold = np.array(t['feature']), np.array(t['threshold'], dtype='float32')
        node = np.zeros(len(x), dtype=int)
        while True:
            idx = np.flatnonzero(left[node] != -1)
            if not len(idx): break
            n = node[idx]
            node[idx] = np.where(x[idx, feature[n]] <= threshold[n], left[n], right[n])
        result += np.array(t['score'], dtype='float32')[node]
    return result/np.float32(len(trees))


def export(model, output=None):
    ident = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()[:16]
    lines = ['#pragma once', '// Generated by local pipeline.py; BLE selected int16 count features.',
             'constexpr int MODEL_SAMPLE_HZ = 100;', 'constexpr bool MODEL_COUNTS_INT16 = true;',
             'constexpr bool MODEL_READY = true;', f'constexpr char MODEL_ID[] = "{ident}";',
             'struct TreeNode { int feature, left, right; float threshold, score; };']
    for k, tree in enumerate(model['trees']):
        lines.append(f'static const TreeNode TREE_{k}[] = {{')
        for f, l, r, t, s in zip(*(tree[n] for n in ('feature', 'left', 'right', 'threshold', 'score'))):
            lines.append(f'{{{f},{l},{r},{t:.9e}f,{s:.9e}f}},')
        lines.append('};')
    lines += [f'constexpr int TREE_COUNT = {len(model["trees"])};',
              'static const TreeNode* const TREES[] = {'+', '.join(f'TREE_{k}' for k in range(len(model['trees'])))+'};',
              f'constexpr float START_THRESHOLD = {model["start"]:.9e}f;',
              f'constexpr float STOP_THRESHOLD = {model["stop"]:.9e}f;',
              'constexpr long long STOP_HOLD_US = 200000;']
    (output or ROOT/'model_data.h').write_text('\n'.join(lines)+'\n')


def verify_cpp(rows, model, compiler):
    exe = OUT/'verify_detector.exe'
    subprocess.run([compiler, '-std=c++11', '-O2', '-Wall', '-Wextra',
                    str(ROOT/'training/verify_detector.cpp'), '-o', str(exe)], check=True)
    summary = {'recordings': 0, 'samples': 0, 'decisions': 0, 'max_feature_error': 0.0,
               'max_score_error': 0.0, 'flag_mismatches': 0, 'event_mismatches': 0}
    for r in rows:
        expected_scores = predict(model['trees'], r['features'])
        flags, events = replay(r, expected_scores, model['start'], model['stop'])
        valid = r['valid'] & ~r['ignored']
        lines = [f'{t//1000} {int(v)} '+ ' '.join(map(str, x)) for t, v, x in zip(r['time'], valid, r['x'])]
        output = subprocess.run([str(exe)], input='\n'.join(lines)+'\n', text=True,
                                capture_output=True, check=True).stdout
        actual = np.loadtxt(output.splitlines(), ndmin=2)
        if len(actual) != len(r['x']): raise AssertionError('C++ sample count')
        np.testing.assert_array_equal(actual[:, 0], flags)
        np.testing.assert_array_equal(actual[:, 1], events)
        idx = np.flatnonzero(actual[:, 2])
        np.testing.assert_array_equal(idx, r['indices'])
        np.testing.assert_allclose(actual[idx, 3], expected_scores, rtol=0, atol=1e-7)
        np.testing.assert_allclose(actual[idx, 4], r['energy'], rtol=1e-6, atol=1e-6)
        np.testing.assert_allclose(actual[idx, 5:], r['features'], rtol=1e-7, atol=.004)
        summary['recordings'] += 1; summary['samples'] += len(actual); summary['decisions'] += len(idx)
        if len(idx):
            summary['max_feature_error'] = max(summary['max_feature_error'], float(np.max(np.abs(actual[idx, 5:]-r['features']))))
            summary['max_score_error'] = max(summary['max_score_error'], float(np.max(np.abs(actual[idx, 3]-expected_scores))))
    save('cpp_verification.json', summary)
    print('C++ parity:', summary, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sessions', type=Path, default=ROOT.parents[2]/'sessions/ble')
    parser.add_argument('--legacy-sessions', type=Path, help='Optional directory of original 500 Hz recordings')
    parser.add_argument('--run-name', help='Save this run under training/<name>')
    parser.add_argument('--compiler', default='g++')
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    global OUT
    if args.run_name:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', args.run_name):
            raise ValueError('Run name must contain only letters, digits, underscore or hyphen')
        OUT = ROOT/'training'/args.run_name
    OUT.mkdir(parents=True, exist_ok=True)
    rows = load_sessions(args.sessions.resolve(), args.legacy_sessions.resolve() if args.legacy_sessions else None)
    if args.verify_only:
        verify_cpp(rows, json.loads((OUT/'model.json').read_text()), args.compiler); return
    train = [r for r in rows if r['item']['split'] == 'train']
    validation = [r for r in rows if r['item']['split'] == 'validation']
    test = [r for r in rows if r['item']['split'] == 'test']
    x = np.concatenate([r['features'] for r in train])
    y = np.concatenate([r['y'][r['indices']] for r in train])
    estimators = {'tree': DecisionTreeClassifier(max_depth=6, min_samples_leaf=20, class_weight='balanced', random_state=16),
                  'forest': RandomForestClassifier(n_estimators=8, max_depth=6, min_samples_leaf=20,
                             class_weight='balanced', random_state=16, n_jobs=1)}
    trials, candidates = [], []
    for name, estimator in estimators.items():
        estimator.fit(x, y)
        trees = convert(estimator)
        np.testing.assert_allclose(predict(trees, x), estimator.predict_proba(x)[:, 1], atol=2e-7, rtol=0)
        predictions = [predict(trees, r['features']) for r in validation]
        # The segmenter's rearm/timeout locks also depend on stop: lowering start
        # alone can paradoxically miss more gestures by entering an early lock.
        threshold_pairs = [(s, p) for s in (.20,.30,.40,.50,.60,.70)
                           for p in (.10,.20,.30,.40,.50,.60) if p < s]
        for start, stop in threshold_pairs:
            report = metrics(validation, predictions, start, stop)
            trial = dict(kind=name, start=start, stop=stop, report=report)
            trials.append(trial)
            candidates.append(dict(kind=name, start=start, stop=stop, trees=trees, validation=report))
            print(name, start, 'recall', round(report['useful_sample_recall'],4),
                  'background', round(report['background_active_fraction'],4), flush=True)
    # Recall first. Among models within 1 percentage point of the best sample recall,
    # retain gesture detection within 1 point of best, then prefer less active background.
    best_recall = max(m['validation']['useful_sample_recall'] for m in candidates)
    eligible = [m for m in candidates if m['validation']['useful_sample_recall'] >= best_recall-.01]
    best_detection = max(m['validation']['gesture_detection_rate'] for m in eligible)
    eligible = [m for m in eligible if m['validation']['gesture_detection_rate'] >= best_detection-.01]
    chosen = min(eligible, key=lambda m: (m['validation']['background_active_fraction'], len(m['trees'])))
    chosen['training'] = {'seed':16, 'windows':len(y), 'positive_windows':int(y.sum()),
                          'input':'selected int16 counts at 100 Hz', 'window':30, 'hop':5,
                          'manifest_sha256':hashlib.sha256((OUT/'manifest.json').read_bytes()).hexdigest()}
    save('selection_report.json', trials)
    save('model.json', chosen)
    report = metrics(test, [predict(chosen['trees'], r['features']) for r in test], chosen['start'], chosen['stop'])
    save('test_report.json', report)
    export(chosen)
    verify_cpp(rows, chosen, args.compiler)
    shutil.copy2(ROOT/'model_data.h', OUT/'model_data.h')
    shutil.copy2(ROOT/'pipeline.py', OUT/'pipeline.py')
    print('SELECTED', chosen['kind'], chosen['start'], chosen['stop'], flush=True)
    print('TEST', json.dumps({k:v for k,v in report.items() if k not in ('recordings','per_class')}), flush=True)


if __name__ == '__main__':
    main()
