"""Tests for scripts/dashboard, the screen that sums up both logs, and run_all.ps1.

It parses log lines and broker messages, so most tests feed it lines in the real format.
The subscriber's command line is run against a real Mosquitto.
"""

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import asyncssh
import pytest

from helpers import DEADLINE_S, wait_until

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import dashboard.__main__ as app  # noqa: E402
from dashboard import board_link, drawing, relay_log, stop_all  # noqa: E402
from test_glove_status import MOSQUITTO, Broker  # noqa: E402

T0 = 1793700000.0  # a whole second, in UTC
ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def stamp(offset):
    """Return the relay's timestamp for T0 + offset seconds."""
    moment = T0 + offset
    millis = int(moment % 1 * 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(moment)) + f",{millis:03d}Z"


def line(offset, name, message):
    return f"{stamp(offset)} [{name}] {message}"


def counter(node, rate=100.5, missed=0, dup=0, sent=0, echoed=0):
    return (
        f"node {node}: n=1000 seq=12 rate={rate} Hz 20.9 kbps missed={missed} dup={dup} "
        f"dropped=0 bad_crc=0 sent={sent} echoed={echoed}"
    )


def relay_from(*lines):
    relay = relay_log.Relay(Path("pipeline_board_broker_000000Z.log"))
    for text in lines:
        relay.apply(text)
    return relay


def screen(dashboard_state, now, mono_now=None, width=120):
    mono_now = time.monotonic() if mono_now is None else mono_now
    lines = drawing.render(dashboard_state, now, mono_now, width=width, height=80)
    return "\n".join(ANSI.sub("", text) for text in lines)


def row(text, label):
    return next(line_ for line_ in text.splitlines() if line_.strip().startswith(label))


def with_relay(relay):
    state = app.Dashboard(watch_board=False)
    state.relay = relay
    return state


# --------------------------------------------------------------------------
# The relay's log
# --------------------------------------------------------------------------


def test_a_glove_streams_while_its_counter_is_fresh_then_shows_its_last_state():
    relay = relay_from(
        line(0, "ble_client", "node 1: connected"),
        line(0.1, "ble_client", "node 1: subscribed; mtu=128"),
        line(1, "ble_client", counter(1, rate=99.5, missed=2, sent=4, echoed=3)),
    )

    fresh = row(screen(with_relay(relay), T0 + 1 + relay_log.STALE_S), "Glove 1")
    stale = row(screen(with_relay(relay), T0 + 1.1 + relay_log.STALE_S), "Glove 1")

    assert "streaming" in fresh and "99.5 Hz" in fresh and "miss 2" in fresh
    assert "buzz 3/4" in fresh
    assert "connected, no data" in stale and "Hz" not in stale


@pytest.mark.parametrize(
    "message, state",
    [
        ("scanning for b1160001-0000-4000-8000-000000000001 (up to 10s)", "scanning"),
        ("nothing advertising that service", "not found"),
        ("session ended after 10s", "disconnected"),
        ("peripheral disconnected", "disconnected"),
        ("retrying in 4s", "retrying"),
        ("supervisor stopped after 19726 packets (missed=0 dup=0 dropped=0)", "stopped"),
    ],
)
def test_each_ble_line_sets_the_glove_state(message, state):
    relay = relay_from(line(0, "ble_client", f"node 2: {message}"))

    assert relay.gloves[2].state == state
    assert state in row(screen(with_relay(relay), T0 + 1), "Glove 2")


def test_board_tcp_stays_connected_when_a_new_client_replaces_the_old_one():
    relay = relay_from(
        line(0, "tcp_server", "listening on ('127.0.0.1', 8888)"),
        line(1, "tcp_server", "client connected: ('127.0.0.1', 1)"),
        line(2, "tcp_server", "new client ('127.0.0.1', 2) supersedes ('127.0.0.1', 1)"),
        line(2, "tcp_server", "client connected: ('127.0.0.1', 2)"),
        line(3, "tcp_server", "client disconnected: ('127.0.0.1', 1) (9 frames: heartbeats=9)"),
    )
    assert "connected" in row(screen(with_relay(relay), T0 + 4), "Board TCP")
    assert "not connected" not in row(screen(with_relay(relay), T0 + 4), "Board TCP")

    relay.apply(line(5, "tcp_server", "client disconnected: ('127.0.0.1', 2) (3 frames)"))
    assert "not connected" in row(screen(with_relay(relay), T0 + 6), "Board TCP")


def test_the_uplink_rate_shows_only_while_its_counter_is_fresh():
    relay = relay_from(
        line(0, "tcp_server", "client connected: ('127.0.0.1', 1)"),
        line(1, "pipeline", "uplink: forwarded=900 unsent=0 201.0 Hz 41.8 kbps queued=3"),
    )

    assert "uplink 201.0 Hz  queued 3" in row(screen(with_relay(relay), T0 + 2), "Board TCP")
    assert "uplink" not in row(screen(with_relay(relay), T0 + 5), "Board TCP")


@pytest.mark.parametrize(
    "message, state",
    [
        ("attempt 1: opening tunnel", "connecting"),
        ("tunnel open: board localhost:8888 -> laptop 127.0.0.1:8888", "up"),
        ("session ended after 40s (VPN down? port taken?)", "down"),
        ("reconnecting in 4s", "down, retrying"),
        ("authentication rejected: Permission denied", "login rejected"),
        ("tunnel supervisor stopped after 1 attempt(s)", "stopped"),
    ],
)
def test_each_tunnel_line_sets_the_tunnel_state(message, state):
    relay = relay_from(
        line(0, "tunnel", "tunnel open: board localhost:8888 -> laptop 127.0.0.1:8888"),
        line(1, "tunnel", message),
    )

    assert relay.tunnel == state


def test_only_warnings_are_listed_not_state_changes_counters_or_chatter():
    relay = relay_from(
        line(0, "ble_client", "node 1: connected"),
        line(1, "pipeline_board_broker", "VIBRATE node=1 pattern=1 -> CMD seq=5 queued"),
        line(1, "ble_client", "node 1: CMD seq=5 fields[:3]=[1, 0, 0]"),
        line(1, "ble_client", "node 1: VIBRATE seq=5 echoed, not forwarded"),
        line(2, "tcp_server", "board link: 10 frames received (heartbeats=7 dropped=0)"),
        line(2, "tcp_server", "client disconnected: ('127.0.0.1', 1) (9 frames: dropped=0)"),
        line(3, "ble_client", counter(1)),
        line(3, "pipeline", "uplink: node=1 SENSOR seq=12 ax=1 reserved=[0, 0] crc=0x1234"),
        line(4, "ble_client", "node 1: seq gap, expected 5 got 8 (missed 3)"),
        line(5, "ble_client", "node 1: supervisor stopped after 9 packets (dropped=0 cmd_failed=0)"),
        line(6, "asyncssh", "Connection lost"),
        line(7, "bleak.backends.winrt.client", "38:18:2B: unhandled services changed event"),
        line(8, "plumbing", "ignored GATT completion after cancel: Exception in callback"),
    )

    assert [text for _, text in relay.warnings] == [
        "[ble_client] node 1: seq gap, expected 5 got 8 (missed 3)",
        "[asyncssh] Connection lost",
        "[bleak.backends.winrt.client] 38:18:2B: unhandled services changed event",
    ]


def test_the_warnings_section_says_none_or_lists_them_with_a_count():
    relay = relay_from(line(0, "ble_client", "node 1: connected"))
    text = screen(with_relay(relay), T0)
    assert re.search(r"WARNINGS \(relay log\)\s+0 in this run\n\s+none", text)

    relay.apply(line(1, "ble_client", "node 2: peripheral disconnected"))
    text = screen(with_relay(relay), T0)
    assert "1 in this run" in text
    assert "10:00:01  [ble_client] node 2: peripheral disconnected" in text


def test_a_rejected_tunnel_login_is_listed_without_the_username():
    relay = relay_from(
        line(0, "tunnel", "authentication rejected: Permission denied for user student-id on j"),
    )

    assert relay.tunnel == "login rejected"
    assert relay.warnings[-1][1] == (
        "[tunnel] authentication rejected: Permission denied for user <username> on j"
    )


def test_only_the_newest_warnings_are_kept_but_all_are_counted():
    relay = relay_from(*(line(i, "ble_client", f"node 1: seq gap, expected 1 got {i}")
                         for i in range(10)))

    assert len(relay.warnings) == relay_log.WARNINGS_SHOWN
    assert relay.warning_count == 10
    assert relay.warnings[-1][1] == "[ble_client] node 1: seq gap, expected 1 got 9"


RELAY_MODULES = ("ble_client", "ble_packet", "tcp_server", "tunnel", "pipeline",
                 "pipeline_board_broker", "plumbing", "framing", "logging_setup")
LAPTOP = Path(__file__).resolve().parents[1]
SAMPLES = {"d": "7", "s": "boom", "r": "'boom'", "f": "3", "x": "1f"}


def logged_templates():
    """Yield (level, place, message format) for every log call in the relay's modules."""
    import ast

    for module in RELAY_MODULES:
        tree = ast.parse((LAPTOP / f"{module}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("debug", "info", "warning", "error", "exception")
                    and node.args):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                text = first.value
            elif isinstance(first, ast.JoinedStr):  # an f-string, with each {...} as %s
                text = "".join(part.value if isinstance(part, ast.Constant) else "%s"
                               for part in first.values)
            else:
                continue
            yield node.func.attr, f"{module}.py:{node.lineno}", text


def filled(template):
    return re.sub(r"%[-#0 +]*\d*(?:\.\d+)?([dsrfx])", lambda m: SAMPLES[m[1]], template)


# "node %d: %s" carries a packet error or an IMU problem, so it is checked with those texts
GENERIC = {"node %d: %s"}


def test_every_relay_warning_is_flagged_and_no_info_line_is():
    wrong = []
    found = {True: 0, False: 0}
    for level, place, template in logged_templates():
        if template in GENERIC:
            continue
        expected = level in ("warning", "error", "exception")
        found[expected] += 1
        if relay_log.is_warning("ble_client", filled(template)) != expected:
            wrong.append(f"{place} {level}: {template}")

    assert found[True] >= 30 and found[False] >= 30  # the source walk really found the calls
    assert wrong == []


def test_packet_errors_and_imu_problems_are_flagged():
    import ble_packet

    texts = list(ble_packet.IMU_PROBLEMS.values()) + ["unknown IMU status 9"]
    sensor = bytes([ble_packet.TYPE_SENSOR, 0])
    for data in (bytes(1), sensor + bytes(5), bytes(ble_packet.PACKET_LEN)):
        try:
            ble_packet.decode(data)
        except ValueError as exc:
            texts.append(str(exc))

    assert len(texts) >= 5  # both IMU problems, the unknown status and three packet errors
    for text in texts:
        assert relay_log.is_warning("ble_client", f"node 1: {text}"), text


def test_the_relay_shows_stopped_after_ctrl_c():
    relay = relay_from(
        line(0, "ble_client", counter(1)),
        line(1, "pipeline", "interrupt received; shutting down (Ctrl-C again to force)"),
    )

    assert "relay stopped" in row(screen(with_relay(relay), T0 + 2), "Relay log")


def test_an_old_log_shows_its_age_from_its_own_timestamps_not_from_when_it_was_read():
    relay = relay_from(line(0, "ble_client", counter(1)))

    text = screen(with_relay(relay), T0 + 600)

    assert "last line 10m ago" in row(text, "Relay log")
    assert "streaming" not in row(text, "Glove 1")


def test_lines_that_are_not_log_records_are_ignored():
    relay = relay_from(
        "Traceback (most recent call last):",
        '  File "ble_client.py", line 1, in <module>',
        line(0, "ble_client", "node 1: connected"),
    )

    assert relay.gloves[1].state == "connected"
    assert relay.last_line_at == T0


# --------------------------------------------------------------------------
# Following the log file
# --------------------------------------------------------------------------


def test_a_line_written_in_two_parts_is_applied_once_whole(tmp_path):
    path = tmp_path / "pipeline_board_broker_000000Z.log"
    relay = relay_log.Relay(path)
    whole = line(0, "ble_client", "node 1: connected")
    path.write_bytes(whole[:20].encode())

    offset, partial = relay_log.read_new_lines(path, 0, b"", relay.apply)
    assert relay.gloves.get(1) is None

    with open(path, "ab") as file:
        file.write(whole[20:].encode() + b"\r\n")
    offset, partial = relay_log.read_new_lines(path, offset, partial, relay.apply)

    assert relay.gloves[1].state == "connected"
    assert partial == b""


def test_a_file_that_shrank_is_read_again_from_the_start(tmp_path):
    path = tmp_path / "pipeline_board_broker_000000Z.log"
    path.write_text("\n".join([line(0, "ble_client", "node 1: connected")] * 3) + "\n")
    relay = relay_log.Relay(path)
    offset, _ = relay_log.read_new_lines(path, 0, b"", relay.apply)

    path.write_text(line(1, "ble_client", "node 1: peripheral disconnected") + "\n")
    relay_log.read_new_lines(path, offset, b"", relay.apply)

    assert relay.gloves[1].state == "disconnected"


WRITER = """
import logging, sys, time
handler = logging.FileHandler(sys.argv[1], encoding="utf-8")
log = logging.getLogger("ble_client")
log.addHandler(handler)
log.setLevel(logging.INFO)
for i in range(300):
    log.info("2026-11-02 10:00:00,000Z [ble_client] node 1: seq gap, expected 1 got %d", i)
    time.sleep(0.002)
"""


def test_reading_never_gets_in_the_way_of_the_relay_writing_its_log(tmp_path):
    path = tmp_path / "pipeline_board_broker_000000Z.log"
    path.touch()
    relay = relay_log.Relay(path)
    writer = subprocess.Popen([sys.executable, "-c", WRITER, str(path)], stderr=subprocess.PIPE)
    offset, partial = 0, b""
    while writer.poll() is None:
        offset, partial = relay_log.read_new_lines(path, offset, partial, relay.apply)
    offset, partial = relay_log.read_new_lines(path, offset, partial, relay.apply)

    assert writer.returncode == 0, writer.stderr.read()
    assert relay.warnings[-1][1] == "[ble_client] node 1: seq gap, expected 1 got 299"
    assert relay.warning_count == 300


@pytest.mark.asyncio
async def test_the_follower_moves_to_a_newer_relay_log_and_starts_from_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(relay_log, "FIND_LOG_S", 0.05)
    monkeypatch.setattr(relay_log, "LOG_POLL_S", 0.02)
    old = tmp_path / "2026-11-01" / "pipeline_board_broker_100000Z.log"
    new = tmp_path / "2026-11-02" / "pipeline_board_broker_090000Z.log"
    other = tmp_path / "2026-11-02" / "dashboard_090000Z.log"
    for path in (old, new, other):
        path.parent.mkdir(exist_ok=True)
    now = time.time()
    old.write_text(line(0, "ble_client", "node 1: connected") + "\n")
    os.utime(old, (now - 100, now - 100))
    # newer than every relay log, so only the name keeps it out
    other.write_text(line(0, "ble_client", "node 2: connected") + "\n")
    os.utime(other, (now + 3600, now + 3600))
    state = app.Dashboard(watch_board=False)
    stop = asyncio.Event()
    task = asyncio.create_task(relay_log.follow_relay_log(state, tmp_path, stop))
    try:
        await wait_until(lambda: state.relay.path == old and 1 in state.relay.gloves)

        new.write_text(line(5, "ble_client", "node 2: scanning for x (up to 10s)") + "\n")
        await wait_until(lambda: state.relay.path == new and 2 in state.relay.gloves)

        assert 1 not in state.relay.gloves
        assert state.relay.gloves[2].state == "scanning"
    finally:
        stop.set()
        await asyncio.wait_for(task, DEADLINE_S)


# --------------------------------------------------------------------------
# The board's broker
# --------------------------------------------------------------------------


def envelope_json(payload):
    return json.dumps({"event_id": "ultra96-relay:abc:1", "source": "ultra96-relay", "seq": 1,
                       "ts": T0, "payload": payload})


def board_state(*lines, now=100.0):
    state = app.Dashboard()
    state.board.set_link("connected", ok=True)
    for text in lines:
        state.board.apply(text, now)
    return state


def test_glove_states_come_from_the_retained_status_message():
    state = board_state("status/gloves " + envelope_json({"player1": "ready", "player2": "down"}))

    assert "P1 ready" in row(screen(state, T0, 100.0), "Glove state")
    assert "P2 down" in row(screen(state, T0, 100.0), "Glove state")


def test_glove_states_read_unknown_while_the_board_link_is_down():
    state = board_state("status/gloves " + envelope_json({"player1": "ready", "player2": "ready"}))
    state.board.set_link("down: connection lost")

    assert "P1 unknown" in row(screen(state, T0, 100.0), "Glove state")


def test_gestures_go_to_the_player_in_the_topic_newest_first_and_only_the_last_three():
    gestures = ["mage", "rogue", "shield", "fireball"]
    state = board_state(*("game/gesture/2 " + envelope_json({"player": 1, "gesture": g})
                          for g in gestures))
    state.board.apply("game/gesture/0 " + envelope_json({"gesture": "hero"}), 100.0)

    assert [g for g, _ in state.board.gestures[2]] == ["fireball", "shield", "rogue"]
    assert not state.board.gestures[1]
    assert "fireball" in row(screen(state, T0, 104.0), "Gestures")


def test_phone_ready_and_echo_are_timed_per_player():
    state = board_state("viz/ready/1 {}", now=90.0)
    state.board.apply('viz/echo/2 {"seq": 1}', 95.0)

    text = screen(state, T0, 100.0)
    assert re.search(r"Last ready\s+10s ago\s+none seen", text)
    assert re.search(r"Last echo\s+none seen\s+5s ago", text)


@pytest.mark.parametrize(
    "message",
    [
        "status/gloves (null)",
        "status/gloves not json",
        'status/gloves ["player1"]',
        'status/gloves {"payload": "ready"}',
        'status/gloves {"payload": {"player1": "ready"}}',
        'status/gloves {"payload": {"player1": 1, "player2": "ready"}}',
        'status/gloves {"payload": {"player1": ["ready"], "player2": "ready"}}',
        'game/gesture/1 {"payload": {"gesture": 3}}',
        'game/gesture/1 {"payload": {"gesture": ["mage"]}}',
        'game/gesture/1 {"payload": null}',
        'game/gesture/1 [1, 2]',
        pytest.param("game/gesture/1 " + "[" * 100000 + "]" * 100000, id="nested-too-deep"),
        "$SYS/broker/clients/connected many",
    ],
)
def test_a_malformed_message_is_counted_and_changes_nothing(message):
    state = board_state()

    state.board.apply(message, 100.0)

    assert state.board.bad_messages == 1
    assert state.board.gloves is None
    assert not state.board.gestures[1]
    assert state.board.clients is None


def test_escape_codes_in_a_message_never_reach_the_screen():
    state = board_state('game/gesture/1 {"payload": {"gesture": "\\u001b[2Jboom"}}')

    raw = "\n".join(drawing.render(state, T0, 100.0, width=120, height=80))

    assert "\033[2J" not in raw
    assert "?[2Jboom" in raw


def test_the_link_says_when_the_subscriber_has_printed_nothing_yet():
    state = board_state()

    assert "no messages yet" in row(screen(state, T0, 100.0), "Board link")

    state.board.apply("$SYS/broker/clients/connected 3", 90.0)
    assert "last msg 10s ago" in row(screen(state, T0, 100.0), "Board link")
    assert "clients 3" in row(screen(state, T0, 100.0), "Board link")


def test_mosquitto_subs_own_words_are_shown_as_a_note():
    state = board_state("Error: Connection refused")

    assert "Error: Connection refused" in screen(state, T0, 100.0)
    assert state.board.last_message_at is None


def test_a_new_subscription_forgets_what_the_broker_will_send_again():
    state = board_state("status/gloves " + envelope_json({"player1": "ready", "player2": "ready"}),
                        "$SYS/broker/clients/connected 4")

    state.board.subscribed()

    assert state.board.gloves is None and state.board.clients is None


def test_every_line_fits_the_width():
    state = board_state("Error: " + "x" * 300)
    state.relay = relay_from(line(0, "ble_client", "node 1: session failed: " + "y" * 300))

    for text in drawing.render(state, T0 + 1, 100.0, width=60, height=80):
        assert len(ANSI.sub("", text)) <= 60


# --------------------------------------------------------------------------
# Against a real broker
# --------------------------------------------------------------------------


@pytest.fixture
def broker():
    if not Path(MOSQUITTO).exists():
        pytest.skip("needs Mosquitto")
    broker = Broker()
    broker.start()
    yield broker
    broker.stop()


def publish(port, topic, payload, retain=False):
    pub = str(Path(MOSQUITTO).with_name("mosquitto_pub" + Path(MOSQUITTO).suffix))
    args = [pub, "-h", "127.0.0.1", "-p", str(port), "-t", topic, "-m", payload, "-q", "1"]
    subprocess.run(args + (["-r"] if retain else []), check=True, timeout=DEADLINE_S)


@pytest.mark.asyncio
async def test_the_subscriber_command_reads_a_real_broker(broker):
    publish(broker.port, "status/gloves",
            envelope_json({"player1": "ready", "player2": "down"}), retain=True)
    args = board_link.subscribe_args(port=broker.port)
    args[0] = str(Path(MOSQUITTO).with_name("mosquitto_sub" + Path(MOSQUITTO).suffix))
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE)
    state = board_state()

    async def read():
        async for raw in process.stdout:
            state.board.apply(raw.decode().rstrip("\r\n"), time.monotonic())

    reader = asyncio.create_task(read())
    try:
        await wait_until(lambda: state.board.gloves is not None, timeout=5)
        publish(broker.port, "game/gesture/1", envelope_json({"gesture": "lightning"}))
        publish(broker.port, "viz/echo/2", '{"seq": 0}')
        await wait_until(lambda: state.board.gestures[1] and state.board.echo_at[2], timeout=5)

        assert state.board.gloves == {1: "ready", 2: "down"}
        assert state.board.gestures[1][0][0] == "lightning"
        assert state.board.bad_messages == 0 and state.board.note == ""
    finally:
        process.kill()
        await process.wait()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


# --------------------------------------------------------------------------
# Reconnecting to the board
# --------------------------------------------------------------------------


async def no_agent(_stop):
    return None, ()


@pytest.mark.asyncio
async def test_a_rejected_login_is_never_retried():
    calls = []

    async def session(*_args):
        calls.append(1)
        raise asyncssh.PermissionDenied("Permission denied")

    board = board_link.Board()
    watching = board_link.watch_board(None, "pw", board, asyncio.Event(), session, no_agent)
    await asyncio.wait_for(watching, DEADLINE_S)

    assert calls == [1]
    assert board.link.startswith("login rejected")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error, message",
    [
        (asyncssh.PermissionDenied, "Permission denied for user student-id on host jump"),
        (asyncssh.ConnectionLost, "Permission denied for user student-id on host jump"),
        (asyncssh.ConnectionLost, "lost the link to student-id@jump"),  # any other wording
        (asyncssh.ConnectionLost, "lost the link to boarduser@board"),
    ],
)
async def test_ssh_usernames_never_reach_the_screen_or_the_log(error, message, monkeypatch, caplog):
    monkeypatch.setattr(board_link, "BACKOFF_MIN_S", 0.01)
    config = board_link.tunnel.TunnelConfig("student-id", "jump", "boarduser", "board", 8888)
    stop = asyncio.Event()

    async def session(*_args):
        stop.set()
        raise error(message)

    board = board_link.Board()
    watching = board_link.watch_board(config, "pw", board, stop, session, no_agent)
    await asyncio.wait_for(watching, DEADLINE_S)

    assert "<username>" in board.link
    for user in ("student-id", "boarduser"):
        assert user not in board.link and user not in caplog.text


class FakeAgent:
    def __init__(self):
        self.closed = False

    async def get_keys(self):
        return ["key"]

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_the_agent_is_retried_while_its_pipe_is_busy(monkeypatch):
    agent = FakeAgent()
    attempts = []

    async def connect_agent():
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError(22, "Invalid argument")  # what a busy pipe gives on Windows
        return agent

    monkeypatch.setattr(board_link.asyncssh, "connect_agent", connect_agent)
    held, keys = await asyncio.wait_for(board_link.open_agent(asyncio.Event()), DEADLINE_S)

    assert len(attempts) == 3
    assert held is agent and keys == ["key"]


@pytest.mark.asyncio
async def test_no_agent_running_falls_back_at_once(monkeypatch):
    async def connect_agent():
        raise FileNotFoundError(2, "no pipe")

    monkeypatch.setattr(board_link.asyncssh, "connect_agent", connect_agent)

    assert await board_link.open_agent(asyncio.Event()) == (None, ())


@pytest.mark.asyncio
async def test_reconnects_reuse_the_one_agent_and_close_it_at_the_end(monkeypatch):
    monkeypatch.setattr(board_link, "BACKOFF_MIN_S", 0.01)
    agent = FakeAgent()
    opened, seen = [], []
    stop = asyncio.Event()

    async def open_once(_stop):
        opened.append(1)
        return agent, ["key"]

    async def session(_config, _password, _board, _stop, keys):
        seen.append(keys)
        if len(seen) == 3:
            stop.set()
            return
        raise OSError("link dropped")

    watching = board_link.watch_board(None, "pw", board_link.Board(), stop, session, open_once)
    await asyncio.wait_for(watching, DEADLINE_S)

    assert opened == [1]
    assert seen == [["key"]] * 3
    assert agent.closed


@pytest.mark.asyncio
async def test_held_keys_go_to_both_hops_and_the_agent_pipe_is_not_reopened(monkeypatch):
    calls = []

    class Connection:
        def __init__(self, kwargs):
            calls.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    async def subscribe(*_args):
        pass

    monkeypatch.setattr(board_link.asyncssh, "connect", lambda _host, **kwargs: Connection(kwargs))
    monkeypatch.setattr(board_link, "subscribe_until_stopped", subscribe)
    config = board_link.tunnel.TunnelConfig("u", "jump", "b", "board", 8888)

    await board_link.board_session(config, "pw", board_link.Board(), asyncio.Event(), ["key"])
    await board_link.board_session(config, "pw", board_link.Board(), asyncio.Event(), ())

    assert [c.get("client_keys") for c in calls] == [["key"], ["key"], None, None]
    no_agent_pipe = board_link.NO_AGENT_PIPE
    assert [c.get("agent_path", "default") for c in calls] == [no_agent_pipe] * 2 + ["default"] * 2


@pytest.mark.asyncio
async def test_a_failed_link_is_retried_until_stop(monkeypatch):
    monkeypatch.setattr(board_link, "BACKOFF_MIN_S", 0.01)
    stop = asyncio.Event()
    calls = []

    async def session(_config, _password, board, _stop, _keys):
        calls.append(board.link)
        if len(calls) == 3:
            stop.set()
            return
        raise OSError("network unreachable")

    board = board_link.Board()
    watching = board_link.watch_board(None, "pw", board, stop, session, no_agent)
    await asyncio.wait_for(watching, DEADLINE_S)

    assert calls == ["connecting"] * 3


@pytest.mark.asyncio
async def test_stop_ends_a_long_backoff_at_once(monkeypatch):
    monkeypatch.setattr(board_link, "BACKOFF_MIN_S", 60.0)
    stop = asyncio.Event()

    async def session(*_args):
        raise asyncio.TimeoutError()

    board = board_link.Board()
    task = asyncio.create_task(board_link.watch_board(None, "pw", board, stop, session, no_agent))
    await wait_until(lambda: "retrying" in board.link)
    stop.set()
    await asyncio.wait_for(task, DEADLINE_S)

    assert board.link == "down: TimeoutError, retrying in 60s"


@pytest.mark.asyncio
async def test_ctrl_c_quits_within_the_shutdown_bound_even_if_ssh_hangs(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "SHUTDOWN_S", 0.2)
    hung = asyncio.Event()

    async def hanging_watch(*_args):
        hung.set()
        await asyncio.sleep(3600)  # a connect that ignores stop

    monkeypatch.setattr(board_link, "watch_board", hanging_watch)
    original = signal.getsignal(signal.SIGINT)
    task = asyncio.create_task(
        app.run(app.Dashboard(), drawing.Screen(Stream(tty=False)), object(), "pw",
                      logs_dir=tmp_path)
    )
    await asyncio.wait_for(hung.wait(), DEADLINE_S)
    signal.raise_signal(signal.SIGINT)
    await asyncio.wait_for(task, DEADLINE_S)

    assert signal.getsignal(signal.SIGINT) is original


class FakeProcess:
    def __init__(self, lines):
        self.stdout = self._lines(lines)
        self.closed = False

    async def _lines(self, lines):
        for text in lines:
            yield text

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, *runs):
        self.runs = list(runs)
        self.processes = []

    async def create_process(self, command, term_type=None):
        assert command == board_link.SUBSCRIBE_COMMAND and term_type
        process = FakeProcess(self.runs.pop(0) if self.runs else [])
        self.processes.append(process)
        if not self.runs:
            await asyncio.sleep(3600)  # the last one never ends by itself
        return process


@pytest.mark.asyncio
async def test_a_subscriber_that_exits_is_started_again_on_the_same_connection(monkeypatch):
    monkeypatch.setattr(board_link, "SUBSCRIBER_RETRY_S", 0.01)
    conn = FakeConnection(["Error: Connection refused\r\n"], ["$SYS/broker/clients/connected 3"])
    board = board_link.Board()
    stop = asyncio.Event()
    task = asyncio.create_task(board_link.subscribe_until_stopped(conn, board, stop))
    try:
        await wait_until(lambda: len(conn.processes) == 2)
        assert conn.processes[0].closed
        assert board.note == "Error: Connection refused"
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# --------------------------------------------------------------------------
# The screen
# --------------------------------------------------------------------------


class Stream:
    def __init__(self, tty):
        self.tty = tty
        self.writes = []

    def isatty(self):
        return self.tty

    def write(self, text):
        self.writes.append(text)

    def flush(self):
        pass


def test_output_that_is_not_a_console_gets_plain_frames():
    stream = Stream(tty=False)
    out = drawing.Screen(stream)

    out.draw(drawing.render(board_state(), T0, 100.0))
    out.close()

    assert stream.writes and "\033" not in "".join(stream.writes)


def test_a_console_is_redrawn_in_place_and_only_when_the_frame_changes(monkeypatch):
    monkeypatch.setattr(drawing, "enable_vt_output", lambda: True)
    stream = Stream(tty=True)
    out = drawing.Screen(stream)
    frame = drawing.render(board_state(), T0, 100.0)

    out.draw(frame)
    out.draw(frame)
    out.close()

    draws = [text for text in stream.writes if text.startswith("\033[H")]
    assert len(draws) == 1
    assert stream.writes[0] == "\033[?1049h\033[?25l"
    assert stream.writes[-1] == "\033[?25h\033[?1049l"


# --------------------------------------------------------------------------
# Stopping the whole pipeline
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command_line, script",
    [
        (r'"C:\c\venv\python.exe" pipeline_board_broker.py --tunnel', "pipeline_board_broker.py"),
        (r"C:\c\.venv\Scripts\python.exe C:\c\scripts\run_board.py --sync", "run_board.py"),
        (r'"C:\Program Files\py\python.exe" "C:\my comms\scripts\run_board.py"', "run_board.py"),
        (r"python.exe -m pytest tests\test_run_board.py", "-m"),
        (r"python.exe .\scripts\dashboard --stop-pipeline", "dashboard"),
        ("python.exe", ""),
    ],
)
def test_the_script_is_the_first_argument_after_the_interpreter(command_line, script):
    assert stop_all.script_name(command_line) == script


RUN_ID = "639292238689094498"
RELAY = r'"C:\c\.venv\Scripts\python.exe" pipeline_board_broker.py --tunnel'
BOARD = r"python.exe C:\c\scripts\run_board.py --sync"


def pane(pid, run_id):
    """Return a Win32_Process row for a run_all pane's PowerShell, opened by run `run_id`."""
    return {"ProcessId": pid, "ParentProcessId": 1,
            "CommandLine": f"powershell -NoExit -Command run_pipeline.ps1 ; # run {run_id}"}


def test_one_pid_per_script_and_nothing_else():
    rows = [
        pane(100, RUN_ID),
        {"ProcessId": 10, "ParentProcessId": 100, "CommandLine": RELAY},  # the venv's launcher
        {"ProcessId": 11, "ParentProcessId": 10, "CommandLine": RELAY},  # the python it starts
        {"ProcessId": 20, "ParentProcessId": 100, "CommandLine": BOARD},
        {"ProcessId": 30, "ParentProcessId": 100,
         "CommandLine": r"python.exe -m pytest tests\test_run_board.py"},
        {"ProcessId": 31, "ParentProcessId": 100,
         "CommandLine": r"python.exe C:\c\laptop\tests\test_run_board.py"},
        {"ProcessId": 40, "ParentProcessId": 100, "CommandLine": r"python.exe scripts\dashboard"},
        {"ProcessId": 50, "ParentProcessId": 100, "CommandLine": None},
    ]

    picked = stop_all.pick_pipeline(rows, RUN_ID)

    assert picked == {"pipeline_board_broker.py": 10, "run_board.py": 20}


def test_another_runs_relay_and_board_are_never_picked_even_when_listed_first():
    rows = [
        pane(200, "639000000000000000"),  # another run_all window
        {"ProcessId": 21, "ParentProcessId": 200, "CommandLine": RELAY},
        {"ProcessId": 22, "ParentProcessId": 200, "CommandLine": BOARD},
        {"ProcessId": 31, "ParentProcessId": 999, "CommandLine": RELAY},  # parent already gone
        {"ProcessId": 32, "ParentProcessId": None, "CommandLine": BOARD},  # started by hand
        pane(100, RUN_ID),
        {"ProcessId": 11, "ParentProcessId": 100, "CommandLine": RELAY},
        {"ProcessId": 12, "ParentProcessId": 100, "CommandLine": BOARD},
    ]

    picked = stop_all.pick_pipeline(rows, RUN_ID)

    assert picked == {"pipeline_board_broker.py": 11, "run_board.py": 12}
    assert stop_all.pick_pipeline(rows[:5], RUN_ID) == {}


def test_a_parent_chain_that_loops_ends():
    rows = [{"ProcessId": 11, "ParentProcessId": 12, "CommandLine": RELAY},
            {"ProcessId": 12, "ParentProcessId": 11, "CommandLine": "python.exe x"}]

    assert stop_all.pick_pipeline(rows, RUN_ID) == {}


def test_stop_presses_ctrl_c_once_per_window_then_waits_for_each():
    sent, waited, said = [], [], []
    running = {"pipeline_board_broker.py": 10, "run_board.py": 20}

    stop_all.stop_pipeline(
        RUN_ID,
        find=lambda run_id: running if run_id == RUN_ID else {},
        send=lambda pid: sent.append(pid) or True,
        wait=lambda pid, seconds: waited.append(pid) or pid == 10,
        say=said.append,
    )

    assert sent == [10, 20] and waited == [10, 20]
    assert said == [
        "Ctrl-C sent to the relay",
        "Ctrl-C sent to the board client",
        "the relay stopped",
        f"the board client still running after {stop_all.PIPELINE_STOP_S}s",
    ]


def test_stop_says_so_when_the_process_list_cannot_be_read():
    said = []

    def find(_run_id):
        raise ValueError("Expecting value: line 1 column 1")  # powershell printed no JSON

    stop_all.stop_pipeline(RUN_ID, find=find, send=None, wait=None, say=said.append)

    assert said == ["could not look for the relay and board client "
                    "(Expecting value: line 1 column 1), so stop them by hand"]


def test_a_ctrl_c_that_cannot_be_sent_is_reported_not_raised(monkeypatch):
    def timed_out(*_args, **_kwargs):
        raise stop_all.subprocess.TimeoutExpired("python", 15)

    monkeypatch.setattr(stop_all.subprocess, "run", timed_out)

    assert stop_all.send_ctrl_c(1234) is False


def test_stop_says_so_when_nothing_is_running():
    said = []

    stop_all.stop_pipeline(RUN_ID, find=lambda _run_id: {}, send=None, wait=None, say=said.append)

    assert said == ["No relay or board client from this run is running."]


def test_run_all_starts_the_dashboard_with_stop_pipeline_and_stacks_the_logs_on_the_right():
    launcher = (SCRIPTS / "run_all.ps1").read_text()

    assert "scripts\\dashboard --stop-pipeline $launch" in launcher
    # every pane carries the launch id, so the dashboard can tell this run's processes apart
    assert re.search(r"--title Relay[^\n]*run_pipeline\.ps1 \\; # run \$launch", launcher)
    assert re.search(r"--title Board[^\n]*\$waitForRelay", launcher) and "$launch 30" in launcher
    # the relay pane is index 1, and the board pane must split it, not the dashboard
    assert re.search(r'"focus-pane -t 1",\s*"split-pane -H --title Board', launcher)


# --------------------------------------------------------------------------
# run_all.ps1 waiting for each login before the next pane starts
# --------------------------------------------------------------------------

POWERSHELL = shutil.which("powershell")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
TUNNEL_OPEN = "2026-11-02 12:00:01,000Z [tunnel] tunnel open: board localhost:8888 -> laptop"


def ticks(unix_seconds):
    """Return .NET DateTime ticks for a Unix time, as run_all.ps1 passes the launch time."""
    return int(unix_seconds * 10_000_000) + 621_355_968_000_000_000


WAITERS = []


@pytest.fixture
def tree(tmp_path):
    """Copy run_all.ps1 next to an empty logs folder, then end any waiter left running."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPTS / "run_all.ps1", tmp_path / "scripts")
    (tmp_path / "logs" / "2026-11-02").mkdir(parents=True)
    yield tmp_path
    for waiter in WAITERS:
        if waiter.poll() is None:
            waiter.kill()
            waiter.wait()
    WAITERS.clear()


def wait_for(tree, name, text, launch, seconds):
    command = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               str(tree / "scripts" / "run_all.ps1"), "--wait-for", name, text,
               str(ticks(launch)), str(seconds)]
    WAITERS.append(subprocess.Popen(command, stdout=subprocess.PIPE, text=True))
    return WAITERS[-1]


@needs_powershell
def test_the_wait_ends_soon_after_a_new_log_has_the_line(tree):
    waiter = wait_for(tree, "pipeline_board_broker", "tunnel open", time.time(), 20)
    time.sleep(2)  # let it start and poll an empty folder
    assert waiter.poll() is None

    log = tree / "logs" / "2026-11-02" / "pipeline_board_broker_120000Z.log"
    attempt = "2026-11-02 12:00:00,000Z [tunnel] attempt 1: opening tunnel"
    log.write_text(f"{attempt}\n{TUNNEL_OPEN}\n")
    written = time.monotonic()
    out = waiter.communicate(timeout=10)[0]

    assert time.monotonic() - written < 2
    assert "starting anyway" not in out


@needs_powershell
def test_the_wait_ignores_a_log_from_before_the_launch(tree):
    old = tree / "logs" / "2026-11-02" / "pipeline_board_broker_110000Z.log"
    old.write_text(TUNNEL_OPEN + "\n")
    started = time.monotonic()

    out = wait_for(tree, "pipeline_board_broker", "tunnel open", time.time() + 5, 2).communicate(
        timeout=20)[0]

    assert time.monotonic() - started >= 2
    assert "No 'tunnel open' after 2 s, so starting anyway" in out


@needs_powershell
def test_the_wait_ignores_logs_with_other_names(tree):
    day = tree / "logs" / "2026-11-02"
    started = time.time()
    waiter = wait_for(tree, "run_board", "connected to the board", started, 3)
    for other in ("run_board_a_120000Z.log", "dashboard_120000Z.log", "run_board.log"):
        (day / other).write_text("2026-11-02 12:00:00,000Z [run_board] connected to the board\n")

    assert "starting anyway" in waiter.communicate(timeout=20)[0]


WAIT_WRITER = """
import logging, sys, time
handler = logging.FileHandler(sys.argv[1], encoding="utf-8")
log = logging.getLogger("tunnel")
log.addHandler(handler)
log.setLevel(logging.INFO)
errors = 0
for i in range(300):
    try:
        log.info("2026-11-02 12:00:00,000Z [tunnel] attempt %d: opening tunnel", i)
        if i == 150:
            log.info(sys.argv[2])
    except Exception:
        errors += 1
    time.sleep(0.005)
print(errors)
"""


@needs_powershell
def test_the_wait_reads_a_log_the_relay_still_has_open_without_disturbing_it(tree):
    log = tree / "logs" / "2026-11-02" / "pipeline_board_broker_120000Z.log"
    waiter = wait_for(tree, "pipeline_board_broker", "tunnel open", time.time(), 20)
    writer = subprocess.Popen([sys.executable, "-c", WAIT_WRITER, str(log), TUNNEL_OPEN],
                              stdout=subprocess.PIPE, text=True)

    out = waiter.communicate(timeout=30)[0]
    errors = writer.communicate(timeout=30)[0].strip()

    assert "starting anyway" not in out
    assert errors == "0"
    assert len(log.read_text().splitlines()) == 301


def test_each_pane_waits_for_a_line_the_real_code_logs():
    launcher = (SCRIPTS / "run_all.ps1").read_text()
    tunnel_source = (Path(__file__).resolve().parents[1] / "tunnel.py").read_text()
    run_board_source = (SCRIPTS / "run_board.py").read_text()

    relay_wait = re.search(r"--wait-for pipeline_board_broker '([^']+)'", launcher)[1]
    board_wait = re.search(r"--wait-for run_board '([^']+)'", launcher)[1]

    assert f'"{relay_wait}:' in tunnel_source  # tunnel.run_tunnel logs "tunnel open: ..."
    assert f'log.info("{board_wait}")' in run_board_source
    # the board pane waits for the relay, and the dashboard pane for the board client
    assert re.search(r"Title Board[^\n]*\$waitForRelay", launcher, re.IGNORECASE)
    assert re.search(r"Title Dashboard[^\n]*\$waitForBoard", launcher, re.IGNORECASE)
