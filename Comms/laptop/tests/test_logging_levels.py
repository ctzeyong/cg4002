"""What `-v` means, what stays legible without it, and the console colour rules.
"""

import asyncio
import atexit
import json
import logging
import re
import time

import pytest

import logging_setup
import mqtt_client
import mqtt_publisher
import pipeline


# --- third-party floors ----------------------------------------------------


@pytest.fixture
def configure_logging(tmp_path):
    """Run the real setup_logging against a clean root logger, then restore.

    Handlers are cleared inside the returned callable, not in fixture setup:
    pytest adds its capture handler per test phase, so a fixture-time clear is
    too early and setup_logging hits its no-op guard.
    """
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved = {name: logging.getLogger(name).level for name in logging_setup.THIRD_PARTY_LEVELS}

    def apply(**kwargs):
        root.handlers = []
        return logging_setup.setup_logging(tmp_path / "audit.log", **kwargs)

    try:
        yield apply
    finally:
        for listener in logging_setup._log_listeners:
            atexit.unregister(listener.stop)  # stop() is not idempotent
            listener.stop()
        logging_setup._log_listeners.clear()
        root.handlers, root.level = saved_handlers, saved_level
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


@pytest.mark.parametrize("verbose", [False, True])
def test_asyncssh_is_floored_above_info_whatever_verbose_does(configure_logging, verbose):
    """asyncssh names the jump username and both hosts at INFO, so `-v` must not
    reach it. Only `tunnel.py -v` and `--debug-libs` lift the floor."""
    configure_logging(verbose=verbose)

    assert not logging.getLogger("asyncssh").isEnabledFor(logging.INFO)


@pytest.mark.parametrize("name", sorted(logging_setup.THIRD_PARTY_LEVELS))
def test_verbose_alone_does_not_unfloor_a_library(configure_logging, name):
    """`-v` dumps our packets; it is not a library-debug switch."""
    configure_logging(verbose=True)

    assert not logging.getLogger(name).isEnabledFor(logging.DEBUG), name


def test_debug_libs_is_what_unfloors_them(configure_logging):
    """The escape hatch, for debugging bleak or the SSH exchange."""
    configure_logging(verbose=True, debug_libs=True)

    assert logging.getLogger("bleak").isEnabledFor(logging.DEBUG)
    assert logging.getLogger("asyncssh").isEnabledFor(logging.DEBUG)


def test_our_own_loggers_still_reach_debug_under_verbose(configure_logging):
    """The floors must not have quietened the dump `-v` exists to produce."""
    configure_logging(verbose=True)

    assert logging.getLogger("ble_client").isEnabledFor(logging.DEBUG)
    assert logging.getLogger("pipeline").isEnabledFor(logging.DEBUG)


def test_nothing_is_floored_into_silence(configure_logging):
    """A floor above WARNING would hide a real failure."""
    configure_logging()

    for name in logging_setup.THIRD_PARTY_LEVELS:
        assert logging.getLogger(name).isEnabledFor(logging.WARNING), name


# --- one file per run, in a UTC date folder --------------------------------


def test_each_run_gets_its_own_file_under_a_utc_date_folder(configure_logging, tmp_path):
    """`logs/audit.log` becomes `logs/<UTC date>/audit_<UTC time>Z.log`."""
    opened = configure_logging()

    assert opened != tmp_path / "audit.log", "the stem itself must not be written"
    assert opened.parent.parent == tmp_path
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", opened.parent.name), opened.parent.name
    assert re.fullmatch(r"audit_\d{6}Z\.log", opened.name), opened.name
    # UTC, not the laptop's SGT: the board files under the same name.
    assert opened.parent.name == time.strftime("%Y-%m-%d", time.gmtime())


def test_two_runs_in_the_same_second_do_not_share_a_file(tmp_path):
    """Otherwise "one file per run" quietly becomes two runs interleaved."""
    stem = tmp_path / "pipeline.log"
    now = time.gmtime()

    first = logging_setup.run_log_path(stem, now)
    first.parent.mkdir(parents=True, exist_ok=True)
    first.touch()
    second = logging_setup.run_log_path(stem, now)

    assert second != first


def test_one_process_cannot_open_two_files(configure_logging, tmp_path):
    """A run is one process, not one connection, so nothing a reconnect does can
    rotate the file: the second call hits the no-op guard and returns None.

    Called directly rather than through the fixture, which clears the root handlers
    each time and would therefore always re-enter -- testing the fixture, not the
    guard."""
    opened = configure_logging()

    again = logging_setup.setup_logging(tmp_path / "audit.log")

    assert again is None, "setup_logging must be a no-op once handlers exist"
    assert opened.exists()
    assert len(logging_setup._log_listeners) == 1


def test_the_line_format_is_utc_and_says_so():
    """The laptop runs SGT and the board UTC; without this the two logs are 8 hours
    apart and unreadable side by side."""
    assert logging_setup.LINE_FORMAT.startswith("%(asctime)sZ")

    record = logging.LogRecord("pipeline", logging.INFO, "f.py", 1, "m", None, None)
    formatter = logging.Formatter(logging_setup.LINE_FORMAT)
    formatter.converter = time.gmtime
    assert "Z [" in formatter.format(record)


def test_the_written_timestamps_are_utc(configure_logging):
    """End to end: a run started now must stamp the UTC hour, not the local one."""
    opened = configure_logging()
    logging.getLogger("pipeline").info("probe")

    for listener in logging_setup._log_listeners:
        listener.stop()
        atexit.unregister(listener.stop)
    logging_setup._log_listeners.clear()

    line = opened.read_text(encoding="utf-8").splitlines()[0]
    assert line.startswith(time.strftime("%Y-%m-%d %H:%M", time.gmtime())), line
    assert "Z [pipeline] probe" in line


# --- colour ----------------------------------------------------------------


def _record(name, level=logging.INFO, progress=False, msg="x"):
    record = logging.LogRecord(name, level, "f.py", 1, msg, None, None)
    if progress:
        record.progress = True
    return record


def test_the_log_file_never_gets_escape_codes(configure_logging, tmp_path):
    """Escape codes in logs/*.log would break grep and every measurement made
    with one. `colour=True` is forced because it auto-detects off under pytest."""
    log_file = configure_logging(colour=True)

    logging.getLogger("ble_client").info("node 1: connected")
    logging.getLogger("tcp_server").error("protocol error; dropping client")

    for listener in logging_setup._log_listeners:  # flush through the handlers
        listener.stop()
        atexit.unregister(listener.stop)
    logging_setup._log_listeners.clear()

    written = log_file.read_text(encoding="utf-8")
    assert "node 1: connected" in written, "the file handler wrote nothing"
    assert "\033" not in written, f"escape codes reached the log file: {written!r}"


def test_every_link_gets_its_own_colour():
    """One logger per distinct leg; the shared hues in LINK_COLOURS are the same
    leg seen twice (both ends, or either board client)."""
    legs = ["ble_client", "tcp_server", "mqtt_client", "tunnel", "pipeline"]
    colours = {logging_setup.LINK_COLOURS[name] for name in legs}

    assert len(colours) == len(legs), f"{len(legs)} legs share {len(colours)} colours"


def test_high_rate_records_and_per_event_records_differ_in_weight():
    """The weight axis. Without it colour paints the 89% that is counters. BLE is the
    exception (one shade per board, below), so this checks the Ultra96 leg."""
    fmt = logging_setup.ConsoleColourFormatter(enabled=True)

    event = fmt.colour_for(_record("tcp_server"))
    progress = fmt.colour_for(_record("tcp_server", progress=True))
    dump = fmt.colour_for(_record("tcp_server", level=logging.DEBUG))

    assert progress == dump, "the two high-rate classes must read the same"
    # The index carries the weight, not SGR 2, which terminals widely ignore.
    counter, highlight = logging_setup.LINK_COLOURS["tcp_server"]
    assert progress == logging_setup._sgr(counter)
    assert event == logging_setup._sgr(highlight)
    assert counter != highlight


def test_the_legs_a_configuration_b_laptop_shows_are_all_distinct():
    """B runs no MQTT leg here, so the console is only these four. They came out
    green/cyan/blue/grey -- two of them blue -- before the 256-colour palette."""
    legs = ["ble_client", "tcp_server", "tunnel", "pipeline_board_broker"]
    brights = {logging_setup.LINK_COLOURS[name][1] for name in legs}
    brights |= {pair[1] for pair in logging_setup.NODE_COLOURS.values()}  # each FireBeetle

    assert len(brights) == len(legs) + 2, f"{legs} and the boards share {len(brights)} colours"


def test_each_firebeetle_prints_in_one_green_of_its_own():
    """A colour per connection, and each board is one. One shade per
    board, since a second shade per board read as a second board, and a third green
    for lines about both (node None). None may be any other leg's colour or a level's."""
    fmt = logging_setup.ConsoleColourFormatter(enabled=True)
    by_board = {}
    for node in (1, 2, None):
        shades = set()
        for level in (logging.DEBUG, logging.INFO):
            for progress in (False, True):
                record = _record("ble_client", level, progress=progress)
                if node is not None:
                    record.node = node
                shades.add(fmt.colour_for(record))
        assert len(shades) == 1, f"board {node} prints in {len(shades)} shades"
        by_board[node] = shades.pop()

    others = {
        logging_setup._sgr(i)
        for name, pair in logging_setup.LINK_COLOURS.items()
        if name not in ("ble_client", "file_transfer")
        for i in pair
    }
    others |= {logging_setup._YELLOW, logging_setup._BRIGHT_RED}
    assert len(set(by_board.values())) == 3, "two of board 1, board 2 and shared match"
    assert not set(by_board.values()) & others


def test_level_outranks_the_leg_colour():
    """A degrading link must not be camouflaged in its own colour."""
    fmt = logging_setup.ConsoleColourFormatter(enabled=True)

    assert fmt.colour_for(_record("ble_client", logging.WARNING)) == logging_setup._YELLOW
    assert fmt.colour_for(_record("mqtt_client", logging.ERROR)) == logging_setup._BRIGHT_RED
    assert fmt.colour_for(_record("asyncssh", logging.ERROR)) == logging_setup._BRIGHT_RED


def test_a_progress_marked_warning_is_still_a_warning():
    """Pins the precedence, so a periodic line that escalates cannot go dim."""
    fmt = logging_setup.ConsoleColourFormatter(enabled=True)

    assert fmt.colour_for(_record("ble_client", logging.WARNING, progress=True)) == (
        logging_setup._YELLOW
    )


def test_colour_disabled_emits_no_escape_codes_at_all():
    """Not even a bare reset."""
    fmt = logging_setup.ConsoleColourFormatter("%(name)s %(message)s", enabled=False)

    for record in (_record("ble_client"), _record("tcp_server", logging.ERROR)):
        assert "\033" not in fmt.format(record)


def test_an_uncoloured_logger_gets_no_reset_either():
    fmt = logging_setup.ConsoleColourFormatter("%(message)s", enabled=True)

    assert fmt.format(_record("bleak", msg="discovery")) == "discovery"


def test_colour_is_off_when_the_stream_is_not_a_terminal(monkeypatch):
    """`python pipeline.py > run.log` must collect clean text."""
    monkeypatch.delenv("NO_COLOR", raising=False)

    class NotATerminal:
        def isatty(self):
            return False

    assert logging_setup.console_supports_colour(NotATerminal()) is False


def test_no_color_is_honoured(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")

    class Terminal:
        def isatty(self):
            return True

    assert logging_setup.console_supports_colour(Terminal()) is False


# --- the keypress chain, at INFO -------------------------------------------


@pytest.mark.asyncio
async def test_downlink_routing_is_legible_without_verbose(caplog):
    """Hop 4: the envelope and topic. Once per event, so not DEBUG."""
    downlink, outbound = asyncio.Queue(maxsize=8), asyncio.Queue(maxsize=8)
    stop = asyncio.Event()
    task = asyncio.create_task(
        pipeline.forward_downlink(downlink, outbound, mqtt_client.MqttStats(), stop)
    )
    try:
        with caplog.at_level(logging.INFO, logger="pipeline"):
            downlink.put_nowait({"type": "AI_EVENT", "player": 1, "gesture": "circle"})
            await asyncio.wait_for(outbound.get(), 2.0)
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    routed = [r for r in caplog.records if "downlink seq=" in r.message]
    assert routed, "the envelope/topic line is not visible at INFO"
    assert routed[0].levelno == logging.INFO
    assert "game/gesture/1" in routed[0].getMessage()


class _AcceptingClient:
    """Stand-in for an aiomqtt client that accepts every publish."""

    def __init__(self):
        self.published = []

    async def publish(self, topic, payload=None, qos=0):
        self.published.append((topic, json.loads(payload)))


async def _drain_one(loop_coro, published, stop):
    """Run a publish loop until it has published once, then stop it."""
    task = asyncio.create_task(loop_coro)
    try:
        for _ in range(2000):
            if published:
                return
            await asyncio.sleep(0)
        pytest.fail("the publish loop never published")
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_laptop_publish_is_legible_without_verbose(caplog):
    """Hop 5 in the laptop-broker setup: reached the broker, not just the outbound queue."""
    outbound = asyncio.Queue(maxsize=8)
    stop = asyncio.Event()
    client = _AcceptingClient()
    envelope = mqtt_client.build_envelope({"gesture": "circle"}, 0)
    outbound.put_nowait(("game/gesture/1", envelope))

    with caplog.at_level(logging.INFO, logger="mqtt_client"):
        await _drain_one(
            mqtt_client._publish_loop(client, outbound, [], mqtt_client.MqttStats(), stop),
            client.published,
            stop,
        )

    lines = [r for r in caplog.records if "published" in r.message]
    assert lines, "the publish line is not visible at INFO"
    assert lines[0].levelno == logging.INFO
    assert envelope["event_id"] in lines[0].getMessage()


@pytest.mark.asyncio
async def test_board_publish_is_legible_without_verbose(caplog):
    """Hop 5 on the board, which had no publish line at any level. The two
    setups never share a file, so each needs its own test."""
    outbound = asyncio.Queue(maxsize=8)
    stop = asyncio.Event()
    client = _AcceptingClient()
    stats = mqtt_publisher.PublishStats()
    mqtt_publisher.enqueue_event(
        outbound, "game/gesture/2", {"event_id": "u96:1:0", "payload": {"g": "shield"}}, stats
    )

    with caplog.at_level(logging.INFO, logger="mqtt_publisher"):
        await _drain_one(
            mqtt_publisher._publish_loop(client, outbound, [], stats, stop),
            client.published,
            stop,
        )

    lines = [r for r in caplog.records if "published" in r.message]
    assert lines, "the publish line is not visible at INFO"
    assert lines[0].levelno == logging.INFO
    assert "u96:1:0" in lines[0].getMessage()


@pytest.mark.asyncio
async def test_an_envelope_without_an_event_id_does_not_kill_the_publish_loop():
    """The publish line must never raise. Raising after a successful publish would
    make the supervisor republish and re-raise forever."""
    for module, stats in (
        (mqtt_client, mqtt_client.MqttStats()),
        (mqtt_publisher, mqtt_publisher.PublishStats()),
    ):
        outbound = asyncio.Queue(maxsize=8)
        stop = asyncio.Event()
        client = _AcceptingClient()
        # Each module's own enqueue, because the two queues hold different item shapes.
        module.enqueue_event(outbound, "game/gesture/1", {"payload": {"no": "event_id"}}, stats)

        await _drain_one(
            module._publish_loop(client, outbound, [], stats, stop), client.published, stop
        )
        assert stats.published == 1, f"{module.__name__} did not publish"
