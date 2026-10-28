"""Record labelled gesture data over BLE from the normal comms firmware (firebeetle.ino).

Hardware's record.py does the same over USB with its own 500 Hz sketch. This one uses the
glove exactly as gameplay does: the same BLE connection code (ble_client), the same 100 Hz
SENSOR stream, the same gesture flag. So the recordings are what live inference sees. Run it
INSTEAD of the pipeline (a glove takes one BLE connection at a time):

    python record_ble.py --node 1 --participant p2 --session day3 --live-labels \
        --symbol chop --drawing-mode air_front --direction cw --posture standing

Keys (Windows terminal, with --live-labels; Hardware's live_labels.py): Space start/end an
attempt, D discard, Q finish. Unlabelled time is background (the noise class), as with record.py.

Output, auto-named under Hardware/Data Collection/sessions/ble/ (never overwrites, adds _2, _3):
  <name>.csv          sequence, esp_time_us, ax_g..gz_dps, host_rx_unix_ns, imu_status, gesture_flag
  <name>.labels.json  the intervals, as record.py writes them
  <name>.meta.json    raw_hz=100, transport=ble, node, participant, ...
The same layout as record.py, so gesture_pipeline.export_dataset and the background loader read
it unchanged (they take the sample rate from raw_hz).

Timing: the packet's 8-bit seq is unwrapped into a running sample count on the firmware's 10 ms
grid, and esp_time_us = count x 10 000. BLE arrival time never sets the timeline, so a lost or
non-OK packet shows up as a gap (the exporter never bridges one), not as squeezed samples.
Not usable to retrain the on-glove detector: that needs Hardware's 500 Hz raw recordings.
"""

import argparse
import asyncio
import csv
import json
import logging
import sys
import time
from pathlib import Path

import ble_client
from ble_packet import IMU_OK, IMU_FIELD_COUNT, TYPE_SENSOR, unpack_fields
from logging_setup import setup_logging
from plumbing import install_gatt_cancel_error_filter, suppress_queue_get_shutdown_warning

# Hardware's recorder contract (schema, channel names, gesture list, live labels) lives in
# its data-collection folder; reuse it rather than keep a second copy.
DATA_COLLECTION = Path(__file__).resolve().parents[2] / "Hardware" / "Data Collection"
sys.path.append(str(DATA_COLLECTION))
from core import CHANNELS, GESTURES, SCHEMA  # noqa: E402 - needs the sys.path above
from live_labels import ConsoleKeys, LiveLabels  # noqa: E402

SESSIONS_DIR = DATA_COLLECTION / "sessions" / "ble"
LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "record_ble.log"

RAW_HZ = 100                 # firebeetle.ino PACKET_PERIOD_MS = 10
PACKET_PERIOD_US = 1_000_000 // RAW_HZ
COUNTS_PER_G = 8192.0        # imu.h ranges, the imu-counts-int16-v1 contract
COUNTS_PER_DPS = 65.5
STATUS_FIELD = IMU_FIELD_COUNT      # reserved[0]: IMU status
FLAG_FIELD = IMU_FIELD_COUNT + 1    # reserved[1]: the glove's gesture flag
# An arrival gap this long means the link dropped. The 8-bit seq wraps every 2.56 s, so it
# can't count the packets lost across it; the count is estimated from arrival time instead.
RESYNC_S = 0.5
KEY_POLL_S = 0.02
FLUSH_EVERY = RAW_HZ               # flush the CSV and save labels once a second
PROGRESS_EVERY = 10 * RAW_HZ       # print a progress line every 10 s

HEADER = ["sequence", "esp_time_us", *CHANNELS, "host_rx_unix_ns", "imu_status", "gesture_flag"]

log = logging.getLogger("record_ble")


class SampleClock:
    """Turn the board's 8-bit SENSOR seq into a running sample count on its 10 ms grid.

    feed() returns (count, gap), or None for a repeat or late packet. gap is True when packets
    were lost just before this one, or the link dropped (count then estimated from arrival).
    """

    def __init__(self):
        self.count = None
        self.last_seq = None
        self.last_t = None

    def feed(self, seq, t_recv):
        if self.count is None:
            self.count, gap = 0, False
        elif t_recv - self.last_t > RESYNC_S:
            self.count += max(1, round((t_recv - self.last_t) * 1_000_000 / PACKET_PERIOD_US))
            gap = True
        else:
            steps = (seq - self.last_seq) & 0xFF
            if steps == 0 or steps >= ble_client.SEQ_BACKWARD_THRESHOLD:
                return None                       # repeat or late: never moves time backwards
            self.count += steps
            gap = steps > 1
        self.last_seq, self.last_t = seq, t_recv
        return self.count, gap


def sample_row(fields, count, wall_ns):
    """One CSV row from a SENSOR packet's 11 fields, in record.py's units (g, deg/s)."""
    g_dps = [v / COUNTS_PER_G for v in fields[0:3]] + [v / COUNTS_PER_DPS for v in fields[3:6]]
    return [count, count * PACKET_PERIOD_US, *g_dps, wall_ns,
            fields[STATUS_FIELD], fields[FLAG_FIELD]]


class Recorder:
    """Write SENSOR packets as samples and feed the live labels their timeline.

    Only IMU_OK packets become rows; any other status (no reading) makes the next row a gap,
    so a label in progress is marked ignore and the exporter never crops across it.
    """

    def __init__(self, csv_file, labels=None):
        self.writer = csv.writer(csv_file)
        self.writer.writerow(HEADER)
        self.file = csv_file
        self.labels = labels
        self.clock = SampleClock()
        self.pending_gap = False
        # perf_counter (BlePacket.t_recv) -> wall clock, for the host_rx_unix_ns column
        self.wall_offset_ns = time.time_ns() - time.perf_counter_ns()
        self.samples = self.gaps = self.late = self.no_imu = 0

    def add(self, packet):
        """Record one packet. Returns True if it became a sample row."""
        if packet.type != TYPE_SENSOR:
            return False                          # ECHO / EVENT: not sensor data
        tick = self.clock.feed(packet.seq, packet.t_recv)
        if tick is None:
            self.late += 1
            return False
        count, gap = tick
        fields = unpack_fields(packet.payload)
        if fields[STATUS_FIELD] != IMU_OK:
            self.no_imu += 1
            self.pending_gap = True
            return False
        gap, self.pending_gap = gap or self.pending_gap, False
        if gap:
            self.gaps += 1
        wall_ns = self.wall_offset_ns + int(packet.t_recv * 1e9)
        self.writer.writerow(sample_row(fields, count, wall_ns))
        self.samples += 1
        if self.labels:
            self.labels.sample(count * PACKET_PERIOD_US, gap)
        if self.samples % FLUSH_EVERY == 0:
            self.file.flush()
            if self.labels:
                self.labels.save()
        return True

    def progress(self):
        return (f"{self.samples} samples ({self.samples / RAW_HZ:.0f} s); {self.gaps} gaps; "
                f"{self.no_imu} without IMU reading; {self.late} late/repeated")


def default_name(args):
    """sessions/ble/<participant>_<session>[_symbol_mode_direction_posture].csv, as record.py."""
    parts = [args.participant, args.session]
    if args.symbol:
        parts += [args.symbol, args.drawing_mode, args.direction, args.posture]
    return SESSIONS_DIR / ("_".join(parts) + ".csv")


def unique_output(path):
    """path, or path with _2/_3/... so no existing CSV, meta or labels file is overwritten."""
    def taken(p):
        return any(p.with_suffix(s).exists() for s in (".csv", ".meta.json", ".labels.json"))

    if not taken(path):
        return path
    n = 2
    while taken(path.with_name(f"{path.stem}_{n}{path.suffix}")):
        n += 1
    return path.with_name(f"{path.stem}_{n}{path.suffix}")


async def record(node, recorder, keys, labels):
    """Run one BLE node into the recorder until Q (with keys) or Ctrl-C. Returns True on Q."""
    queue = asyncio.Queue(maxsize=ble_client.QUEUE_MAXSIZE)
    outboxes = ble_client.make_outboxes((node,))
    stop = asyncio.Event()
    restore_exc_handler = install_gatt_cancel_error_filter()
    ble_task = asyncio.create_task(ble_client.run_all((node,), queue, outboxes, stop))
    finished = False
    try:
        while not stop.is_set():
            if keys:
                key = keys.poll()
                if key == "q":
                    finished = True
                    break
                if key:
                    message = labels.key(key)
                    if message:
                        print(message, flush=True)
            try:
                packet = await asyncio.wait_for(queue.get(), timeout=KEY_POLL_S)
            except asyncio.TimeoutError:
                continue
            batch = [packet]
            while not queue.empty():
                batch.append(queue.get_nowait())
            for packet in batch:
                if recorder.add(packet) and recorder.samples % PROGRESS_EVERY == 0:
                    print(recorder.progress(), flush=True)
                    if labels:
                        print(labels.progress(), flush=True)
            if ble_task.done():
                break                             # run_all only ends on stop or an error
    finally:
        stop.set()
        ble_task.cancel()
        await asyncio.gather(ble_task, return_exceptions=True)
        restore_exc_handler()  # after the gather; see the filter's docstring
    return finished


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--node", type=int, choices=[n.node_id for n in ble_client.NODES], default=1,
                   help="glove to record: its firmware's PLAYER_ID (default 1)")
    p.add_argument("--out", type=Path,
                   help="output CSV; default: auto-named under Hardware/Data Collection/sessions/ble/")
    p.add_argument("--participant", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--notes", default="")
    p.add_argument("--live-labels", action="store_true",
                   help="Windows terminal: Space start/end, D discard, Q finish")
    p.add_argument("--symbol", choices=GESTURES)
    p.add_argument("--drawing-mode", choices=("table", "air_front"), default="table")
    p.add_argument("--direction", choices=("cw", "ccw", "na"), default="na")
    p.add_argument("--posture", choices=("seated", "standing"), default="seated")
    p.add_argument("-v", "--verbose", action="store_true", help="log every BLE packet")
    args = p.parse_args(argv)
    if args.live_labels and not args.symbol:
        p.error("--live-labels requires --symbol")
    return args


def main(argv=None):
    args = parse_args(argv)
    keys = None
    if args.live_labels:
        if sys.platform != "win32" or not sys.stdin.isatty():
            sys.exit("Live keys need a Windows terminal (for example PowerShell or Anaconda Prompt)")
        keys = ConsoleKeys()

    setup_logging(LOG_FILE, args.verbose)
    suppress_queue_get_shutdown_warning()
    node = next(n for n in ble_client.NODES if n.node_id == args.node)
    out = unique_output(args.out or default_name(args))
    out.parent.mkdir(parents=True, exist_ok=True)
    meta_path, labels_path = out.with_suffix(".meta.json"), out.with_suffix(".labels.json")
    meta = dict(schema=SCHEMA, raw_hz=RAW_HZ, transport="ble", node=args.node,
                firmware="Comms/firebeetle/firebeetle.ino", default_output_steps=100, dlpf=3,
                accel_range_g=4, gyro_range_dps=500, participant=args.participant,
                session=args.session, notes=args.notes, started_unix=time.time(),
                channels=list(CHANNELS),
                timestamp_basis="firmware SENSOR seq unwrapped x 10 ms (nominal 100 Hz), "
                                "not BLE arrival time")
    labels = None
    if keys:
        labels = LiveLabels(labels_path, args.symbol, dict(drawing_mode=args.drawing_mode,
                            direction=args.direction, posture=args.posture))
        labels.doc["boundary_basis"] = "latest received sensor sample; BLE latency not corrected"

    print(f"Recording node {args.node} to {out}")
    print("Connecting to the glove over BLE (stop the pipeline first; a glove takes one connection).")
    if labels:
        print(f"Live labels: {args.symbol}, {args.drawing_mode}, {args.direction}, {args.posture}.")
        print("Tap SPACE start/end; D discard active/last attempt; Q finish. Keep this terminal focused.")
    else:
        print("No live labels: Ctrl+C to finish.")

    stop_reason, finished = None, False
    with out.open("x", newline="") as f:
        meta_path.write_text(json.dumps(meta, indent=2))
        recorder = Recorder(f, labels)
        if labels:
            labels.save()
        try:
            finished = asyncio.run(record(node, recorder, keys, labels))
        except KeyboardInterrupt:
            pass
        except Exception as exc:  # noqa: BLE001 - keep what was recorded, report why it stopped
            stop_reason = f"{type(exc).__name__}: {exc}"
            print(f"Recording stopped: {stop_reason}")
        finally:
            if labels:
                labels.finish()
            meta.update(samples=recorder.samples, gaps=recorder.gaps, no_imu=recorder.no_imu,
                        late_or_repeated=recorder.late, stop_reason=stop_reason,
                        finished_unix=time.time())
            meta_path.write_text(json.dumps(meta, indent=2))

    print(f"Saved {recorder.samples} samples to {out}. {recorder.progress()}")
    if labels and finished and recorder.samples and not stop_reason:
        print("Unlabelled time becomes BACKGROUND. Every symbol attempt must be labelled or ignored.")
        try:
            confirmed = input("Did you mark every attempt, discard mistakes, and capture usable boundaries? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            confirmed = ""
        labels.doc["reviewed"] = confirmed.strip().lower() == "y"
        labels.save()
    if labels:
        print(labels.progress())
        print(f"Labels {'confirmed' if labels.doc['reviewed'] else 'NOT reviewed (export skips it)'}: {labels_path}")


if __name__ == "__main__":
    main()
