"""Phone <-> laptop clock offset over USB, and the phone's logcat, for one latency run.

    python phone_clock.py start A1     # clear logcat, measure the offset. Then UNPLUG the phone
    ...                                # run the test with the phone on battery
    python phone_clock.py finish A1    # replug first: measure the offset again, dump logcat

Writes logs/phone/<name>_start.json, <name>_end.json and <name>_logcat.txt, which
latency_report.py reads with --phone logs/phone/<name>.

The phone runs unplugged because USB power may change its Wi-Fi power saving, so its logcat
lines are recovered afterwards from the buffer, and the offset is measured on both sides of
the run and interpolated. Method: notes/latency.md 3.3.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
OUT_DIR = REPO / "logs" / "phone"

# Unity's bundled adb, never the Android SDK's: two adb servers fight over port 5037
# (notes/unity_visualiser.md). ADB in the environment overrides it.
UNITY_ADB = "C:/Program Files/Unity/Hub/Editor/*/Editor/Data/PlaybackEngines/AndroidPlayer/SDK/platform-tools/adb.exe"

# A shell builtin, so no process is spawned on the phone: forking `date` would add time to
# one direction only. mksh, Android's shell, has $EPOCHREALTIME; check on the phone first.
TIME_COMMAND = "echo $EPOCHREALTIME"
SAMPLES = 50


class ClockError(Exception):
    """The phone's shell did not return a usable time."""


def find_adb():
    if os.environ.get("ADB"):
        return os.environ["ADB"]
    found = sorted(glob.glob(UNITY_ADB))
    if not found:
        raise SystemExit("no Unity adb found; set ADB to its path (notes/unity_visualiser.md)")
    return found[-1]


def parse_phone_time(text):
    """`$EPOCHREALTIME`'s output -> seconds. Empty means the shell has no such variable."""
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        raise ClockError(
            f"phone returned {text!r} for {TIME_COMMAND!r}: its shell may lack $EPOCHREALTIME"
        ) from None


def measure(query, samples=SAMPLES):
    """Offset from the lowest-RTT exchange. `query()` returns (l1, phone_time, l3), with
    l1 and l3 the laptop's perf_counter either side.

    offset = phone_time - (l1 + l3)/2, so phone_time_on_laptop_clock = phone_time - offset,
    the same sign as the board's (latency.md 3.3). Wrong by at most rtt/2.
    """
    query()  # warm-up: the first exchange pays for the shell starting up
    best = min((query() for _ in range(samples)), key=lambda s: s[2] - s[0])
    l1, phone_time, l3 = best
    return {
        "phone_time": phone_time,
        "laptop_time": (l1 + l3) / 2,
        "offset": phone_time - (l1 + l3) / 2,
        "rtt": l3 - l1,
        "samples": samples,
    }


class PhoneShell:
    """One `adb shell` kept open, so each exchange is a line out and a line back."""

    def __init__(self, adb):
        self.process = subprocess.Popen(
            [adb, "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    def query(self):
        l1 = time.perf_counter()
        self.process.stdin.write(TIME_COMMAND + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        l3 = time.perf_counter()
        return l1, parse_phone_time(line), l3

    def close(self):
        try:
            self.process.stdin.write("exit\n")
            self.process.stdin.flush()
            self.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()


def run_adb(adb, *args):
    # Logcat is UTF-8 and may hold bytes the Windows code page cannot decode (0x81 on
    # 2 Oct), which would lose the whole dump. Replace them; the lines we parse are ASCII.
    result = subprocess.run(
        [adb, *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        raise SystemExit(
            f"adb {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}\n"
            "No device? Check USB debugging, and that Samsung Auto Blocker is off "
            "(notes/unity_visualiser.md)."
        )
    return result.stdout


def sync(adb):
    shell = PhoneShell(adb)
    try:
        sample = measure(shell.query)
    finally:
        shell.close()
    sample["laptop_wall_minus_perf"] = time.time() - time.perf_counter()
    print(f"phone offset {sample['offset']:.6f} s, rtt {sample['rtt'] * 1000:.3f} ms "
          f"(bound {sample['rtt'] * 500:.3f} ms)")
    return sample


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"wrote {path}")


def start(adb, name, buffer_size):
    run_adb(adb, "get-state")
    if buffer_size:
        run_adb(adb, "logcat", "-G", buffer_size)
    print(run_adb(adb, "logcat", "-g").strip())
    run_adb(adb, "logcat", "-c")
    write_json(OUT_DIR / f"{name}_start.json", sync(adb))
    print("now UNPLUG the phone, run the test, replug, then: phone_clock.py finish " + name)


def finish(adb, name):
    run_adb(adb, "get-state")
    write_json(OUT_DIR / f"{name}_end.json", sync(adb))
    dump = run_adb(adb, "logcat", "-d", "-v", "epoch", "-v", "usec")
    path = OUT_DIR / f"{name}_logcat.txt"
    path.write_text(dump, encoding="utf-8")
    print(f"wrote {path} ({dump.count(chr(10))} lines)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=("start", "finish", "check"))
    parser.add_argument("name", nargs="?", default="run", help="run name, e.g. A1 (start/finish)")
    parser.add_argument(
        "--buffer", metavar="SIZE", help="start: enlarge logcat's buffer first, e.g. 16M"
    )
    args = parser.parse_args(argv)
    adb = find_adb()
    try:
        if args.step == "start":
            start(adb, args.name, args.buffer)
        elif args.step == "finish":
            finish(adb, args.name)
        else:  # check: the builtin and the USB round trip, nothing written
            run_adb(adb, "get-state")
            sync(adb)
    except ClockError as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    sys.exit(main())
