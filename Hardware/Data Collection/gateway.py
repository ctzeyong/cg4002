"""Receive ESP event chunks, save a durable outbox, optionally POST to HTTPS.

No cloud endpoint is assumed. The cloud must support event_id/Idempotency-Key
deduplication because an acknowledged request can still be retried after a crash.
"""
import argparse
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.request
from core import GESTURES, CHANNELS


def validate(packet):
    if packet.get('schema') != 1 or not re.fullmatch(r'[0-9a-f]{16}-[0-9]+', packet.get('event_id', '')):
        raise ValueError('Invalid schema or event ID')
    if packet.get('class_hint') not in GESTURES or packet.get('channels') != list(CHANNELS):
        raise ValueError('Invalid gesture/channels')
    a, b = int(packet['start_us']), int(packet['end_us'])
    if not 300000 <= b-a <= 6000000 or not 0 <= packet['score'] <= 1:
        raise ValueError('Invalid duration/score')
    samples = packet['samples']
    if not 1 <= len(samples) <= 640:
        raise ValueError('Invalid sample count')
    previous = None
    import math
    for row in samples:
        if len(row) != 8 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in row):
            raise ValueError('Invalid sample row')
        if previous is not None and not 5000 <= row[0]-previous <= 15000:
            raise ValueError('Data gap in event')
        previous = row[0]
        if row[7] != int(a <= row[0] < b):
            raise ValueError('Gesture mask does not match boundaries')
    if samples[0][0] > a or samples[-1][0] < b-15000:
        raise ValueError('Truncated gesture')
    return packet


def spool(packet, outbox):
    validate(packet)
    target = outbox/(packet['event_id']+'.json')
    if (outbox/'sent'/target.name).exists():
        return outbox/'sent'/target.name
    if target.exists():
        return target
    temp = target.with_suffix('.tmp')
    with temp.open('w') as f:
        json.dump(packet, f, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(target)
    return target


def upload_loop(outbox, endpoint, token_env, stop):
    sent = outbox/'sent'
    sent.mkdir(exist_ok=True)
    while not stop.is_set():
        pending = sorted(outbox.glob('*.json'))
        failed = False
        for path in pending:
            try:
                headers = {'Content-Type': 'application/json', 'Idempotency-Key': path.stem}
                token = os.environ.get(token_env)
                if token:
                    headers['Authorization'] = 'Bearer '+token
                request = urllib.request.Request(endpoint, data=path.read_bytes(), headers=headers, method='POST')
                with urllib.request.urlopen(request, timeout=10) as response:
                    if not 200 <= response.status < 300:
                        raise RuntimeError(f'HTTP {response.status}')
                path.replace(sent/path.name)
                print(f'Uploaded {path.stem}', flush=True)
            except Exception as e:
                print(f'Upload pending: {type(e).__name__}. Files retained in outbox.', flush=True)
                failed = True
                break
        stop.wait(5 if failed else 1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', help='Omit to retry previously spooled packets only')
    p.add_argument('--outbox', type=Path, default=Path('outbox'))
    p.add_argument('--endpoint', help='Your real HTTPS cloud ingestion URL; omitted = save locally')
    p.add_argument('--token-env', default='GESTURE_CLOUD_TOKEN')
    args = p.parse_args()
    if not args.port and not args.endpoint:
        p.error('Supply --port or --endpoint')
    if args.endpoint and not args.endpoint.startswith('https://'):
        p.error('The ingestion endpoint must use HTTPS')
    args.outbox.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    if args.endpoint:
        threading.Thread(target=upload_loop, args=(args.outbox, args.endpoint, args.token_env, stop), daemon=True).start()
    try:
        if args.port:
            import serial
            with serial.Serial(args.port, 921600, timeout=1) as dev:
                buffer = bytearray()
                while True:
                    buffer.extend(dev.read(max(1, min(dev.in_waiting, 8192))))
                    while b'\n' in buffer:
                        line, _, buffer = buffer.partition(b'\n')
                        if not line.startswith(b'{'):
                            print(line.decode(errors='replace')[:300])
                            continue
                        try:
                            packet = json.loads(line)
                            path = spool(packet, args.outbox)
                            print(f'Saved {path.name}: {packet["class_hint"]}', flush=True)
                        except (ValueError, KeyError, TypeError) as e:
                            print(f'Rejected malformed event: {e}', flush=True)
                    if len(buffer) > 131072:
                        buffer.clear()
                        print('Oversized/incomplete UART frame discarded', flush=True)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
        print('Stopped. Unsent events remain in the outbox.')


if __name__ == '__main__':
    main()
