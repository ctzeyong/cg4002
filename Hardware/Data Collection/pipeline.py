"""Train/compare small binary trees, evaluate data retention, export plain C++."""
import argparse
import hashlib
import json
from pathlib import Path
import numpy as np
from core import (SCHEMA, GESTURES, N_FEATURES, DEFAULTS, Frontend, binary_labels,
                  RAW_HZ, RAW_PERIOD_US, recording_metadata,
                  load_csv, labels_for, load_model, validate_model, validate_config, replay)


def sessions(manifest):
    groups, hashes, result = {}, set(), []
    for entry in json.loads(manifest.read_text())['sessions']:
        split, group = entry['split'], entry['group']
        if split not in ('train', 'validation', 'test') or not group:
            raise ValueError('Every session needs a group and train/validation/test split')
        if group in groups and groups[group] != split:
            raise ValueError(f'Data leakage: group {group} crosses splits')
        groups[group] = split
        path = (manifest.parent/entry['csv']).resolve()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in hashes:
            raise ValueError('Duplicate recording in manifest')
        hashes.add(digest)
        recording_metadata(path)
        seq, times, x = load_csv(path)
        annotations = labels_for(path.with_suffix('.labels.json'), times)
        gaps = np.flatnonzero((np.diff(seq) != 1) | (np.diff(times) != RAW_PERIOD_US))
        for a in annotations:
            if a['label']=='ignore': continue
            if any(a['start_us'] < times[k+1] and a['end_us'] > times[k] for k in gaps):
                raise ValueError(f'Gesture annotation spans a missing-data gap: {path}')
        result.append((entry, seq, times, x, annotations))
    return result


def dataset(rows, split):
    xx, yy = [], []
    covered = set()
    for entry, seq, times, x, annotations in rows:
        if entry['split'] != split:
            continue
        covered.update(a['label'] for a in annotations if a['label']!='ignore')
        front, local, targets = Frontend(), [], []
        for s, t, row in zip(seq, times, x):
            if any(a['label']=='ignore' and a['start_us'] <= t < a['end_us'] for a in annotations):
                front.reset()
                continue
            r = front.push(int(s), int(t), row)
            if r['features'] is not None:
                local.append(r['features'])
                targets.append(t)
        if local:
            xx.extend(local)
            yy.extend(binary_labels(np.array(targets), annotations))
    if not xx or set(yy) != {0, 1}:
        raise ValueError(f'{split} needs usable positive AND background frames')
    if covered != set(GESTURES):
        raise ValueError(f'{split} missing gesture labels: {set(GESTURES)-covered}')
    return np.asarray(xx, dtype=np.float32), np.asarray(yy, dtype=np.int64)


def convert(estimator, name):
    trees = getattr(estimator, 'estimators_', [estimator])
    result = dict(schema=SCHEMA, n_features=N_FEATURES, kind=name, trees=[])
    for e in trees:
        tree = e.tree_
        values = tree.value[:, 0, :]
        positive_index = list(e.classes_).index(1)
        scores = values[:, positive_index]/values.sum(axis=1)
        threshold = tree.threshold.astype('float32')
        # sklearn inputs are float32; round thresholds down to preserve comparisons.
        up = threshold.astype('float64') > tree.threshold
        threshold[up] = np.nextafter(threshold[up], np.float32(-np.inf))
        result['trees'].append(dict(feature=tree.feature.tolist(),
            left=tree.children_left.tolist(), right=tree.children_right.tolist(),
            threshold=threshold.tolist(), probability=scores.astype('float32').tolist()))
    return validate_model(result)


def train(args):
    from sklearn.tree import DecisionTreeClassifier
    from sklearn.ensemble import RandomForestClassifier
    rows = sessions(args.manifest)
    x, y = dataset(rows, 'train')
    dataset(rows, 'validation')  # Check coverage, never fit with validation samples.
    estimators = dict(tree=DecisionTreeClassifier(max_depth=4, min_samples_leaf=5,
                         class_weight='balanced', random_state=args.seed),
                      forest=RandomForestClassifier(n_estimators=8, max_depth=4,
                         min_samples_leaf=5, class_weight='balanced',
                         max_features='sqrt', random_state=args.seed, n_jobs=1))
    args.out.mkdir(parents=True, exist_ok=True)
    for name, estimator in estimators.items():
        estimator.fit(x, y)
        model = convert(estimator, name)
        model['training'] = dict(seed=args.seed, frames=len(y), positive_frames=int(y.sum()),
            manifest_sha256=hashlib.sha256(args.manifest.read_bytes()).hexdigest())
        (args.out/f'{name}.json').write_text(json.dumps(model, indent=2))
        print(f'{name}: {sum(len(t["feature"]) for t in model["trees"])} nodes; trained on {len(y)} frames')
    print('Now select thresholds on validation. No real-data accuracy is assumed.')


def metrics(rows, model, config, split):
    totals = dict(positive=0, retained_positive=0, background=0, retained_background=0,
                  gestures=0, covered_gestures=0, background_starts=0,
                  packet_tp=0, packet_fp=0, packet_fn=0, merged_packets=0)
    details, delays = [], []
    per_class = {name: dict(total=0, covered=0) for name in GESTURES}
    for entry, seq, times, x, truth in rows:
        if entry['split'] != split:
            continue
        ignored=[a for a in truth if a['label']=='ignore']
        truth=[a for a in truth if a['label']!='ignore']
        r = replay(seq, times, x, model, config)
        # Keep continuous replay, but do not score candidates touching uncertain data.
        excluded=[(a,b) for a,b in r['intervals'] if touches(a,b,ignored)]
        r['intervals']=[(a,b) for a,b in r['intervals'] if not touches(a,b,ignored)]
        affected=[a for a in truth if any(a['start_us']<b and a['end_us']>s for s,b in excluded)]
        truth=[a for a in truth if a not in affected]
        valid=np.ones(len(times),dtype=bool)
        for a,b in [(i['start_us'],i['end_us']) for i in ignored+affected]+excluded:
            valid &= ~((times>=a)&(times<b))
        pairs=[]
        for i,(a,b) in enumerate(r['intervals']):
            overlaps=0
            for j,gt in enumerate(truth):
                overlap=max(0,min(b,gt['end_us'])-max(a,gt['start_us']))
                if overlap >= .2*(gt['end_us']-gt['start_us']): overlaps+=1
                union=max(b,gt['end_us'])-min(a,gt['start_us'])
                if overlap/union >= .5: pairs.append((overlap/union,i,j))
            if overlaps>1: totals['merged_packets']+=1
        used_i,used_j=set(),set()
        for _,i,j in sorted(pairs,reverse=True):
            if i not in used_i and j not in used_j: used_i.add(i); used_j.add(j)
        totals['packet_tp']+=len(used_i)
        totals['packet_fp']+=len(r['intervals'])-len(used_i)
        totals['packet_fn']+=len(truth)-len(used_j)
        # Score every recorded raw sample, including warmup, with the proposed prebuffer.
        sent = np.zeros(len(times), dtype=bool)
        for a, b in r['intervals']:
            sent |= (times >= a) & (times < b)
        y = binary_labels(times, truth)==1
        totals['positive'] += int(y.sum())
        totals['retained_positive'] += int((sent & y).sum())
        totals['background'] += int((~y & valid).sum())
        totals['retained_background'] += int((sent & ~y & valid).sum())
        covered = 0
        for a in truth:
            mask = (times >= a['start_us']) & (times < a['end_us'])
            fraction = float(sent[mask].mean()) if mask.any() else 0.
            ok = fraction >= .95
            covered += int(ok)
            per_class[a['label']]['total'] += 1
            per_class[a['label']]['covered'] += int(ok)
            starts = [e['time_us'] for e in r['transitions'] if e['kind'] == 'START'
                      and a['start_us'] <= e['time_us'] < a['end_us']]
            if starts:
                delays.append((min(starts)-a['start_us'])/1000)
        for e in r['transitions']:
            if (e['kind'] == 'START' and not touches(e['time_us'],e['time_us']+1,ignored)
                and not any(a<=e['time_us']<b for a,b in excluded)
                and not touches(e['time_us'],e['time_us']+1,affected)
                and not any(a['start_us'] <= e['time_us'] < a['end_us'] for a in truth)):
                totals['background_starts'] += 1
        totals['gestures'] += len(truth)
        totals['covered_gestures'] += covered
        details.append(dict(csv=entry['csv'], excluded_packets=len(excluded), excluded_samples=int((~valid).sum()),
                            retained_fraction=float(sent[valid].mean()) if valid.any() else None,
                            active_at_end=r['active_at_end'], transitions=r['transitions']))
    if not details:
        raise ValueError(f'No {split} recordings')
    positive, background = totals['positive'], totals['background']
    return dict(split=split,
        gesture_sample_retention=totals['retained_positive']/positive if positive else None,
        gesture_coverage=totals['covered_gestures']/totals['gestures'] if totals['gestures'] else None,
        background_sent_fraction=totals['retained_background']/background if background else None,
        background_starts_per_minute=totals['background_starts']/(background/RAW_HZ/60) if background else None,
        packet_precision=totals['packet_tp']/max(1,totals['packet_tp']+totals['packet_fp']),
        packet_recall=totals['packet_tp']/max(1,totals['packet_tp']+totals['packet_fn']),
        onset_delay_p95_ms=float(np.percentile(delays, 95)) if delays else None,
        totals=totals, per_class=per_class, sessions=details)


def select(args):
    rows = sessions(args.manifest)
    dataset(rows, 'validation')
    trials, eligible = [], []
    for file in ('tree.json', 'forest.json'):
        model = load_model(args.models/file)
        for start in (.3, .4, .5):
            for stop in (.1, .2):
                config = dict(DEFAULTS, start_threshold=start, stop_threshold=stop)
                report = metrics(rows, model, config, 'validation')
                trials.append(dict(model=file, config=config, report=report))
                retention = report['gesture_sample_retention']
                coverage = report['gesture_coverage']
                bg = report['background_sent_fraction']
                per_class_ok = all(v['total'] and v['covered']/v['total'] >= args.min_coverage
                                   for v in report['per_class'].values())
                print(f'{file}, start={start}, stop={stop}: retained={retention:.3f}, covered={coverage:.3f}, background sent={bg:.3f}', flush=True)
                if (retention >= args.min_retention and coverage >= args.min_coverage and per_class_ok and bg <= args.max_background
                        and report['packet_precision'] >= args.min_packet_score
                        and report['packet_recall'] >= args.min_packet_score and report['totals']['merged_packets']==0):
                    eligible.append((bg, len(model['trees']), model, config, report))
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out/'selection_report.json').write_text(json.dumps(trials, indent=2))
    if not eligible:
        raise ValueError('Neither model meets the validation targets. Collect better examples or improve the model; do not export.')
    _, _, model, config, report = min(eligible, key=lambda a: (a[0], a[1]))
    (args.out/'model.json').write_text(json.dumps(model, indent=2))
    (args.out/'gate.json').write_text(json.dumps(config, indent=2))
    print(f'Selected {model["kind"]}; now evaluate on untouched test sessions.')


def evaluate(args):
    config = validate_config(json.loads(args.config.read_text()))
    report = metrics(sessions(args.manifest), load_model(args.model), config, args.split)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))
    print(json.dumps({k:v for k,v in report.items() if k != 'sessions'}, indent=2))


def export(args):
    model, config = load_model(args.model), validate_config(json.loads(args.config.read_text()))
    ident = hashlib.sha256(args.model.read_bytes()+args.config.read_bytes()).hexdigest()[:16]
    lines = ['#pragma once', '// Exported gesture-packet-v4 model. No Bluetooth code.',
        'constexpr int MODEL_RAW_HZ = 500;',
        'constexpr bool MODEL_READY = true;', f'constexpr char MODEL_ID[] = "{ident}";',
        'struct TreeNode { int feature, left, right; float threshold, score; };']
    for i, tree in enumerate(model['trees']):
        lines.append(f'static const TreeNode TREE_{i}[] = {{')
        for f,l,r,t,p in zip(tree['feature'],tree['left'],tree['right'],tree['threshold'],tree['probability']):
            lines.append(f'{{{f},{l},{r},{float(t):.9e}f,{float(p):.9e}f}},')
        lines.append('};')
    lines.extend([f'constexpr int TREE_COUNT = {len(model["trees"])};',
                  'static const TreeNode* const TREES[] = {'+', '.join(f'TREE_{i}' for i in range(len(model['trees'])))+'};',
                  f'constexpr float START_THRESHOLD = {config["start_threshold"]:.9e}f;',
                  f'constexpr float STOP_THRESHOLD = {config["stop_threshold"]:.9e}f;',
                  f'constexpr long long STOP_HOLD_US = {config["stop_hold_us"]};'])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text('\n'.join(lines)+'\n')
    print(f'Exported {ident} to {args.out}')


def prepare(args):
    from cloud_export import prepare_examples
    return prepare_examples(args, sessions)


def touches(start, end, intervals):
    return any(start < a['end_us'] and end > a['start_us'] for a in intervals)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    t = sub.add_parser('train')
    t.add_argument('--manifest', type=Path, required=True)
    t.add_argument('--out', type=Path, default=Path('artifacts/candidates'))
    t.add_argument('--seed', type=int, default=42)
    t.set_defaults(func=train)
    s = sub.add_parser('select')
    s.add_argument('--manifest', type=Path, required=True)
    s.add_argument('--models', type=Path, default=Path('artifacts/candidates'))
    s.add_argument('--out', type=Path, default=Path('artifacts/selected'))
    s.add_argument('--min-retention', type=float, default=.98)
    s.add_argument('--min-coverage', type=float, default=.95)
    s.add_argument('--max-background', type=float, default=.60)
    s.add_argument('--min-packet-score', type=float, default=.90)
    s.set_defaults(func=select)
    for name in ('evaluate', 'export'):
        e = sub.add_parser(name)
        e.add_argument('--model', type=Path, required=True)
        e.add_argument('--config', type=Path, required=True)
        e.add_argument('--out', type=Path, required=True)
        if name == 'evaluate':
            e.add_argument('--manifest', type=Path, required=True)
            e.add_argument('--split', choices=['validation','test'], default='test')
            e.set_defaults(func=evaluate)
        else:
            e.set_defaults(func=export)
    pack=sub.add_parser('prepare')
    pack.add_argument('--manifest',type=Path,required=True)
    pack.add_argument('--source',choices=['labels','detector'],default='labels')
    pack.add_argument('--model',type=Path)
    pack.add_argument('--config',type=Path)
    pack.add_argument('--format',choices=['csv','npz'],default='csv')
    pack.add_argument('--split',choices=['train','validation','test'],default='train')
    pack.add_argument('--steps',type=int,default=100)
    pack.add_argument('--out',type=Path,required=True)
    pack.set_defaults(func=prepare)
    args = p.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
