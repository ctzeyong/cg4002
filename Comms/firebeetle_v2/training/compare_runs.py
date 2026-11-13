"""Compare fixed BLE holdouts; never choose/tune models using the test results."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load(run, name):
    return json.loads((ROOT/run/name).read_text())


def main():
    runs = ('ble_comparison', 'combined_500hz')
    manifests = [load(r, 'manifest.json') for r in runs]
    # Both runs must evaluate the same CSV contents, labels and metadata.
    for split in ('validation', 'test'):
        signatures = [{(a['csv'], a['sha256'], a['labels_sha256'], a['metadata_sha256'])
                       for a in m['recordings'] if a.get('split') == split} for m in manifests]
        if not signatures[0] or signatures[0] != signatures[1]:
            raise ValueError(f'The {split} recordings differ between runs')
    results = {}
    for run in runs:
        model = load(run, 'model.json')
        results[run] = {'kind': model['kind'], 'nodes': sum(len(t['feature']) for t in model['trees']),
                        'start_threshold': model['start'], 'stop_threshold': model['stop']}
        for split, report in (('validation', model['validation']), ('test', load(run, 'test_report.json'))):
            results[run][split] = {k: v for k, v in report.items() if k not in ('recordings', 'per_class')}
            results[run][split]['gestures'] = sum(a['gestures'] for a in report['per_class'].values())
            results[run][split]['detected'] = sum(a['detected'] for a in report['per_class'].values())
    result = {'same_holdout_files_and_annotations': True,
              'validation_participant': 'juven', 'test_participant': 'jermaine', 'runs': results}
    (ROOT/'comparison.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
