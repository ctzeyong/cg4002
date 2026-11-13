"""Board power and inference time under each power-saving setting, one setting at a time.

    sudo -n env HOME="$HOME" XILINX_XRT=/usr ~/comms-venv/bin/python ~/comms/power_sweep.py

It loads its own overlay, so it refuses to start while the board client is running. For each
setting (board_settings.py; "baseline" is the board as it is) it:

    1. applies the setting, with every original saved and a guardian watching first
    2. waits SETTLE_S, then reads the rails for --idle s with nothing running: the board between
       gestures, which is nearly all of a game
    3. runs inferences back to back for --load s, timing each one, still reading the rails:
       the most inference can cost. Each answer is checked against baseline's for the same window
    4. puts every original back before the next setting

Results go to ~/comms/logs/<UTC date>/power_sweep_<time>Z.csv and the log, one row per setting:
idle and load power, inference median/p95/max ms, and the energy one inference adds.
"""

import argparse
import csv
import logging
import os
import pathlib
import signal
import statistics
import threading
import time

import board_settings
import gesture
import power_monitor
from board_logging import setup_logging

DEFAULT_CONFIGS = (
    "baseline",
    "khz=599999", "khz=399999", "khz=299999",
    "cores=2", "cores=1",
    "pl=50", "pl=25",
    "khz=599999+cores=2", "khz=399999+cores=1", "khz=299999+cores=1",
    "khz=399999+cores=1+pl=50", "khz=299999+cores=1+pl=25",
    "baseline",  # again, to see how far the board drifted during the sweep
)
SETTLE_S = 3.0
WINDOWS = 16  # distinct inputs, cycled, so a stale result cannot pass the answer check
LOG_FILE = power_monitor.LOG_DIR / "power_sweep.log"
CLIENTS = ("tcp_client", "power_monitor.py", "power_sweep.py")

log = logging.getLogger("power")


class Sampler(threading.Thread):
    """Reads every rail every `interval` s until stopped; mean() gives each rail and TOTAL."""

    def __init__(self, rails, interval=0.25):
        super().__init__(daemon=True)
        self.rails, self.interval = rails, interval
        self.means = power_monitor.Means()
        self.halt = threading.Event()

    def run(self):
        while not self.halt.is_set():
            try:
                self.means.add({n: read() for n, read in self.rails.items()}, self.interval)
            except Exception as exc:  # one bad read must not end the sweep
                log.warning("power read failed: %s", exc)
            self.halt.wait(self.interval)

    def stop(self):
        self.halt.set()
        self.join()
        return self.means.mean()


def measure(rails, seconds, work=None):
    """Mean power over `seconds`, with `work` called back to back if given, and its times in s."""
    sampler = Sampler(rails)
    sampler.start()
    times = []
    end = time.monotonic() + seconds
    if work is None:
        time.sleep(seconds)
    while work is not None and time.monotonic() < end:
        t0 = time.perf_counter()
        work()
        times.append(time.perf_counter() - t0)
    return sampler.stop(), times


def others_running():
    """Command lines of the board client, the monitor or another sweep, other than this one."""
    found = []
    for proc in pathlib.Path("/proc").glob("[0-9]*"):
        try:
            argv = (proc / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            continue
        cmd = " ".join(argv).strip()
        # python itself, not the sudo or setsid that started it
        is_python = "python" in pathlib.PurePosixPath(argv[0]).name
        if proc.name != str(os.getpid()) and is_python and any(c in cmd for c in CLIENTS):
            found.append(cmd)
    return found


def percentile(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]


def row_for(config, now, idle, load, times, wrong):
    ms = [t * 1000 for t in times]
    added_w = load["TOTAL"] - idle["TOTAL"]
    row = {"config": config, "khz": now.get("khz"), "cores": now.get("cores"),
           "pl_mhz": now.get("pl"), "idle_W": round(idle["TOTAL"], 4),
           "load_W": round(load["TOTAL"], 4), "load_minus_idle_W": round(added_w, 4),
           "inferences": len(ms), "infer_median_ms": round(statistics.median(ms), 3),
           "infer_p95_ms": round(percentile(ms, 95), 3), "infer_max_ms": round(max(ms), 3),
           "mJ_per_inference": round(added_w * statistics.mean(times) * 1000, 3),
           "wrong_answers": wrong}
    for state, mean in (("idle", idle), ("load", load)):
        for rail in power_monitor.KEY_RAILS:
            row[f"{state}_{rail}"] = round(mean.get(rail, 0.0), 4)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("configs", nargs="*", default=list(DEFAULT_CONFIGS))
    parser.add_argument("--bitstream", default="new_gestures_all.xsa")
    parser.add_argument("--vocab", default="new")
    parser.add_argument("--idle", type=float, default=20.0, help="seconds of idle per setting")
    parser.add_argument("--load", type=float, default=20.0, help="seconds of inference per setting")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    for config in args.configs:
        board_settings.parse(config)  # a typo fails now, not half way through
    setup_logging(args.verbose, LOG_FILE)
    running = others_running()
    if running:
        raise SystemExit("not sweeping while these run (stop them first):\n  " + "\n  ".join(running))

    def stop(signum, frame):  # pkill, Ctrl-C, a dropped session: unwind so the finally runs
        raise SystemExit(f"stopped by signal {signum}")

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)

    import numpy as np  # the board's venv

    settings = board_settings.Settings()
    settings.recover()  # anything a killed earlier run left changed
    log.info("board before the sweep: %s", settings.now())
    rails = power_monitor.board_rails()
    model = gesture.load_classifier(None, args.vocab, args.bitstream).model
    log.info("overlay %s loaded; board now %s", args.bitstream, settings.now())
    rng = np.random.default_rng(0)
    shape = (model.config.win, model.config.channels)
    windows = [rng.normal(0, 3, size=shape).astype(np.float32) for _ in range(WINDOWS)]

    def raw(i):
        model.infer_window(windows[i])
        return np.array(model.outb)

    expected = [raw(i) for i in range(WINDOWS)]
    out_dir = power_monitor.LOG_DIR / time.strftime("%Y-%m-%d", time.gmtime())
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"power_sweep_{time.strftime('%H%M%S', time.gmtime())}Z.csv"
    rows = []
    try:
        for config in args.configs:
            wanted = board_settings.parse(config)
            try:
                settings.apply(wanted)
                now = settings.now()
                log.info("%s: board now %s", config, now)
                time.sleep(SETTLE_S)
                idle, _ = measure(rails, args.idle)
                count = [0, 0]  # inferences, wrong answers

                def one():
                    i = count[0] % WINDOWS
                    model.infer_window(windows[i])
                    count[0] += 1
                    if not np.array_equal(model.outb, expected[i]):
                        count[1] += 1

                load, times = measure(rails, args.load, one)
            finally:
                settings.restore()
            row = row_for(board_settings.describe(wanted), now, idle, load, times, count[1])
            rows.append(row)
            log.info("%s: idle %.3f W, inferring %.3f W (%+.3f), inference median %.2f ms "
                     "p95 %.2f ms, %.2f mJ each, %d wrong of %d", row["config"], row["idle_W"],
                     row["load_W"], row["load_minus_idle_W"], row["infer_median_ms"],
                     row["infer_p95_ms"], row["mJ_per_inference"], row["wrong_answers"],
                     row["inferences"])
            with open(path, "w", newline="", encoding="utf-8") as f:  # rewritten as rows come
                writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
    finally:
        settings.restore()
        log.info("board after the sweep: %s (state file left: %s)", settings.now(),
                 settings.state_file.exists())
    log.info("results in %s", path)


if __name__ == "__main__":
    main()
