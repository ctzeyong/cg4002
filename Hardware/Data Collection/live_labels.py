"""Manual console annotations on the latest received sensor timeline."""
import json
import time

from core import RAW_PERIOD_US


class ConsoleKeys:
    """Windows terminal keys without blocking serial collection."""
    def __init__(self):
        import msvcrt
        self.console = msvcrt
        self.last = ('', 0.0)

    def poll(self):
        if not self.console.kbhit():
            return None
        key = self.console.getwch().lower()
        if key in ('\x00', '\xe0'):
            self.console.getwch()
            return None
        now = time.monotonic()
        previous, when = self.last
        self.last = (key, now)
        # Suppress held-key repeats; tap and release each control.
        return None if key == previous and now - when < .3 else key


class LiveLabels:
    def __init__(self, path, symbol, variant):
        self.path = path
        self.symbol = symbol
        self.variant = variant
        self.latest = None
        self.active = None
        self.save_pending = False
        self.doc = dict(reviewed=False, annotation_method='live_keyboard',
                        boundary_basis='latest received sensor sample; USB latency not corrected',
                        intervals=[])

    def progress(self):
        completed = [a for a in self.doc['intervals'] if a is not self.active
                     and a['end_us'] > a['start_us']]
        ignored = sum(a['label'] == 'ignore' for a in completed)
        active = ('ignore' if self.active['label'] == 'ignore' else self.symbol) if self.active else 'none'
        return (f'Labelled gestures: {len(completed) - ignored}; '
                f'ignored attempts: {ignored}; in progress: {active}')

    def save(self):
        # Persist an open attempt as ignore, so a crash cannot create a valid
        # half-symbol. The in-memory label remains selected until normal end.
        intervals = []
        for original in self.doc['intervals']:
            item = dict(original)
            if original is self.active:
                item.update(label='ignore', ignore_reason='unfinished',
                            end_us=self.latest + RAW_PERIOD_US)
            if item['end_us'] > item['start_us']:
                intervals.append(item)
        temp = self.path.with_suffix('.json.tmp')
        temp.write_text(json.dumps(dict(self.doc, intervals=intervals), indent=2), encoding='utf-8')
        try:
            temp.replace(self.path)
        except PermissionError:
            # Keep the complete temporary snapshot and retry on the next save.
            # Never report confirmation when the main labels file is stale.
            self.doc['reviewed'] = False
            temp.write_text(json.dumps(dict(self.doc, intervals=intervals), indent=2), encoding='utf-8')
            if not self.save_pending:
                print(f'WARNING: Windows blocked label replacement. Latest draft retained at {temp}. '
                      'Recording continues; retrying on the next save. Close any program holding the labels file.')
            self.save_pending = True
            return
        if self.save_pending:
            print(f'Label saving recovered: {self.path}')
        self.save_pending = False

    def sample(self, timestamp, gap=False):
        self.latest = timestamp
        if gap and self.active is not None:
            self.active.update(label='ignore', ignore_reason='sensor_gap')
            self.save()

    def key(self, key):
        if self.latest is None:
            return 'Waiting for the first valid sensor sample.'
        boundary = self.latest + RAW_PERIOD_US
        if key == ' ':
            if self.active is None:
                self.active = dict(label=self.symbol, start_us=boundary, end_us=boundary,
                                   **self.variant)
                self.doc['intervals'].append(self.active)
                message = f'START {self.symbol}: draw now.'
            else:
                if boundary <= self.active['start_us']:
                    return 'Wait for more sensor samples before ending.'
                self.active['end_us'] = boundary
                message = f'END {self.active["label"]}. Reposition before the next start.'
                self.active = None
        elif key == 'd':
            target = self.active if self.active is not None else (
                self.doc['intervals'][-1] if self.doc['intervals'] else None)
            if target is None:
                return 'No gesture to discard.'
            target.update(label='ignore', ignore_reason='discarded')
            message = ('DISCARD: keep the attempt open; press Space when finished.'
                       if self.active is not None else 'Last gesture marked ignore.')
        else:
            return None
        self.doc['reviewed'] = False
        self.save()
        return f'{message}\n{self.progress()}'

    def finish(self):
        if self.active is not None:
            self.active.update(label='ignore', ignore_reason='unfinished',
                               end_us=self.latest + RAW_PERIOD_US)
            if self.active['end_us'] <= self.active['start_us']:
                self.doc['intervals'].remove(self.active)
            self.active = None
        self.save()
