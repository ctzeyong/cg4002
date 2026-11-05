"""Label a block recording (many repeats of one gesture, with pauses) after the fact.

Record without --live-labels, then:

    python annotate_blocks.py sessions/ble/yuwen_day1_star_air_front_cw_seated.csv

Gestures are proposed automatically from the motion energy (the pauses separate them); you
review and fix them. Writes the same <name>.labels.json as record.py / annotate.py.

Mouse:  wheel zoom (time axis) | drag pan | drag an interval's edge to move that boundary
        Shift+drag to draw a new interval | click an interval to select it
Keys:   Left/Right previous/next interval | Tab next suspicious (odd length) | Del delete
        I toggle ignore | M merge with next | Z zoom to selected | F fit all
        D re-detect | Ctrl+Z undo | Ctrl+S save draft | Ctrl+Shift+S save reviewed
"""
import argparse
import collections
import json
from pathlib import Path
import re
import sys

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from annotate import VARIANT_KEYS, recording_period
from core import GESTURES, labels_for, load_csv

LABELS = (*GESTURES, 'ignore')
GRAVITY_WINDOW_S = 1.0      # moving mean removed from the accelerometer before taking energy
SMOOTH_S = 0.1
SUSPICIOUS = (0.6, 1.5)     # intervals outside this fraction of the median length are flagged
QT = QtCore.Qt


def from_filename(path):
    """symbol/drawing_mode/direction/posture from record.py's default name
    <participant>_<session>_<symbol>_<mode>_<direction>_<posture>[_2].csv, else {}."""
    pattern = rf'_({"|".join(GESTURES)})_(table|air_front)_(cw|ccw|na)_(seated|standing)(_\d+)?$'
    match = re.search(pattern, Path(path).stem)
    return dict(zip(('symbol', *VARIANT_KEYS), match.groups()[:4])) if match else {}


def smooth(v, n):
    n = max(1, int(n))
    return np.convolve(v, np.ones(n) / n, mode='same')


def motion_energy(x, hz):
    """Unitless motion level per sample: about 0.5 at rest, several units mid-gesture.
    Gyro magnitude (bias removed) /100 dps + gravity-removed accel magnitude /0.25 g."""
    gyro = x[:, 3:] - np.median(x[:, 3:], axis=0)
    accel = x[:, :3] - np.stack([smooth(x[:, i], GRAVITY_WINDOW_S * hz) for i in range(3)], axis=1)
    return smooth(np.linalg.norm(gyro, axis=1) / 100 + np.linalg.norm(accel, axis=1) / 0.25, SMOOTH_S * hz)


def default_threshold(energy):
    rest = np.percentile(energy, 20)
    return float(max(4 * rest, 0.6 * np.percentile(energy, 90)))


def detect(energy, hz, threshold, merge_gap_s, min_len_s, pad_s):
    """[(first, last+1)] sample ranges: motion above threshold/2 that peaks above threshold,
    with pauses shorter than merge_gap_s bridged (corners of a star, for example)."""
    on = energy > threshold / 2
    edges = np.flatnonzero(np.diff(np.r_[0, on.astype(np.int8), 0]))
    merged = []
    for a, b in zip(edges[::2], edges[1::2]):
        if energy[a:b].max() <= threshold:
            continue
        if merged and a - merged[-1][1] < merge_gap_s * hz:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    pad = int(round(pad_s * hz))
    return [(max(0, a - pad), min(len(energy), b + pad)) for a, b in merged if b - a >= min_len_s * hz]


class EdgeRegion(pg.LinearRegionItem):
    """Only the edges drag; dragging the body pans the view like empty space does."""
    def mouseDragEvent(self, ev):
        ev.ignore()

    def hoverEvent(self, ev):
        self.setMouseHover(False)


class LabelViewBox(pg.ViewBox):
    """Time-only zoom/pan; click selects, Shift+drag draws a new interval."""
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setMouseEnabled(x=True, y=False)
        self.setMenuEnabled(False)

    def mouseClickEvent(self, ev):
        if ev.button() == QT.MouseButton.LeftButton:
            ev.accept()
            self.app.click(self.mapSceneToView(ev.scenePos()).x())
        else:
            super().mouseClickEvent(ev)

    def mouseDragEvent(self, ev, axis=None):
        if ev.button() == QT.MouseButton.LeftButton and ev.modifiers() & QT.KeyboardModifier.ShiftModifier:
            ev.accept()
            a = self.mapSceneToView(ev.buttonDownScenePos()).x()
            b = self.mapSceneToView(ev.scenePos()).x()
            self.app.drawing(min(a, b), max(a, b), ev.isFinish())
        else:
            super().mouseDragEvent(ev, axis)


class BlockAnnotator(QtWidgets.QMainWindow):
    def __init__(self, path):
        super().__init__()
        self.path = Path(path)
        self.period_us, meta = recording_period(path)
        self.meta = {**from_filename(path), **{k: v for k, v in meta.items() if v}}
        self.hz = 1e6 / self.period_us
        self.seq, self.times, self.x = load_csv(path)
        if len(self.times) < 2:
            raise ValueError('Annotation requires at least two samples')
        self.t0 = int(self.times[0])
        self.seconds = (self.times - self.t0) / 1e6
        self.end_s = float(self.seconds[-1]) + self.period_us / 1e6
        gaps = np.flatnonzero((np.diff(self.seq) != 1) | (np.diff(self.times) != self.period_us))
        self.gaps = [(int(self.times[k]), int(self.times[k + 1])) for k in gaps]
        self.energy = motion_energy(self.x, self.hz)

        self.out = self.path.with_suffix('.labels.json')
        self.doc = json.loads(self.out.read_text()) if self.out.exists() else dict(reviewed=False, intervals=[])
        self.intervals = sorted(self.doc['intervals'], key=lambda s: s['start_us'])
        self.symbol = self.default_symbol()
        self.selected = None
        self.undo_stack = []
        self.dirty = False
        self.fresh_detection = False      # True until a detection result is edited by hand
        self.regions = []                 # per interval: one LinearRegionItem per plot
        self.syncing = False
        self.preview = None

        self.build_ui()
        self.plot_data()
        if not self.intervals:
            self.run_detect(confirm=False)
        else:
            self.rebuild()
            self.message(f'Loaded {len(self.intervals)} existing intervals. Press D to re-detect.')
        self.fit_all()

    # ---------- layout ----------
    def build_ui(self):
        self.setWindowTitle(f'{self.path.name} - block annotator')
        self.resize(1500, 900)
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        bar = QtWidgets.QHBoxLayout()
        layout.addLayout(bar)

        self.label_box = QtWidgets.QComboBox()
        self.label_box.addItems(LABELS)
        self.label_box.setCurrentText(self.symbol)
        self.label_box.currentTextChanged.connect(self.set_symbol)
        bar.addWidget(QtWidgets.QLabel('New interval label'))
        bar.addWidget(self.label_box)
        bar.addSpacing(20)

        params = self.doc.get('auto_detect', {})
        def spin(name, value, step, lo, hi, suffix):
            box = QtWidgets.QDoubleSpinBox()
            box.setRange(lo, hi); box.setSingleStep(step); box.setDecimals(2)
            box.setValue(value); box.setSuffix(suffix)
            box.editingFinished.connect(self.params_changed)
            bar.addWidget(QtWidgets.QLabel(name)); bar.addWidget(box)
            return box
        self.threshold = spin('Threshold', params.get('threshold', default_threshold(self.energy)), .1, .05, 100, '')
        self.merge_gap = spin('Merge gaps <', params.get('merge_gap_s', .35), .05, 0, 3, ' s')
        self.min_len = spin('Min length', params.get('min_len_s', .4), .05, 0, 10, ' s')
        self.pad = spin('Pad', params.get('pad_s', .05), .05, 0, 1, ' s')
        detect_button = QtWidgets.QPushButton('Detect (D)')
        detect_button.clicked.connect(lambda: self.run_detect())
        bar.addWidget(detect_button)
        bar.addStretch()
        for text, reviewed in (('Save draft (Ctrl+S)', False), ('Save reviewed (Ctrl+Shift+S)', True)):
            button = QtWidgets.QPushButton(text)
            button.clicked.connect(lambda _, r=reviewed: self.save(r))
            bar.addWidget(button)

        self.graphics = pg.GraphicsLayoutWidget()
        self.graphics.setFocusPolicy(QT.FocusPolicy.StrongFocus)
        layout.addWidget(self.graphics, stretch=1)
        self.plots = []
        for row, name in enumerate(('Acceleration (g)', 'Gyro (deg/s)', 'Motion energy')):
            plot = self.graphics.addPlot(row=row, col=0, viewBox=LabelViewBox(self))
            plot.setLabel('left', name)
            plot.getAxis('left').setWidth(70)     # keep the stacked time axes aligned
            plot.showGrid(x=True, alpha=.15)
            plot.setDownsampling(auto=True, mode='peak')
            plot.setClipToView(True)
            plot.enableAutoRange(y=True)
            plot.setAutoVisible(y=True)
            plot.setLimits(xMin=-1, xMax=self.end_s + 1)
            if self.plots:
                plot.setXLink(self.plots[0])
            self.plots.append(plot)
        self.plots[-1].setLabel('bottom', 'Sensor seconds from first reading')

        self.overview = self.graphics.addPlot(row=3, col=0)
        self.overview.setMaximumHeight(110)
        self.overview.setMouseEnabled(x=False, y=False)
        self.overview.setMenuEnabled(False)
        self.overview.hideAxis('left')
        self.view_region = pg.LinearRegionItem(brush=(80, 80, 200, 50))
        self.view_region.setZValue(10)
        self.overview.addItem(self.view_region)
        self.view_region.sigRegionChanged.connect(self.overview_moved)
        self.plots[0].sigXRangeChanged.connect(self.view_moved)

        self.threshold_line = pg.InfiniteLine(self.threshold.value(), angle=0, movable=True,
                                              pen=pg.mkPen('r', width=1, style=QT.PenStyle.DashLine),
                                              label='threshold (drag)', labelOpts=dict(position=.02, color='r'))
        self.threshold_line.sigPositionChangeFinished.connect(self.threshold_dragged)
        self.plots[2].addItem(self.threshold_line)

        self.setCentralWidget(central)
        self.status = QtWidgets.QLabel()
        self.statusBar().addWidget(self.status, 1)

        shortcuts = {
            'Left': lambda: self.step(-1), 'Right': lambda: self.step(1), 'Tab': self.next_suspicious,
            'Delete': self.delete, 'Backspace': self.delete, 'I': self.toggle_ignore, 'M': self.merge,
            'Z': self.zoom_selected, 'F': self.fit_all, 'D': self.run_detect, 'Ctrl+Z': self.undo,
            'Ctrl+S': lambda: self.save(False), 'Ctrl+Shift+S': lambda: self.save(True),
        }
        for key, action in shortcuts.items():
            QtGui.QShortcut(QtGui.QKeySequence(key), self, action)

    def plot_data(self):
        colors = ('#d62728', '#2ca02c', '#1f77b4')
        for i in range(3):
            self.plots[0].plot(self.seconds, self.x[:, i], pen=pg.mkPen(colors[i], width=1), name='xyz'[i])
            self.plots[1].plot(self.seconds, self.x[:, i + 3], pen=pg.mkPen(colors[i], width=1))
        self.plots[2].plot(self.seconds, self.energy, pen=pg.mkPen('k', width=1))
        self.overview.plot(self.seconds, self.energy, pen=pg.mkPen((120, 120, 120), width=1))
        self.mask_curve = self.overview.plot(pen=None, fillLevel=0, brush=(255, 140, 0, 90))
        for a, b in self.gaps:
            for plot in self.plots:
                plot.addItem(pg.InfiniteLine((a - self.t0) / 1e6, pen=pg.mkPen('m', width=2)))
        if self.gaps:
            print(f'{len(self.gaps)} sensor gaps (magenta lines); gestures across a gap must be ignore.')

    # ---------- view ----------
    def view_moved(self, *_):
        lo, hi = self.plots[0].viewRange()[0]
        self.view_region.blockSignals(True)
        self.view_region.setRegion((lo, hi))
        self.view_region.blockSignals(False)

    def overview_moved(self):
        self.plots[0].setXRange(*self.view_region.getRegion(), padding=0)

    def fit_all(self):
        self.plots[0].setXRange(0, self.end_s, padding=.01)

    def zoom_selected(self):
        if self.selected is None:
            return
        s = self.intervals[self.selected]
        lo, hi = self.to_s(s['start_us']), self.to_s(s['end_us'])
        self.plots[0].setXRange(lo - 1.5, hi + 1.5, padding=0)

    def center_selected(self):
        """Keep the current zoom; pan so the selection is visible."""
        s = self.intervals[self.selected]
        lo, hi = self.plots[0].viewRange()[0]
        a, b = self.to_s(s['start_us']), self.to_s(s['end_us'])
        if b - a > (hi - lo) * .8 or hi - lo > 60:
            self.zoom_selected()
        elif a < lo or b > hi:
            mid = (a + b) / 2
            self.plots[0].setXRange(mid - (hi - lo) / 2, mid + (hi - lo) / 2, padding=0)

    # ---------- time conversion ----------
    def to_s(self, us):
        return (us - self.t0) / 1e6

    def to_us(self, seconds):
        """Snap to the sample grid and clip to the recording."""
        us = self.t0 + int(round(seconds * 1e6 / self.period_us)) * self.period_us
        return int(np.clip(us, self.t0, int(self.times[-1]) + self.period_us))

    # ---------- intervals ----------
    def default_symbol(self):
        symbol = self.meta.get('symbol')
        if symbol not in GESTURES:
            used = collections.Counter(a['label'] for a in self.intervals if a['label'] in GESTURES)
            symbol = used.most_common(1)[0][0] if used else GESTURES[0]
        return symbol

    def variant(self):
        if all(self.meta.get(k) for k in VARIANT_KEYS):
            return {k: self.meta[k] for k in VARIANT_KEYS}
        sets = collections.Counter(tuple(a[k] for k in VARIANT_KEYS) for a in self.intervals
                                   if a['label'] != 'ignore' and all(a.get(k) for k in VARIANT_KEYS))
        return dict(zip(VARIANT_KEYS, sets.most_common(1)[0][0])) if sets else {}

    def make_interval(self, label, start_us, end_us):
        interval = dict(label=label, start_us=start_us, end_us=end_us)
        if label != 'ignore':
            if self.crosses_gap(start_us, end_us):
                interval['label'] = 'ignore'
            else:
                interval.update(self.variant())
        return interval

    def crosses_gap(self, start_us, end_us):
        return any(start_us < b and end_us > a for a, b in self.gaps)

    def set_symbol(self, label):
        self.symbol = label

    def checkpoint(self, manual=True):
        self.undo_stack.append([dict(s) for s in self.intervals])
        del self.undo_stack[:-100]
        self.dirty = True
        if manual:
            self.fresh_detection = False

    def undo(self):
        if not self.undo_stack:
            self.message('Nothing to undo.')
            return
        self.intervals = self.undo_stack.pop()
        self.selected = None
        self.dirty = True
        self.rebuild()

    def suspicious(self):
        lengths = [s['end_us'] - s['start_us'] for s in self.intervals if s['label'] != 'ignore']
        if len(lengths) < 3:
            return set()
        median = np.median(lengths)
        return {i for i, s in enumerate(self.intervals) if s['label'] != 'ignore' and
                not SUSPICIOUS[0] <= (s['end_us'] - s['start_us']) / median <= SUSPICIOUS[1]}

    def rebuild(self):
        """Redraw every interval from self.intervals (after a structural change)."""
        for items in self.regions:
            for plot, item in zip(self.plots, items):
                plot.removeItem(item)
        self.regions = []
        self.intervals.sort(key=lambda s: s['start_us'])
        odd = self.suspicious()
        for i, s in enumerate(self.intervals):
            items = []
            for row, plot in enumerate(self.plots):
                item = EdgeRegion((self.to_s(s['start_us']), self.to_s(s['end_us'])),
                                           swapMode='block', bounds=(0, self.end_s))
                if row == 0:
                    pg.InfLineLabel(item.lines[0], f'{i + 1} {s["label"]}', position=.93,
                                    anchors=[(0, 0), (0, 0)], rotateAxis=(1, 0))
                item.sigRegionChanged.connect(lambda r, i=i: self.region_moving(i, r))
                item.sigRegionChangeFinished.connect(lambda r, i=i: self.region_moved(i, r))
                plot.addItem(item)
                items.append(item)
            self.regions.append(items)
            self.style(i, i in odd)
        self.update_mask()
        self.message()

    def style(self, i, odd=None):
        if odd is None:
            odd = i in self.suspicious()
        s = self.intervals[i]
        if s['label'] == 'ignore':
            rgb = (128, 128, 128)
        else:
            rgb = pg.intColor(GESTURES.index(s['label']), len(GESTURES)).getRgb()[:3]
        selected = i == self.selected
        brush = pg.mkBrush(*rgb, 110 if selected else 45)
        pen = pg.mkPen('r' if odd else (0, 0, 0) if selected else rgb, width=3 if selected or odd else 1)
        for item in self.regions[i]:
            item.setBrush(brush)
            item.setHoverBrush(pg.mkBrush(*rgb, 140))
            for line in item.lines:
                line.setPen(pen)
                line.setHoverPen(pg.mkPen((0, 0, 0), width=4))

    def update_mask(self):
        mask = np.zeros(len(self.times))
        for s in self.intervals:
            if s['label'] != 'ignore':
                a, b = np.searchsorted(self.times, (s['start_us'], s['end_us']))
                mask[a:b] = 1
        self.mask_curve.setData(self.seconds, mask * float(np.percentile(self.energy, 99)))

    def region_moving(self, i, region):
        """Mirror a drag onto the same interval in the other plots."""
        if self.syncing:
            return
        self.syncing = True
        for item in self.regions[i]:
            if item is not region:
                item.setRegion(region.getRegion())
        self.syncing = False

    def region_moved(self, i, region):
        if self.syncing:
            return
        lo, hi = region.getRegion()
        start, end = self.to_us(lo), self.to_us(hi)
        if i > 0:
            start = max(start, self.intervals[i - 1]['end_us'])
        if i + 1 < len(self.intervals):
            end = min(end, self.intervals[i + 1]['start_us'])
        s = self.intervals[i]
        if (start, end) == (s['start_us'], s['end_us']):
            self.region_moving(i, region)
            return
        self.checkpoint()
        if end - start < self.period_us:
            self.message('Too short or fully overlapped; reverted.')
            self.undo_stack.pop()
            start, end = s['start_us'], s['end_us']
        else:
            s['start_us'], s['end_us'] = start, end
            if s['label'] != 'ignore' and self.crosses_gap(start, end):
                s['label'] = 'ignore'
                self.message('Interval now crosses a sensor gap; set to ignore.')
                self.selected = i
                self.rebuild()
                return
        self.syncing = True
        for item in self.regions[i]:
            item.setRegion((self.to_s(start), self.to_s(end)))
        self.syncing = False
        self.selected = i
        odd = self.suspicious()
        for j in range(len(self.intervals)):
            self.style(j, j in odd)
        self.update_mask()
        self.message()

    def click(self, seconds):
        self.graphics.setFocus()
        us = self.t0 + seconds * 1e6
        hit = [i for i, s in enumerate(self.intervals) if s['start_us'] <= us < s['end_us']]
        old, self.selected = self.selected, hit[0] if hit else None
        for i in {old, self.selected} - {None}:
            if i < len(self.intervals):
                self.style(i)
        self.message()

    def select(self, i):
        old, self.selected = self.selected, i
        for j in {old, i} - {None}:
            if j < len(self.intervals):
                self.style(j)
        self.center_selected()
        self.message()

    def step(self, direction):
        if not self.intervals:
            return
        if self.selected is None:
            lo, hi = self.plots[0].viewRange()[0]
            ahead = [i for i, s in enumerate(self.intervals) if self.to_s(s['end_us']) > lo]
            self.select(ahead[0] if ahead and direction > 0 else len(self.intervals) - 1 if direction < 0 else 0)
        else:
            self.select(int(np.clip(self.selected + direction, 0, len(self.intervals) - 1)))

    def next_suspicious(self):
        odd = sorted(self.suspicious())
        if not odd:
            self.message('No suspicious lengths.')
            return
        after = [i for i in odd if self.selected is None or i > self.selected]
        self.select(after[0] if after else odd[0])
        self.zoom_selected()

    def drawing(self, a, b, finished):
        if self.preview is None:
            self.preview = [pg.LinearRegionItem((a, b), movable=False, brush=(255, 165, 0, 80)) for _ in self.plots]
            for plot, item in zip(self.plots, self.preview):
                plot.addItem(item)
        for item in self.preview:
            item.setRegion((a, b))
        if not finished:
            return
        for plot, item in zip(self.plots, self.preview):
            plot.removeItem(item)
        self.preview = None
        start, end = self.to_us(a), self.to_us(b)
        if end - start < self.period_us:
            return
        if any(start < s['end_us'] and end > s['start_us'] for s in self.intervals):
            self.message('New intervals cannot overlap existing ones. Drag the existing edges instead.')
            return
        self.checkpoint()
        interval = self.make_interval(self.symbol, start, end)
        self.intervals.append(interval)
        self.intervals.sort(key=lambda s: s['start_us'])
        self.selected = self.intervals.index(interval)
        self.rebuild()
        if interval['label'] != self.symbol:
            self.message('Crosses a sensor gap: added as ignore.')

    def delete(self):
        if self.selected is None:
            self.message('Click an interval first.')
            return
        self.checkpoint()
        self.intervals.pop(self.selected)
        self.selected = min(self.selected, len(self.intervals) - 1) if self.intervals else None
        self.rebuild()

    def toggle_ignore(self):
        if self.selected is None:
            return
        s = self.intervals[self.selected]
        label = self.symbol if s['label'] == 'ignore' else 'ignore'
        if label == 'ignore' and s['label'] == 'ignore':
            self.message('Pick a gesture label in the box first.')
            return
        if label != 'ignore' and self.crosses_gap(s['start_us'], s['end_us']):
            self.message('Crosses a sensor gap; must stay ignore.')
            return
        self.checkpoint()
        self.intervals[self.selected] = self.make_interval(label, s['start_us'], s['end_us'])
        self.rebuild()

    def merge(self):
        if self.selected is None or self.selected + 1 >= len(self.intervals):
            self.message('Select an interval that has a next one.')
            return
        a, b = self.intervals[self.selected], self.intervals[self.selected + 1]
        self.checkpoint()
        self.intervals[self.selected:self.selected + 2] = [self.make_interval(a['label'], a['start_us'], b['end_us'])]
        self.rebuild()

    # ---------- detection ----------
    def threshold_dragged(self):
        self.threshold.setValue(self.threshold_line.value())
        self.params_changed()

    def params_changed(self):
        self.threshold_line.setValue(self.threshold.value())
        if self.fresh_detection or not self.intervals:
            self.run_detect(confirm=False)
        else:
            self.message('Settings changed. Press D to re-detect (replaces your edits).')

    def run_detect(self, confirm=True):
        if confirm and self.intervals and not self.fresh_detection:
            answer = QtWidgets.QMessageBox.question(
                self, 'Re-detect', f'Replace all {len(self.intervals)} intervals with a new detection? (Ctrl+Z undoes)')
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        if self.symbol == 'ignore':
            self.message('Pick a gesture label before detecting.')
            return
        found = detect(self.energy, self.hz, self.threshold.value(), self.merge_gap.value(),
                       self.min_len.value(), self.pad.value())
        self.checkpoint(manual=False)
        self.intervals = [self.make_interval(self.symbol, int(self.times[a]), int(self.times[b - 1]) + self.period_us)
                          for a, b in found]
        self.fresh_detection = True
        self.selected = None
        self.rebuild()

    # ---------- saving ----------
    def message(self, text=None):
        counts = collections.Counter(s['label'] for s in self.intervals)
        lengths = [(s['end_us'] - s['start_us']) / 1e6 for s in self.intervals if s['label'] != 'ignore']
        parts = [', '.join(f'{n} {k}' for k, n in counts.items()) or 'no intervals']
        if lengths:
            parts.append(f'length median {np.median(lengths):.2f} s ({min(lengths):.2f}-{max(lengths):.2f})')
        if self.suspicious():
            parts.append(f'{len(self.suspicious())} suspicious (red, Tab)')
        if self.selected is not None:
            s = self.intervals[self.selected]
            a, b = self.to_s(s['start_us']), self.to_s(s['end_us'])
            parts.append(f'selected #{self.selected + 1} {s["label"]} {a:.2f}-{b:.2f} s ({b - a:.2f} s)')
        parts.append('UNSAVED' if self.dirty else 'reviewed' if self.doc.get('reviewed') else 'draft')
        if text:
            parts.append(text)
        self.status.setText('   |   '.join(parts))

    def save(self, reviewed):
        doc = dict(self.doc, reviewed=reviewed, intervals=sorted(self.intervals, key=lambda s: s['start_us']))
        doc['annotation_method'] = 'block_review'
        doc['auto_detect'] = dict(threshold=self.threshold.value(), merge_gap_s=self.merge_gap.value(),
                                  min_len_s=self.min_len.value(), pad_s=self.pad.value())
        doc.pop('boundary_basis', None)   # live-keyboard note; boundaries are now sensor-time edits
        temp = self.out.with_suffix('.json.tmp')
        temp.write_text(json.dumps(dict(doc, reviewed=True), indent=2))   # validate as if reviewed
        try:
            labels_for(temp, self.times, self.period_us)
        except ValueError as e:
            temp.unlink()
            self.message(f'Not saved: {e}')
            return False
        temp.write_text(json.dumps(doc, indent=2))
        temp.replace(self.out)
        self.doc, self.dirty = doc, False
        self.message(f'Saved {"reviewed" if reviewed else "draft"} labels to {self.out.name}.')
        return True

    def closeEvent(self, event):
        if self.dirty:
            buttons = QtWidgets.QMessageBox.StandardButton
            answer = QtWidgets.QMessageBox.question(
                self, 'Unsaved labels', 'Save reviewed labels before closing?',
                buttons.Save | buttons.Discard | buttons.Cancel)
            if answer == buttons.Cancel or (answer == buttons.Save and not self.save(True)):
                event.ignore()
                return
        event.accept()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('csv', type=Path)
    args = p.parse_args()
    pg.setConfigOptions(background='w', foreground='k', antialias=False)
    app = QtWidgets.QApplication(sys.argv)
    window = BlockAnnotator(args.csv)
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
