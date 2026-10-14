"""The board's log has to survive the SSH session that was watching it.

`ultra96/tcp_client.py` logged to the console only until 2026-09-17. The board is
only ever reached over SSH through the jump host, so when that session dies -- a VPN
drop, a dropped link, a closed laptop -- the output goes with it and the process takes
a SIGHUP. The failures most worth studying were exactly the ones that left no
evidence, and it was a link-recovery test that finally made that obvious.

These tests pin the two decisions that are easy to undo by accident:

  - the file handler exists at all, and actually receives records;
  - it lands under ``~``, never ``/tmp``, which the board wipes on reboot
    (``notes/ultra96_setup.md``);
  - configuration A and configuration B write to *different* files. Both clients now
    live on the board, and running the wrong one is a documented failure whose only
    symptom is a silent phone -- separate logs make "which client was actually
    running" answerable after the fact rather than a guess.

`setup_logging` mutates root-logger state and starts a thread, so every test here
saves and restores that state rather than leaving pytest's own logging wrecked.
"""

import atexit
import logging
import pathlib
import re
import time

import pytest

import board_logging
import tcp_client
import tcp_client_mqtt


def _stop(listener):
    """Stop a listener and drop the atexit hook `setup_logging` registered for it.

    Without the unregister, stopping here leaves a second `stop` queued for
    interpreter exit, where `QueueListener.stop` joins a thread it has already
    cleared -- an ignored AttributeError printed after every pytest run. Production
    never hits it: nothing there stops the listener early, so atexit fires once.
    """
    atexit.unregister(listener.stop)
    try:
        listener.stop()
    except Exception:
        pass


@pytest.fixture
def call_setup_logging():
    """Call `setup_logging` against a clean root logger, then restore pytest's.

    Two traps, both of which made an earlier version of this file pass vacuously:

    - `setup_logging` is a no-op once the root logger has handlers (the basicConfig
      rule), and **pytest's logging plugin adds its capture handler during the call
      phase, i.e. after fixtures have run**. Clearing handlers in fixture setup is
      therefore too early: by the time the test body calls in, pytest's handler is
      back and the function under test quietly does nothing.
    - So the handlers are cleared *inside* this helper, immediately before the call,
      and the snapshot taken there is what gets restored -- pytest's handler included,
      so its own teardown still finds what it expects.
    """
    root = logging.getLogger()
    saved_level = root.level
    saved_listeners = board_logging._log_listeners[:]
    snapshot = None

    def call(log_file, verbose=False, colour=None):
        nonlocal snapshot
        if snapshot is None:
            snapshot = root.handlers[:]
        root.handlers = []
        board_logging._log_listeners = []
        opened = board_logging.setup_logging(verbose, log_file, colour=colour)
        assert board_logging._log_listeners, (
            "setup_logging installed nothing -- it hit its no-op guard, so this test "
            "would prove nothing about the implementation"
        )
        return board_logging._log_listeners, opened

    yield call

    for listener in board_logging._log_listeners:
        _stop(listener)
    if snapshot is not None:
        root.handlers = snapshot
    root.level = saved_level
    board_logging._log_listeners = saved_listeners


def test_records_reach_the_file(tmp_path, call_setup_logging):
    listeners, opened = call_setup_logging(tmp_path / "nested" / "board.log")
    logging.getLogger("probe").info("hello from the board")

    # One listener thread drains the queue; stopping it flushes and joins.
    for listener in listeners:
        _stop(listener)

    assert opened.exists(), "no log file was created"
    assert "hello from the board" in opened.read_text(encoding="utf-8")


def test_each_run_gets_its_own_file_under_a_utc_date_folder(tmp_path, call_setup_logging):
    """`logs/board.log` becomes `logs/<UTC date>/board_<UTC time>Z.log`."""
    stem = tmp_path / "board.log"

    _listeners, opened = call_setup_logging(stem)

    assert opened != stem, "the stem itself must not be written"
    assert opened.parent.parent == tmp_path
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", opened.parent.name), opened.parent.name
    assert re.fullmatch(r"board_\d{6}Z\.log", opened.name), opened.name
    # The folder name must be the UTC day, not the board's local one.
    assert opened.parent.name == time.strftime("%Y-%m-%d", time.gmtime())


def test_two_runs_in_the_same_second_do_not_share_a_file(tmp_path):
    """Otherwise "one file per run" quietly becomes two runs interleaved."""
    stem = tmp_path / "board.log"
    now = time.gmtime()

    first = board_logging.run_log_path(stem, now)
    first.parent.mkdir(parents=True, exist_ok=True)
    first.touch()
    second = board_logging.run_log_path(stem, now)

    assert second != first


def test_the_line_format_is_utc_and_says_so():
    """The board runs UTC and the laptop SGT; without this the two logs are 8 hours
    apart and unreadable side by side."""
    assert board_logging.LINE_FORMAT.startswith("%(asctime)sZ")

    record = logging.LogRecord("tcp_client", logging.INFO, "f.py", 1, "m", None, None)
    formatter = logging.Formatter(board_logging.LINE_FORMAT)
    formatter.converter = time.gmtime
    assert formatter.format(record).endswith("[tcp_client] m")
    assert "Z [" in formatter.format(record)


def test_parent_directory_is_created(tmp_path, call_setup_logging):
    """A first run on a fresh board must not fail on a missing directory."""
    _listeners, opened = call_setup_logging(tmp_path / "does" / "not" / "exist" / "board.log")

    assert opened.parent.is_dir()


def test_console_only_still_possible(call_setup_logging):
    """log_file=None keeps the old behaviour, for anything that must not write."""
    listeners, opened = call_setup_logging(None)

    assert opened is None
    handler_types = {
        type(handler) for listener in listeners for handler in listener.handlers
    }
    assert logging.StreamHandler in handler_types
    assert logging.FileHandler not in handler_types


def test_default_is_under_home_and_not_tmp():
    default = board_logging.DEFAULT_LOG_FILE

    assert default.is_absolute()
    assert pathlib.Path.home() in default.parents, (
        f"{default} is not under the home directory. /tmp is wiped on reboot, which "
        "loses the log of any failure that took the board down with it."
    )
    assert not str(default).startswith("/tmp")


def test_the_two_clients_do_not_share_a_log_file():
    """Configuration A and configuration B must be tellable apart afterwards."""
    assert tcp_client_mqtt.LOG_FILE != board_logging.DEFAULT_LOG_FILE
    assert tcp_client_mqtt.LOG_FILE.parent == board_logging.DEFAULT_LOG_FILE.parent


# --- colour: the board's copy of the laptop's scheme -----------------------


def test_the_board_log_file_never_gets_escape_codes(tmp_path, call_setup_logging):
    """This log is scp'd back and read with grep. `colour=True` is forced because
    it auto-detects off under pytest, which would make this pass vacuously."""
    log_file = tmp_path / "board.log"

    listeners, opened = call_setup_logging(log_file, colour=True)
    logging.getLogger("tcp_client").info("connected")
    logging.getLogger("tcp_client").warning("server closed the connection")
    for listener in listeners:
        _stop(listener)

    written = opened.read_text(encoding="utf-8")
    assert "connected" in written, "the file handler wrote nothing"
    assert "" not in written, f"escape codes reached the board log: {written!r}"


def test_the_boards_colour_scheme_matches_the_laptops():
    """The board's copy is duplicated because only `ultra96/` is deployed. Compared
    on behaviour, so either may be reformatted; only a rendering change is drift."""
    import logging_setup

    board = board_logging.ConsoleColourFormatter("%(message)s", enabled=True)
    laptop = logging_setup.ConsoleColourFormatter("%(message)s", enabled=True)

    # Every logger the board actually emits under either configuration.
    # `tcp_client_mqtt` was absent from the board's map until 2026-09-22, so
    # configuration B's AI event lines -- hop 4 of the chain -- rendered uncoloured.
    for name in ("tcp_client", "tcp_client_mqtt", "mqtt_publisher"):
        for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR):
            for progress in (False, True):
                record = logging.LogRecord(name, level, "f.py", 1, "m", None, None)
                if progress:
                    record.progress = True
                assert board.colour_for(record) == laptop.colour_for(record), (
                    f"the two copies disagree for {name} at level {level} "
                    f"(progress={progress})"
                )

    assert board_logging.PROGRESS == logging_setup.PROGRESS
    for attr in ("_RESET", "_YELLOW", "_BRIGHT_RED"):
        assert getattr(board_logging, attr) == getattr(logging_setup, attr), attr


def test_the_board_dims_its_periodic_counter_and_brightens_its_events():
    """The board runs one leg, so the weight axis is what earns its place here."""
    fmt = board_logging.ConsoleColourFormatter("%(message)s", enabled=True)

    counter = logging.LogRecord("tcp_client", logging.INFO, "f.py", 1, "m", None, None)
    counter.progress = True
    event = logging.LogRecord("tcp_client", logging.INFO, "f.py", 1, "m", None, None)

    counter_ix, event_ix = board_logging.LINK_COLOURS["tcp_client"]
    assert fmt.colour_for(counter) == board_logging._sgr(counter_ix)
    assert fmt.colour_for(event) == board_logging._sgr(event_ix)
