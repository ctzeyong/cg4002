"""Offline cloud datasets. Raw recordings are never modified; no transport code."""
import csv
import json
from pathlib import Path
import numpy as np
from core import RAW_HZ, RAW_PERIOD_US, SCHEMA, load_model, validate_config, replay
from segments import resample_steps, PROCESSING_ID

COUNT_CHANNELS = ['ax_counts', 'ay_counts', 'az_counts', 'gx_counts', 'gy_counts', 'gz_counts']
COUNT_SCALES = np.array([8192, 8192, 8192, 65.5, 65.5, 65.5], dtype=np.float64)
ENCODING_ID = 'imu-counts-int16-v1'


def quantize_counts(frames):
    """Quantize AFTER resampling: nearest, ties away from zero, then saturate.

    Scale in float64 to make the rounding contract explicit for transport code.
    Returned arrays can be serialized in row-major order as little-endian int16.
    """
    values = np.asarray(frames, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 6 or not np.isfinite(values).all():
        raise ValueError('Expected finite frames with shape (steps, 6)')
    counts = values * COUNT_SCALES
    rounded = np.copysign(np.floor(np.abs(counts) + 0.5), counts)
    clipped_values = int(np.count_nonzero((rounded < -32768) | (rounded > 32767)))
    return np.clip(rounded, -32768, 32767).astype('<i2'), clipped_values


def prepare_examples(args, session_loader):
    if not 32 <= args.steps <= 512:
        raise ValueError('steps must be between 32 and 512')
    source, fmt = args.source, args.format
    if source == 'detector' and (not args.model or not args.config):
        raise ValueError('--source detector requires --model and --config')
    if source == 'labels' and (args.model or args.config):
        raise ValueError('Manual-label export needs no model/config; use --source detector for replay')
    model = load_model(args.model) if source == 'detector' else None
    config = validate_config(json.loads(args.config.read_text())) if model else None
    rows = session_loader(args.manifest)
    if not any(e['split'] == args.split for e,*_ in rows):
        raise ValueError(f'No {args.split} recordings')
    if args.out.exists() and any(args.out.iterdir()):
        raise ValueError('Output directory must be empty; use a new folder to avoid stale examples')
    args.out.mkdir(parents=True, exist_ok=True)
    index, skipped = [], []
    for sid,(entry,seq,times,x,annotations) in enumerate(rows):
        if entry['split'] != args.split: continue
        truth = [a for a in annotations if a['label'] != 'ignore']
        ignored = [a for a in annotations if a['label'] == 'ignore']
        if source == 'labels':
            events = [dict(a, reason='manual', trigger_us='', pretrigger_us=0) for a in truth]
        else:
            events = replay(seq,times,x,model,config)['completed']
        path = (args.manifest.parent/entry['csv']).resolve()
        meta = json.loads(path.with_suffix('.meta.json').read_text())
        for i,event in enumerate(events):
            a,b = event['start_us'],event['end_us']
            reason = None
            mask = (times>=a)&(times<b)
            tt, ss, raw = times[mask], seq[mask], x[mask]
            if any(a<g['end_us'] and b>g['start_us'] for g in ignored): reason='touches_ignore'
            elif len(raw)<2: reason='fewer_than_two_readings'
            elif np.any(np.diff(tt)!=RAW_PERIOD_US) or np.any(np.diff(ss)!=1): reason='sensor_gap'
            if reason:
                skipped.append(dict(source_csv=entry['csv'],start_us=a,end_us=b,reason=reason))
                continue
            frames, clipped_values = quantize_counts(resample_steps(raw,args.steps))
            variant = event if source == 'labels' else {}
            if source == 'labels': label=event['label']
            else:
                overlaps=[g for g in truth if max(0,min(b,g['end_us'])-max(a,g['start_us'])) >= .2*(g['end_us']-g['start_us'])]
                label=overlaps[0]['label'] if len(overlaps)==1 else ('background' if not overlaps else 'ambiguous')
                variant=overlaps[0] if len(overlaps)==1 else {}
            name=f's{sid:03d}_gesture{i:04d}.{fmt}'
            grid=np.linspace(0,(int(tt[-1])-int(tt[0]))/1e6,args.steps)
            if fmt=='csv':
                with (args.out/name).open('w',newline='',encoding='utf-8') as f:
                    writer=csv.writer(f)
                    writer.writerow(['step','time_from_start_s',*COUNT_CHANNELS])
                    writer.writerows([j,format(grid[j],'.9f'),*[int(v) for v in row]] for j,row in enumerate(frames))
            else: np.savez(args.out/name,frames=frames)
            index.append(dict(file=name,label=label,source=source,split=args.split,
                drawing_mode=variant.get('drawing_mode',''),direction=variant.get('direction',''),
                posture=variant.get('posture',''),
                source_csv=entry['csv'],participant=meta.get('participant',''),session=meta.get('session',''),
                group=entry['group'],steps=args.steps,raw_count=len(raw),
                start_us=int(tt[0]),end_us=int(tt[-1])+RAW_PERIOD_US,duration_us=len(raw)*RAW_PERIOD_US,
                raw_hz=RAW_HZ,schema=SCHEMA,
                annotation_start_us=a,annotation_end_us=b,grid_step_us=(int(tt[-1])-int(tt[0]))/(args.steps-1),
                trigger_us=event['trigger_us'],pretrigger_us=event['pretrigger_us'],
                reason=event['reason'],processing=PROCESSING_ID,
                encoding=ENCODING_ID,dtype='int16',accel_counts_per_g=8192,
                gyro_counts_per_dps=65.5,rounding='nearest_ties_away_from_zero',
                clipped_values=clipped_values))
    fields=list(index[0]) if index else ['file','label','source','split','source_csv']
    with (args.out/'index.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=fields); writer.writeheader(); writer.writerows(index)
    (args.out/'index.json').write_text(json.dumps(index,indent=2))
    (args.out/'skipped.json').write_text(json.dumps(skipped,indent=2))
    (args.out/'CLOUD_README.txt').write_text(
        'Cloud gesture dataset: imu-counts-int16-v1\n\n'
        'Read index.csv or index.json to map each file to its label and split.\n'
        'CSV input columns, in order: ax_counts, ay_counts, az_counts, gx_counts, gy_counts, gz_counts.\n'
        'Read these six columns as signed int16. Exclude step and time_from_start_s from model input.\n'
        'NPZ exports contain an int16 array named frames. Each example has shape (steps, 6).\n'
        'Values have already been filtered and resampled; do not resample again.\n'
        'Encoding: acceleration 8192 counts/g; gyro 65.5 counts/(degree/s).\n'
        'After resampling: scale in float64, round nearest (ties away from zero),\n'
        'clamp to [-32768, 32767], then cast to int16. Index clipped_values counts saturation.\n'
        'Bluetooth contract: signed little-endian int16, row-major ax,ay,az,gx,gy,gz.\n'
        '100 steps = 1200 binary payload bytes, excluding headers. CSV itself is text, not a wire packet.\n'
        'The firmware transport must implement this encoding; the current capture buffer is float32.\n'
        'Cloud can cast counts to float32 for training or decode to physical units first.\n'
        'Use identical decoding and normalization at inference; fit normalization on training data only.\n'
        'Keep collection groups/days separate across train, validation and test.\n'
        'Manual-label exports contain clean labelled gestures, not background rejection examples.\n',
        encoding='utf-8')
    print(f'Saved {len(index)} {source} examples to {args.out}; skipped {len(skipped)}. Raw CSV files unchanged.')

