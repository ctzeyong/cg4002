"""Power-saving settings on the Ultra96 that always go back to what they were.

A setting string joins them with "+", e.g. "khz=399999+cores=2+pl=50":

    khz=F      the A53 clock, one of cpu0/cpufreq/scaling_available_frequencies, set through
               scaling_setspeed under the board's only governor, userspace
    cores=N    CPU cores left on, 1 to 4: cpu1 to cpu3 go offline from the top; cpu0 never does
    pl=MHZ     the FPGA's clock fclk0 (pynq Clocks), at most what it is now, so never overclocked.
               Loading an overlay resets it, so set it after the overlay is loaded

Nothing else is changed: not the governor, Wi-Fi, a service, or any config file. Every setting is
a runtime value (sysfs, a clock register) that a reboot also resets.

How the originals are always put back:
    1. Before anything changes, each original value goes into a state file, written atomically:
       ~/comms/power_state.json for the power monitor's CPU settings, ~/comms/pl_state.json for
       the board client's PL clock.
    2. A guardian process, in its own session so Ctrl-C and a dropped SSH session miss it, watches
       the process that made the change. If that process dies in any way, kill -9 included, the
       guardian puts the originals back. If the guardian itself is stopped, it puts them back first.
    3. The process puts them back itself when it ends normally (Settings.restore).
    4. If both were killed with -9 at once, the next Settings.apply (or "board_settings.py
       restore") finds the state file and puts the originals back before anything else.

run_board.py --cpu-mhz/--cores/--pl-mhz uses this; see the README's "Tuning the board".

    sudo -n ~/comms-venv/bin/python ~/comms/board_settings.py show
    sudo -n ~/comms-venv/bin/python ~/comms/board_settings.py restore
"""

import json
import logging
import os
import pathlib
import signal
import subprocess
import sys
import time

CPU_ROOT = pathlib.Path("/sys/devices/system/cpu")
# One state file per process that changes settings, so neither puts back the other's:
# the power monitor sets the CPU clock and cores, the board client the PL clock
STATE_FILE = pathlib.Path.home() / "comms" / "power_state.json"
PL_STATE_FILE = pathlib.Path.home() / "comms" / "pl_state.json"
GUARD_LOG = pathlib.Path.home() / "comms" / "logs" / "power_guard.log"
SETSPEED = "cpu0/cpufreq/scaling_setspeed"
GOVERNOR = "cpu0/cpufreq/scaling_governor"
AVAILABLE = "cpu0/cpufreq/scaling_available_frequencies"
KEYS = ("khz", "cores", "pl")
NOT_CHANGED = {  # asked for by name, so say why not instead of "unknown setting"
    "wifi": "Wi-Fi stays on: the board's link to the laptop goes over it",
    "bt": "this board has no Bluetooth radio to turn off",
    "governor": "userspace is the board's only governor",
}
MIN_PL_MHZ = 10.0
GUARD_POLL_S = 0.5

log = logging.getLogger("power")


def parse(spec):
    """'khz=399999+cores=2+pl=50' -> {'khz': 399999, 'cores': 2, 'pl': 50.0}; '' or 'none' -> {}."""
    wanted = {}
    if not spec or spec.strip().lower() in ("none", "baseline"):
        return wanted
    for part in spec.split("+"):
        key, sep, value = part.strip().partition("=")
        if key in NOT_CHANGED:
            raise ValueError(f"{key}: not changed, {NOT_CHANGED[key]}")
        if key not in KEYS or not sep:
            raise ValueError(f"unknown setting {part!r}; use khz=F, cores=N or pl=MHZ")
        try:
            wanted[key] = float(value) if key == "pl" else int(value)
        except ValueError:
            raise ValueError(f"{part!r}: {value!r} is not a number") from None
    return wanted


def describe(wanted):
    """{'khz': 399999, 'cores': 2} -> 'khz=399999+cores=2'; {} -> 'baseline'."""
    if not wanted:
        return "baseline"
    return "+".join(f"{k}={wanted[k]:g}" if k == "pl" else f"{k}={wanted[k]}"
                    for k in KEYS if k in wanted)


def process_start(pid):
    """When `pid` started (clock ticks since boot), or None if it is not running.

    Compared as well as the pid, so a new process that got the same pid is not taken for it.
    """
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return int(stat.rsplit(")", 1)[1].split()[19])


def _pynq_clocks():
    from pynq import Clocks  # the board only; reads and writes the PS clock register, no overlay

    return Clocks


class Settings:
    """Change the settings, keeping what to put back in the state file until it is back."""

    def __init__(self, cpu_root=CPU_ROOT, state_file=STATE_FILE, clocks=None,
                 start_guardian=True, alive=process_start):
        self.cpu = pathlib.Path(cpu_root)
        self.state_file = pathlib.Path(state_file)
        self._clocks = clocks
        self.start_guardian = start_guardian
        self.alive = alive
        self.guardian = None

    @property
    def clocks(self):
        if self._clocks is None:
            self._clocks = _pynq_clocks()
        return self._clocks

    # --- reading the board --------------------------------------------------------

    def read(self, rel):
        return (self.cpu / rel).read_text().strip()

    def write(self, rel, value):
        (self.cpu / rel).write_text(f"{value}\n")

    def khz_for(self, mhz):
        """The available CPU frequency in kHz nearest `mhz`, e.g. 300 -> 299999."""
        available = [int(f) for f in self.read(AVAILABLE).split()]
        nearest = min(available, key=lambda f: abs(f / 1000 - mhz))
        if abs(nearest / 1000 - mhz) > 1:
            raise ValueError(f"cpu {mhz:g} MHz: choose one of "
                             f"{', '.join(str(round(f / 1000)) for f in available)}")
        return nearest

    def other_cores(self):
        """cpu1, cpu2, ... that can go offline, lowest first. cpu0 is never in it."""
        numbers = sorted(int(p.name[3:]) for p in self.cpu.glob("cpu[0-9]*")
                         if p.name[3:].isdigit() and (p / "online").exists())
        return [f"cpu{n}" for n in numbers if n != 0]

    def now(self):
        """The current values, as a dict like the one apply takes."""
        cores = 1 + sum(self.read(f"{c}/online") == "1" for c in self.other_cores())
        current = {"khz": int(self.read(SETSPEED)), "cores": cores}
        try:
            current["pl"] = round(float(self.clocks.fclk0_mhz), 3)
        except Exception:  # off the board, or pynq not importable
            pass
        return current

    # --- the state file ---------------------------------------------------------------

    def load_state(self):
        try:
            return json.loads(self.state_file.read_text())
        except FileNotFoundError:
            return None
        except ValueError:  # cut off mid-write cannot happen (atomic), but never fail on it
            log.warning("state file %s is unreadable; nothing to put back from it", self.state_file)
            return None

    def _save_state(self, state):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_file)

    # --- changing and putting back ---------------------------------------------------

    def check(self, wanted):
        """Raise ValueError if `wanted` is not safe to apply on this board."""
        for key in wanted:
            if key not in KEYS:
                raise ValueError(f"unknown setting {key!r}")
        if "khz" in wanted:
            governor = self.read(GOVERNOR)
            if governor != "userspace":
                raise ValueError(f"the governor is {governor}, not userspace; khz is not changed")
            available = [int(f) for f in self.read(AVAILABLE).split()]
            if wanted["khz"] not in available:
                raise ValueError(f"khz={wanted['khz']} is not one of {available}")
        if "cores" in wanted:
            most = 1 + len(self.other_cores())
            if not 1 <= wanted["cores"] <= most:
                raise ValueError(f"cores={wanted['cores']}: this board has 1 to {most}")
        if "pl" in wanted:
            now = float(self.clocks.fclk0_mhz)
            if not MIN_PL_MHZ <= wanted["pl"] <= now + 0.5:
                raise ValueError(f"pl={wanted['pl']:g}: keep it between {MIN_PL_MHZ:g} and the "
                                 f"current {now:.1f} MHz, so the FPGA is never overclocked")

    def apply(self, wanted):
        """Change to `wanted`. Every original is in the state file, and the guardian watching,
        before the first change. Returns what was saved."""
        self.recover()
        self.check(wanted)
        if not wanted:
            return {}
        files = {}  # sysfs file -> original, in the order to put them back
        cores_off = []
        if "cores" in wanted:
            cores_off = self.other_cores()[wanted["cores"] - 1:]
            for core in cores_off:
                files[f"{core}/online"] = self.read(f"{core}/online")
        if "khz" in wanted:
            files[SETSPEED] = self.read(SETSPEED)  # after the cores: put back once they are on
        state = {"pid": os.getpid(), "pid_start": self.alive(os.getpid()),
                 "settings": describe(wanted), "files": files, "saved_at": time.time()}
        if "pl" in wanted:
            state["pl_mhz"] = float(self.clocks.fclk0_mhz)
        self._save_state(state)
        try:
            if self.start_guardian:
                self.guardian = spawn_guardian(self.state_file, self.cpu)
            for core in reversed(cores_off):
                self.write(f"{core}/online", 0)
            if "khz" in wanted:
                self.write(SETSPEED, wanted["khz"])
            if "pl" in wanted:
                self.clocks.fclk0_mhz = wanted["pl"]
        except BaseException:
            self.restore()
            raise
        log.info("settings %s applied; originals saved in %s", describe(wanted), self.state_file)
        return state

    def restore(self, state=None):
        """Put back every original in the state file, then remove it. Safe to call twice."""
        state = state if state is not None else self.load_state()
        if state is None:
            return False
        failed = []
        for rel, value in state.get("files", {}).items():
            try:
                if self.read(rel) != value:
                    self.write(rel, value)
            except OSError as exc:
                failed.append(f"{rel}: {exc}")
        if "pl_mhz" in state:
            try:
                self.clocks.fclk0_mhz = state["pl_mhz"]
            except Exception as exc:
                failed.append(f"fclk0: {exc}")
        if failed:  # keep the file so the next start tries again
            log.error("could not put back %s; kept %s", "; ".join(failed), self.state_file)
            return False
        self.state_file.unlink(missing_ok=True)
        log.info("settings %s put back to the originals", state.get("settings", "?"))
        if self.guardian is not None:  # it sees the file gone within GUARD_POLL_S and exits
            try:
                self.guardian.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.warning("guardian %d has not exited yet", self.guardian.pid)
            self.guardian = None
        return True

    def recover(self):
        """Put back what a killed run left changed. Refuse if that run is still going."""
        state = self.load_state()
        if state is None:
            return False
        pid = state.get("pid")
        if pid != os.getpid() and pid is not None and self.alive(pid) == state.get("pid_start"):
            raise RuntimeError(f"settings {state.get('settings')} are in use by process {pid}; "
                               f"stop it first")
        log.warning("putting back settings %s left by process %s, which was killed",
                    state.get("settings"), pid)
        return self.restore(state)


def units(values):
    """Settings.now()'s values as the dashboard shows them: {'cpu_mhz', 'cores', 'pl_mhz'}."""
    return {"cpu_mhz": round(values["khz"] / 1000) if "khz" in values else None,
            "cores": values.get("cores"),
            "pl_mhz": round(values["pl"], 1) if values.get("pl") is not None else None}


def tuning(settings, state_files=(STATE_FILE, PL_STATE_FILE)):
    """{'now': ..., 'was': ...}: the board's settings now, and before any run changed them.

    'was' is 'now' with every original still saved in a state file put in its place.
    """
    now = settings.now()
    was = dict(now)
    for path in state_files:
        try:
            state = json.loads(pathlib.Path(path).read_text())
        except (OSError, ValueError):
            continue
        files = state.get("files", {})
        if SETSPEED in files:
            was["khz"] = int(files[SETSPEED])
        was["cores"] += sum(1 for rel, value in files.items()
                            if rel.endswith("/online") and value == "1")
        if "pl_mhz" in state:
            was["pl"] = state["pl_mhz"]
    return {"now": units(now), "was": units(was)}


def spawn_guardian(state_file, cpu_root=CPU_ROOT):
    """Start the guardian for this process, detached so it outlives us."""
    GUARD_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(GUARD_LOG, "a") as out:
        return subprocess.Popen(
            [sys.executable, str(pathlib.Path(__file__).resolve()), "guard",
             str(os.getpid()), str(state_file), str(cpu_root)],
            stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
            close_fds=True)


def guard(owner, state_file, cpu_root=CPU_ROOT, poll=GUARD_POLL_S, alive=process_start):
    """Wait for `owner` to go, then put back whatever its state file still holds."""
    settings = Settings(cpu_root, state_file, start_guardian=False, alive=alive)
    stopping = []

    def stop(signum, frame):
        stopping.append(signum)

    if hasattr(signal, "SIGHUP"):  # not on Windows, where the tests run
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    while True:
        state = settings.load_state()
        if state is None or state.get("pid") != owner:
            return "nothing to put back"  # the owner put them back itself
        if stopping:
            settings.restore(state)
            return f"stopped by signal {stopping[0]}: put back {state.get('settings')}"
        if alive(owner) != state.get("pid_start"):
            settings.restore(state)
            return f"process {owner} died: put back {state.get('settings')}"
        time.sleep(poll)


def main(argv):
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    if argv[:1] == ["guard"]:
        owner, state_file, cpu_root = int(argv[1]), argv[2], argv[3]
        log.info("guardian %d watching process %d", os.getpid(), owner)
        log.info("guardian %d: %s", os.getpid(), guard(owner, state_file, cpu_root))
        return 0
    owners = [Settings(state_file=f, start_guardian=False) for f in (STATE_FILE, PL_STATE_FILE)]
    if argv[:1] == ["restore"]:
        for settings in owners:
            state = settings.load_state()
            if state is None:
                print(f"{settings.state_file.name}: nothing to put back")
            elif settings.recover():
                print(f"{settings.state_file.name}: put back {state.get('settings')}")
        return 0
    if argv[:1] == ["show"]:
        print(json.dumps({**tuning(owners[0]),
                          **{s.state_file.name: s.load_state() for s in owners}}, indent=1))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
