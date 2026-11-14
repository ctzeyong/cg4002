"""Draw the dashboard's screen, in place on a console or as plain blocks otherwise."""

import re
import shutil
import sys
import time

import envelope
from dashboard import sleep_or_stop
from dashboard.board_link import BOARD_QUIET_S, GESTURES_SHOWN
from dashboard.relay_log import STALE_S

REDRAW_S = 0.25
PLAIN_REDRAW_S = 2.0  # when stdout is not a console, print a whole frame this often
POWER_QUIET_S = 15.0  # the monitor publishes every 5 s, so this means it has stalled

GREEN = "\033[38;5;40m"
YELLOW = "\033[38;5;184m"
RED = "\033[38;5;196m"
DIM = "\033[38;5;245m"
BOLD = "\033[1m"
RESET = "\033[0m"


def age(seconds):
    """Return a short age like 4s, 12m or 3h."""
    if seconds < 0:
        seconds = 0
    if seconds < 100:
        return f"{seconds:.0f}s"
    if seconds < 6000:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.0f}h"


def fit(segments, width):
    """Join (text, colour) segments into one line cut to `width` visible characters."""
    out, used = [], 0
    for text, colour in segments:
        if used >= width:
            break
        text = text[: width - used]
        used += len(text)
        out.append(f"{colour}{text}{RESET}" if colour else text)
    return "".join(out)


def _status(text, colour, size=14):
    return (f"{text:<{size}} ", colour)


def _glove_row(relay, node, now):
    glove = relay.gloves.get(node)
    label = (f"  Glove {node}      ", "")
    if glove is None:
        return [label, _status("no lines yet", DIM)]
    if glove.counted_at is not None and now - glove.counted_at <= STALE_S:
        return [
            label,
            _status("streaming", GREEN, 10),
            (f"{glove.rate:5.1f} Hz  miss {glove.missed}  dup {glove.dup}  "
             f"buzz {glove.echoed}/{glove.sent}", ""),
        ]
    state = glove.state or "no data"
    colour = RED if state in ("disconnected", "not found", "stopped") else YELLOW
    if state in ("connected", "subscribed"):
        state = "connected, no data"
    return [label, _status(state, colour, 20)]


def _laptop_rows(relay, now):
    rows = [[("LAPTOP", BOLD)]]
    tunnel_colour = {"up": GREEN, "login rejected": RED, "stopped": RED}.get(relay.tunnel, YELLOW)
    if relay.tunnel:
        rows.append([("  SSH tunnel   ", ""), _status(relay.tunnel, tunnel_colour)])
    else:
        rows.append([("  SSH tunnel   ", ""), ("no tunnel lines", DIM)])
    if relay.tcp_clients > 0:
        tcp = [_status("connected", GREEN)]
        if relay.uplink_at is not None and now - relay.uplink_at <= STALE_S:
            rate, queued = relay.uplink
            tcp.append((f"uplink {rate:5.1f} Hz  queued {queued}", ""))
    elif relay.tcp_seen:
        tcp = [_status("not connected", RED)]
    else:
        tcp = [("no lines yet", DIM)]
    rows.append([("  Board TCP    ", "")] + tcp)
    for node in sorted(set(envelope.PLAYERS) | set(relay.gloves)):
        rows.append(_glove_row(relay, node, now))
    return rows


def _board_rows(board, mono_now):
    rows = [[("BOARD", BOLD)]]
    link = [("  Board link   ", ""), _status(board.link, GREEN if board.link_ok else YELLOW)]
    if board.link_ok and board.last_message_at is None:
        link.append(("no messages yet  ", YELLOW))
    elif board.link_ok:
        quiet = mono_now - board.last_message_at
        link.append((f"last msg {age(quiet)} ago  ", YELLOW if quiet > BOARD_QUIET_S else ""))
    if board.link_ok and board.clients is not None:
        link.append((f"clients {board.clients}", ""))
    rows.append(link)
    if board.note:
        rows.append([("               ", ""), (board.note, YELLOW)])
    states = [("  Glove state  ", "")]
    for player in envelope.PLAYERS:
        if board.link_ok and board.gloves is not None:
            state = board.gloves[player]
            colour = GREEN if state == envelope.READY else RED
        else:
            state, colour = "unknown", DIM
        states.append((f"P{player} ", ""))
        states.append(_status(state, colour, 9))
    if board.bad_messages:
        states.append((f"bad messages {board.bad_messages}", YELLOW))
    rows.append(states)
    rows += _power_rows(board, mono_now)
    return rows


# The board's rail names, shortened for the screen
RAIL_LABELS = (("PSINT_FP", "CPU"), ("INT", "FPGA"), ("PSDDR", "DDR"), ("3V3", "3V3"))


def _watts(value):
    return "-" if value is None else f"{value:.3f} W"


def _power_rows(board, mono_now):
    """The power monitor's latest reading, and the session's idle and playing so far."""
    power = board.power
    if not board.link_ok or power is None:
        return [[("  Power        ", ""), ("not recording (start with --power)", DIM)]]
    state = power["state"]
    live = state in ("idle", "playing")
    quiet = mono_now - board.power_at
    now = [("  Power        ", "")]
    if live:
        now.append((f"{_watts(power['now_W'])} now  ", BOLD))
        now.append(_status(state, GREEN if state == "playing" else "", 8))
        rails = power["now_rails"]
        now.append(("  ".join(f"{label} {rails[name]:.3f}" for name, label in RAIL_LABELS
                              if name in rails) + "  ", DIM))
        now.append((f"{age(quiet)} ago", YELLOW if quiet > POWER_QUIET_S else DIM))
    elif state == "stopped":
        now.append(("monitor stopped; its last session:", YELLOW))
    else:  # the broker's last will: the monitor went without saying goodbye
        now.append(("monitor offline: it stopped without writing totals", YELLOW))
        return [now]
    session = [("  This session ", "")]
    for s in ("idle", "playing"):
        seconds = power[f"{s}_s"] or 0
        session.append((f"{s} {_watts(power[f'{s}_W'])} over {age(seconds):<5}  ",
                        "" if seconds else DIM))
    if power["idle_W"] is not None and power["playing_W"] is not None:
        session.append((f"playing {power['playing_W'] - power['idle_W']:+.3f} W  ", BOLD))
    session.append((f"{power['gestures'] or 0} gestures", DIM))
    return [now, session, _tuning_row(power.get("tuning"), stopped=state == "stopped")]


# (field, how to show its value) for the Tuning row
TUNING_SHOWN = (("cpu_mhz", "CPU {:g} MHz".format),
                ("cores", lambda n: f"{n:g} core" + ("" if n == 1 else "s")),
                ("pl_mhz", "PL {:g} MHz".format))


def _tuning_row(tuning, stopped=False):
    """The board's CPU clock, cores and PL clock; a tuned one also shows what it was.

    Once the monitor stopped they are the session's: the board has had its own put back.
    """
    row = [("  Tuning       ", "")] + ([("session used: ", DIM)] if stopped else [])
    if tuning is None:
        return row + [("unknown (the board has an older power_monitor.py; --sync)", DIM)]
    now, was = tuning["now"], tuning["was"]
    tuned = False
    for name, shown in TUNING_SHOWN:
        if now[name] is None:
            continue
        text = shown(now[name])
        if was[name] is not None and was[name] != now[name]:
            row.append((f"{text} ", BOLD))
            row.append((f"(was {was[name]:g})  ", DIM))
            tuned = True
        else:
            row.append((f"{text}  ", ""))
    if not tuned:
        row.append(("board defaults", DIM))
    return row


def _player_rows(board, mono_now):
    def when(moment):
        return ("none seen", DIM) if moment is None else (f"{age(mono_now - moment)} ago", "")

    columns = 22
    rows = [[(f"{'PLAYERS':<17}", BOLD)]
            + [(f"{f'Player {player}':<{columns}}", BOLD) for player in envelope.PLAYERS]]
    for label, times in (("Last ready", board.ready_at), ("Last echo", board.echo_at)):
        row = [(f"  {label:<15}", "")]
        for player in envelope.PLAYERS:
            text, colour = when(times[player])
            row.append((f"{text:<{columns}}", colour))
        rows.append(row)
    for index in range(GESTURES_SHOWN):
        row = [(f"  {'Gestures' if index == 0 else '':<15}", "")]
        for player in envelope.PLAYERS:
            shown = board.gestures[player]
            if index < len(shown):
                gesture, moment = shown[index]
                text = f"{gesture:<14}{age(mono_now - moment):>5}"
                row.append((f"{text:<{columns}}", "" if index == 0 else DIM))
            else:
                row.append((f"{'-' if index == 0 else '':<{columns}}", DIM))
        rows.append(row)
    return rows


def render(state, now, mono_now, width=100, height=40):
    """Return the screen as lines, each cut to `width`. `now` is wall time, `mono_now` monotonic."""
    relay = state.relay
    rows = []
    if relay.path is None:
        rows.append([("Relay log    ", ""), ("no relay log found yet", YELLOW)])
    else:
        quiet = now - relay.last_line_at if relay.last_line_at is not None else None
        line = [("Relay log    ", ""), (f"{relay.path.name}  ", "")]
        if relay.stopped:
            line.append(("relay stopped", RED))
        elif quiet is not None:
            line.append((f"last line {age(quiet)} ago", YELLOW if quiet > STALE_S else DIM))
        rows.append(line)
    rows.append([])
    rows += _laptop_rows(relay, now)
    rows.append([])
    rows += _board_rows(state.board, mono_now)
    rows.append([])
    rows += _player_rows(state.board, mono_now)
    rows.append([])
    rows.append([("WARNINGS (relay log)  ", BOLD), (f"{relay.warning_count} in this run", DIM)])
    if not relay.warnings:
        rows.append([("  none", DIM)])
    for clock_text, text in relay.warnings:
        rows.append([(f"  {clock_text}  ", DIM), (text, YELLOW)])
    return [fit(row, width) for row in rows[:height]]


class Screen:
    """Draws frames in place on a console, or as plain blocks when output is not a console."""

    def __init__(self, stream):
        self.stream = stream
        self.live = stream.isatty() and enable_vt_output()
        self.last = None
        if self.live:
            # the alternate screen, so quitting gives the terminal back as it was
            stream.write("\033[?1049h\033[?25l")
            stream.flush()

    def draw(self, lines):
        if self.live:
            frame = "\033[H" + "\033[K\n".join(lines) + "\033[K\033[J"
            if frame != self.last:
                self.stream.write(frame)
                self.stream.flush()
                self.last = frame
        else:
            text = re.sub(r"\033\[[0-9;]*m", "", "\n".join(lines))
            self.stream.write(text + "\n\n")
            self.stream.flush()

    def close(self):
        if self.live:
            self.stream.write("\033[?25h\033[?1049l")
            self.stream.flush()


def enable_vt_output():
    """Turn on ANSI escape handling for stdout. Return False if the console refuses."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


async def draw_loop(state, screen, stop):
    """Redraw the screen until `stop` is set."""
    interval = REDRAW_S if screen.live else PLAIN_REDRAW_S
    while not stop.is_set():
        size = shutil.get_terminal_size((100, 40))
        screen.draw(render(state, time.time(), time.monotonic(), size.columns - 1, size.lines))
        await sleep_or_stop(stop, interval)
