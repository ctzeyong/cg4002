"""Record all 500 Hz IMU samples over USB for labelling. No Bluetooth sender."""
import argparse
import csv
import json
from pathlib import Path
import time
from core import CHANNELS, SCHEMA, RAW_HZ, RAW_PERIOD_US, GESTURES
from live_labels import ConsoleKeys, LiveLabels


def parse_line(line):
    fields = line.strip().split(',')
    if fields[0] in ('STATUS','READY','GAP','ERROR'):
        return None
    if fields[0] != 'RAW500' or len(fields) != 9:
        raise ValueError('Unrecognized or damaged RAW500 row')
    seq, t = int(fields[1]), int(fields[2])
    counts = [int(v) for v in fields[3:]]
    if seq < 0 or seq > 0xffffffff or t < 0 or any(v < -32768 or v > 32767 for v in counts):
        raise ValueError('Invalid sequence, timestamp or raw int16 readings')
    x = [v/(8192.0 if c<3 else 65.5) for c,v in enumerate(counts)]
    return [seq, t, *x]


def check_firmware(line, received_samples):
    if line.startswith(('DATA,','RAW200,','RAW1000,')):
        raise RuntimeError('Old firmware detected. Upload the 500 Hz recording sketch and use a new filename.')
    if line.startswith('READY,'):
        fields=line.split(',')
        if len(fields)<3 or fields[1]!=SCHEMA or 'raw_hz=500' not in fields:
            raise RuntimeError(f'Firmware format/rate mismatch. Expected {SCHEMA}, 500 Hz. Received: {line!r}')
        if received_samples:
            raise RuntimeError(f'ESP startup banner received after data: likely device restart. Start a new file. Row: {line!r}')
    if line.startswith('ERROR,'):
        raise RuntimeError(f'Firmware reported an error: {line}')


def check_progress(previous, row, line, sensor_gap=False):
    if previous is None:
        return False
    ds, dt = row[0]-previous[0], row[1]-previous[1]
    if ds<=0 or dt<=0:
        raise RuntimeError(
            'Sequence/timestamp moved backwards or repeated: possible ESP reboot or serial corruption '
            '(this alone does not prove a reboot). '
            f'Previous seq/time={previous}; received={row[:2]}; offending row={line!r}. Start a new file.')
    if not sensor_gap and dt != ds*RAW_PERIOD_US:
        raise RuntimeError(
            'Inconsistent sequence/timestamp progression: possible serial corruption or unreported sensor reset. '
            f'Expected {ds*RAW_PERIOD_US} us for {ds} samples, received {dt} us; '
            f'previous={previous}; offending row={line!r}. Start a new file.')
    return sensor_gap or ds!=1 or dt!=RAW_PERIOD_US


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--participant', required=True)
    p.add_argument('--session', required=True)
    p.add_argument('--notes', default='')
    p.add_argument('--live-labels', action='store_true', help='Windows terminal: Space start/end, D discard, Q finish')
    p.add_argument('--symbol', choices=GESTURES)
    p.add_argument('--drawing-mode', choices=('table', 'air_front'), default='table')
    p.add_argument('--direction', choices=('cw', 'ccw', 'na'), default='na')
    p.add_argument('--posture', choices=('seated', 'standing'), default='seated')
    args = p.parse_args()
    if args.live_labels and not args.symbol:
        p.error('--live-labels requires --symbol')
    keys = None
    if args.live_labels:
        import os
        import sys
        if os.name != 'nt' or not sys.stdin.isatty():
            p.error('Live keys require a Windows terminal (for example Anaconda Prompt)')
        keys = ConsoleKeys()
    import serial
    meta_path = args.out.with_suffix('.meta.json')
    labels_path = args.out.with_suffix('.labels.json')
    if args.out.exists() or meta_path.exists() or labels_path.exists():
        p.error('Choose a new recording filename; no recordings are overwritten')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    meta = dict(schema=SCHEMA, raw_hz=RAW_HZ, default_output_steps=100, dlpf=3,
                accel_range_g=4, gyro_range_dps=500,
                participant=args.participant, session=args.session, notes=args.notes,
                started_unix=time.time(), channels=CHANNELS,
                timestamp_basis='nominal 500 Hz sensor FIFO grid, not USB arrival time')
    previous = None
    count = gaps = malformed = 0
    sensor_gap=False
    stop_reason=None
    bad_rows=[]
    live = LiveLabels(labels_path, args.symbol, dict(drawing_mode=args.drawing_mode,
                      direction=args.direction, posture=args.posture)) if keys else None
    normal_finish = False
    with serial.Serial(args.port, 921600, timeout=1) as dev, args.out.open('x', newline='') as f:
        meta_path.write_text(json.dumps(meta, indent=2))
        writer = csv.writer(f)
        writer.writerow(['sequence', 'esp_time_us', *CHANNELS, 'host_rx_unix_ns'])
        print('Recording continuously at 500 Hz. Draw ONE symbol then briefly decelerate/pause. Ctrl+C to finish.')
        if live:
            live.save()
            print(f'Live labels: {args.symbol}, {args.drawing_mode}, {args.direction}, {args.posture}.')
            print('Tap SPACE start/end; D discard active/last attempt; Q finish. Keep this terminal focused.')
        try:
            while True:
                if keys:
                    key = keys.poll()
                    if key == 'q':
                        normal_finish = True
                        break
                    if key:
                        message = live.key(key)
                        if message: print(message)
                line = dev.readline(256).decode('ascii', errors='replace').strip()
                if not line:
                    continue
                check_firmware(line,count)
                try:
                    row = parse_line(line)
                except ValueError:
                    malformed += 1
                    if len(bad_rows)<10:
                        bad_rows.append(line[:250])
                        print(f'Malformed serial row skipped: {line[:250]!r}')
                    continue
                if row is None:
                    if line.startswith('GAP,'): sensor_gap=True
                    print(line[:250])
                    continue
                gap = check_progress(previous,row,line,sensor_gap)
                if gap:
                    gaps += 1
                    print(f'Data gap at sensor time {row[1]/1e6:.3f} s; do not annotate across it.')
                writer.writerow([*row, time.time_ns()])
                if live: live.sample(row[1], gap)
                previous = row[:2]
                sensor_gap=False
                count += 1
                if count % RAW_HZ == 0:
                    f.flush()
                    if live: live.save()
                if count % (10*RAW_HZ) == 0:
                    print(f'{count} samples; {gaps} gaps; {malformed} malformed rows')
                    if live: print(live.progress())
        except KeyboardInterrupt:
            pass
        except (RuntimeError, serial.SerialException, OSError) as e:
            stop_reason=str(e)
            print(f'Recording stopped: {stop_reason}')
        finally:
            if live:
                try:
                    live.finish()
                except OSError as e:
                    stop_reason = f'Could not save final labels: {e}'
                    print(stop_reason)
            meta.update(samples=count, gaps=gaps, malformed_rows=malformed,
                        malformed_examples=bad_rows,stop_reason=stop_reason,finished_unix=time.time())
            meta_path.write_text(json.dumps(meta, indent=2))
    if live and normal_finish and count and not stop_reason:
        print('Unlabelled time becomes BACKGROUND. Every symbol attempt must be labelled or ignored.')
        try:
            confirmed = input('Did you mark every attempt, discard mistakes, and capture usable boundaries? [y/N] ')
        except (EOFError, KeyboardInterrupt):
            confirmed = ''
        live.doc['reviewed'] = confirmed.strip().lower() == 'y'
        live.save()
    print(f'Saved {count} samples to {args.out}.')
    if live:
        print(live.progress())
        if live.save_pending:
            print(f'Latest labels remain in {labels_path.with_suffix(".json.tmp")}. '
                  'Preserve this file; resolve the file lock and recover it before annotation/training.')
    if live and live.doc['reviewed']:
        print(f'Confirmed labels saved to {labels_path}. Ready for manifest and cloud preparation.')
    else:
        print(f'Next: python annotate.py "{args.out}" (live labels, if any, remain a draft).')
    if stop_reason: raise SystemExit(1)


if __name__ == '__main__': main()
