"""Validation-only comparison of dedicated-noise weights; test only the winner.

Leaves the active firmware/model untouched. Run from any directory with Python.
"""
import hashlib
import json
from pathlib import Path
import shutil
import numpy as np
from sklearn.tree import DecisionTreeClassifier
from sklearn.ensemble import RandomForestClassifier
import pipeline as p

NOISE_FILE = 'yuwen_day4_noise_air_front_cw_standing_2.csv'
WEIGHTS = (1.0, 1.5, 2.0, 3.0)
OUT = p.ROOT/'training/noise_weight_comparison'


def select(candidates):
    recall = max(m['validation']['useful_sample_recall'] for m in candidates)
    eligible = [m for m in candidates if m['validation']['useful_sample_recall'] >= recall-.01]
    detection = max(m['validation']['gesture_detection_rate'] for m in eligible)
    eligible = [m for m in eligible if m['validation']['gesture_detection_rate'] >= detection-.01]
    return min(eligible, key=lambda m: (m['validation']['background_active_fraction'], len(m['trees'])))


def main():
    OUT.mkdir(exist_ok=True)
    active_header = (p.ROOT/'model_data.h').read_bytes()
    p.OUT = OUT
    rows = p.load_sessions(p.ROOT.parents[2]/'sessions/ble')
    train = [r for r in rows if r['item']['split'] == 'train']
    val = [r for r in rows if r['item']['split'] == 'validation']
    test = [r for r in rows if r['item']['split'] == 'test']
    x = np.concatenate([r['features'] for r in train])
    y = np.concatenate([r['y'][r['indices']] for r in train])
    noise = np.concatenate([np.full(len(r['indices']), r['item']['csv'] == NOISE_FILE) for r in train])
    assert noise.sum() == 1705 and not y[noise].any(), 'Unexpected dedicated noise data'
    baseline = json.loads((p.ROOT/'training/ble_juven_val_ty_test/model.json').read_text())
    # Abort if the baseline dataset has changed: this is a controlled weight comparison.
    assert hashlib.sha256((OUT/'manifest.json').read_bytes()).hexdigest() == baseline['training']['manifest_sha256']
    policy = dict(overall_recall_max_drop=.01, per_gesture_sample_recall_max_drop=.02,
                  gesture_detection_rate_max_drop=.01, background_min_reduction=.02,
                  selection_data='Juven validation only', test_data='TY; selected weight only')
    p.save('policy.json', policy)
    summaries, winners = [], []
    for weight in WEIGHTS:
        sample_weight = np.where(noise, weight, 1.0)
        estimators = {
            'tree': DecisionTreeClassifier(max_depth=6,min_samples_leaf=20,class_weight='balanced',random_state=16),
            'forest': RandomForestClassifier(n_estimators=8,max_depth=6,min_samples_leaf=20,
                        class_weight='balanced',random_state=16,n_jobs=1)}
        trials, candidates = [], []
        for name, estimator in estimators.items():
            estimator.fit(x,y,sample_weight=sample_weight)
            trees = p.convert(estimator)
            np.testing.assert_allclose(p.predict(trees,x),estimator.predict_proba(x)[:,1],atol=2e-7,rtol=0)
            scores = [p.predict(trees,r['features']) for r in val]
            for start in (.20,.30,.40,.50,.60,.70):
                for stop in (.10,.20,.30,.40,.50,.60):
                    if stop >= start: continue
                    report = p.metrics(val,scores,start,stop)
                    trials.append(dict(kind=name,start=start,stop=stop,report=report))
                    candidates.append(dict(kind=name,start=start,stop=stop,trees=trees,validation=report))
            print(f'Weight {weight:g}: {name} validation complete',flush=True)
        chosen = select(candidates)
        chosen['training'] = dict(seed=16,windows=len(y),positive_windows=int(y.sum()),
            input='selected int16 counts at 100 Hz',window=30,hop=5,
            manifest_sha256=hashlib.sha256((OUT/'manifest.json').read_bytes()).hexdigest(),
            noise_file=NOISE_FILE,noise_windows=int(noise.sum()),noise_multiplier=weight)
        prefix = f'weight_{weight:g}'
        p.save(prefix+'_model.json', chosen)
        p.save(prefix+'_selection_report.json', trials)
        p.export(chosen, OUT/(prefix+'_model_data.h'))
        if weight == 1:
            assert all(chosen[k] == baseline[k] for k in ('kind','start','stop','trees','validation')), '1x failed to reproduce baseline'
        summaries.append(dict(weight=weight,kind=chosen['kind'],start=chosen['start'],stop=chosen['stop'],
            nodes=sum(len(t['feature']) for t in chosen['trees']),validation=chosen['validation']))
        winners.append(chosen)
        print(f'Weight {weight:g}: recall={chosen["validation"]["useful_sample_recall"]:.4f}, '
              f'background={chosen["validation"]["background_active_fraction"]:.4f}',flush=True)
    base = winners[0]['validation']
    acceptable = [0]
    for i,m in enumerate(winners[1:],1):
        v = m['validation']
        checks = dict(
            overall_recall=v['useful_sample_recall'] >= base['useful_sample_recall']-.01,
            per_gesture_recall=all(v['per_class'][g]['sample_recall'] >= base['per_class'][g]['sample_recall']-.02 for g in p.GESTURES),
            gesture_detection=v['gesture_detection_rate'] >= base['gesture_detection_rate']-.01,
            background_reduction=v['background_active_fraction'] <= base['background_active_fraction']-.02)
        summaries[i]['acceptance_checks'] = checks
        if all(checks.values()): acceptable.append(i)
    best = min(acceptable,key=lambda i:winners[i]['validation']['background_active_fraction'])
    chosen = winners[best]
    p.save('summary.json',dict(policy=policy,results=summaries,selected_weight=WEIGHTS[best],
            active_header_modified=False,baseline_reproduced=True))
    # Test is evaluated only after fixing the chosen weight using validation.
    report = p.metrics(test,[p.predict(chosen['trees'],r['features']) for r in test],chosen['start'],chosen['stop'])
    p.save('selected_test_report.json',report)
    p.save('model.json',chosen)
    p.export(chosen,OUT/'model_data.h')
    if best:
        try:
            p.export(chosen)
            p.verify_cpp(rows,chosen,'g++')
        finally:
            (p.ROOT/'model_data.h').write_bytes(active_header)
    else:
        shutil.copy2(p.ROOT/'training/ble_juven_val_ty_test/cpp_verification.json',OUT/'cpp_verification.json')
    assert (p.ROOT/'model_data.h').read_bytes() == active_header
    shutil.copy2(Path(__file__),OUT/'compare_noise_weights.py')
    print('SELECTED WEIGHT',WEIGHTS[best],flush=True)
    print('TEST',json.dumps({k:v for k,v in report.items() if k not in ('per_class','recordings')}),flush=True)


if __name__ == '__main__':
    main()
