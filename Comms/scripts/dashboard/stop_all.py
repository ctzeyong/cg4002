"""Press Ctrl-C in the relay's and the board client's windows, for --stop-pipeline."""

import json
import re
import subprocess
import sys
import time

# what --stop-pipeline stops, by script name, and the name it prints
PIPELINE_SCRIPTS = {"pipeline_board_broker.py": "the relay", "run_board.py": "the board client"}
# run_board.py stops the board client over SSH before it exits, which takes a few seconds
PIPELINE_STOP_S = 20
FIND_TIMEOUT_S = 15
FIND_PROCESSES = (
    "Get-CimInstance Win32_Process | "
    "Select-Object ProcessId, ParentProcessId, CommandLine | ConvertTo-Json -Compress"
)
# python, the venv's python that started it, then the pane's PowerShell, with one to spare
PARENTS_CHECKED = 4
# the interpreter, then the first argument, each quoted or not
SCRIPT_ARG_PATTERN = re.compile(r'\s*(?:"[^"]*"|\S+)\s+(?:"([^"]*)"|(\S+))')
SYNCHRONIZE = 0x00100000
# run in a helper process, since attaching to another console means leaving our own
CTRL_C_HELPER = """
import ctypes, sys
kernel32 = ctypes.windll.kernel32
kernel32.FreeConsole()
if not kernel32.AttachConsole(int(sys.argv[1])):
    sys.exit(1)
kernel32.SetConsoleCtrlHandler(None, True)
sent = kernel32.GenerateConsoleCtrlEvent(0, 0)
kernel32.FreeConsole()
sys.exit(0 if sent else 2)
"""


def find_pipeline(run_id):
    """Return {script: pid} for the relay and board client that run `run_id` started."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", FIND_PROCESSES],
        capture_output=True, text=True, timeout=FIND_TIMEOUT_S,
    )
    found = json.loads(result.stdout or "[]")
    return pick_pipeline(found if isinstance(found, list) else [found], run_id)


def pick_pipeline(processes, run_id):
    """Pick one pid per pipeline script from Win32_Process rows, only from run `run_id`.

    The venv's python.exe starts a second python with the same command line in the same
    console, so one pid per script is enough, and sending to both would be a second Ctrl-C.
    """
    by_pid = {process["ProcessId"]: process for process in processes}
    picked = {}
    for process in processes:
        script = script_name(str(process.get("CommandLine") or ""))
        if script not in PIPELINE_SCRIPTS or script in picked:
            continue
        if started_by(process, by_pid, run_id):
            picked[script] = process["ProcessId"]
    return picked


def started_by(process, by_pid, run_id):
    """Return True if a parent of `process` has `run_id` in its command line, as run_all sets."""
    parent = by_pid.get(process.get("ParentProcessId"))
    for _ in range(PARENTS_CHECKED):
        if parent is None:
            return False
        if run_id in str(parent.get("CommandLine") or ""):
            return True
        parent = by_pid.get(parent.get("ParentProcessId"))
    return False


def script_name(command_line):
    """Return the file name of the script a python command line runs, or ""."""
    match = SCRIPT_ARG_PATTERN.match(command_line)
    if not match:
        return ""
    script = match.group(1) or match.group(2)
    return script.replace("\\", "/").rsplit("/", 1)[-1]


def send_ctrl_c(pid):
    """Press Ctrl-C in the console of process `pid`. Return True if it was sent."""
    try:
        helper = subprocess.run(
            [sys.executable, "-c", CTRL_C_HELPER, str(pid)], timeout=FIND_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return helper.returncode == 0


def wait_for_exit(pid, seconds):
    """Wait until process `pid` has ended. Return False if it is still running after `seconds`."""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return True  # already gone
    try:
        return kernel32.WaitForSingleObject(handle, int(seconds * 1000)) == 0
    finally:
        kernel32.CloseHandle(handle)


def stop_pipeline(run_id, find=find_pipeline, send=send_ctrl_c, wait=wait_for_exit, say=print):
    """Press Ctrl-C once in the consoles of run `run_id`'s relay and board client, then wait."""
    try:
        running = find(run_id)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        say(f"could not look for the relay and board client ({exc}), so stop them by hand")
        return
    if not running:
        say("No relay or board client from this run is running.")
        return
    for script, pid in running.items():
        sent = send(pid)
        say(f"Ctrl-C sent to {PIPELINE_SCRIPTS[script]}" if sent
            else f"could not reach {PIPELINE_SCRIPTS[script]} (pid {pid})")
    deadline = time.monotonic() + PIPELINE_STOP_S
    for script, pid in running.items():
        stopped = wait(pid, max(0.0, deadline - time.monotonic()))
        say(f"{PIPELINE_SCRIPTS[script]} stopped" if stopped
            else f"{PIPELINE_SCRIPTS[script]} still running after {PIPELINE_STOP_S}s")
