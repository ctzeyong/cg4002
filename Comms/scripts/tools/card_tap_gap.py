"""The card tap board gap with the engine on the board, from the board's two captures alone.

    python scripts/tools/card_tap_gap.py --board logs/board/2026-11-10

Read-only. For each spawn tap it joins three arrivals on the board's mosquitto_sub, so one clock:

1. the phone's game/request/N, by (player, req_id, match_id);
2. the engine's game/event with the same three, ok or refused;
3. for an ok tap, the first game/state at or after the request whose units hold its unit_id.

2 -> 3 is mostly the wait for the next 10 Hz game/state, so 1 -> 3 can be up to 100 ms longer
than the engine's own work. 1 -> 3 is the number to set against the earlier 105 ms,
which ran from the same request to the first game/state with the unit, with the host phone as engine.

Captures come in pairs from one run: engine_game_<stamp>.log holds the requests
and events, engine_stream_<stamp>.log the game/state stream. Each pair is read on its own.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from latency_report import summarise  # noqa: E402


def captured(path):
    """(arrival time, topic, decoded message) per readable line of a `-F '%U %t %p'` capture."""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split(" ", 2)
            if len(parts) != 3:
                continue
            try:
                yield float(parts[0]), parts[1], json.loads(parts[2])
            except (ValueError, json.JSONDecodeError):
                continue


def utc(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def taps(game_log, stream_log):
    """One row per tap's game/event: (event time, player, req_id, result, ms to event, ms to state)."""
    requests = {}
    events = []
    for t, topic, msg in captured(game_log):
        payload = msg.get("payload") if isinstance(msg, dict) else None
        if not isinstance(payload, dict):
            continue
        if topic.startswith("game/request/") and payload.get("action") == "spawn":
            key = (topic.rsplit("/", 1)[-1], payload.get("req_id"), payload.get("match_id"))
            requests.setdefault(key, t)  # QoS 1 can deliver a request twice: keep the first
        elif topic == "game/event" and payload.get("input") == "tap":
            events.append((t, payload))

    # Unit ids restart with each match, so a state counts only for its own match.
    states = []
    for t, topic, msg in captured(stream_log):
        payload = msg.get("payload") if isinstance(msg, dict) else None
        if topic == "game/state" and isinstance(payload, dict):
            units = payload.get("units") or []
            ids = {u.get("id") for u in units if isinstance(u, dict)}
            states.append((t, payload.get("match_id"), ids))

    rows = []
    for t_event, p in events:
        key = (str(p.get("player")), p.get("req_id"), p.get("match_id"))
        t_request = requests.get(key)
        result = "ok" if p.get("ok") else p.get("reason")
        if t_request is None:
            rows.append((t_event, key[0], key[1], result, None, None))
            continue
        to_state = None
        if p.get("ok"):
            for t_state, match_id, ids in states:
                if t_state >= t_request and match_id == p.get("match_id") and p.get("unit_id") in ids:
                    to_state = (t_state - t_request) * 1000
                    break
        rows.append((t_event, key[0], key[1], result, (t_event - t_request) * 1000, to_state))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--board", type=Path, required=True, help="folder of engine_game_*/engine_stream_* captures")
    args = parser.parse_args(argv)

    pairs = []
    for game_log in sorted(args.board.glob("engine_game_*.log")):
        stream_log = game_log.with_name(game_log.name.replace("engine_game_", "engine_stream_"))
        if stream_log.exists():
            pairs.append((game_log, stream_log))
        else:
            print(f"{game_log.name}: no matching engine_stream capture, skipped")
    if not pairs:
        raise SystemExit("no engine_game/engine_stream capture pairs found")

    to_event, to_state = [], []
    for game_log, stream_log in pairs:
        print(f"== {game_log.name} + {stream_log.name}")
        for t_event, player, req_id, result, ms_event, ms_state in taps(game_log, stream_log):
            line = f"  {utc(t_event)}  player {player} req {req_id}  {result}"
            if ms_event is None:
                print(line + "  (no request captured)")
                continue
            line += f"  request -> event {ms_event:6.1f} ms"
            to_event.append(ms_event)
            if ms_state is not None:
                line += f"  request -> state {ms_state:6.1f} ms"
                to_state.append(ms_state)
            elif result == "ok":
                line += "  (unit never seen in game/state)"
            print(line)

    print(f"\npooled over {len(pairs)} capture pair(s)")
    print(f"  {'hop':44} {'n':>4} {'median':>9} {'p95':>9} {'max':>9}  (ms)")
    for name, values in (
        ("1 -> 2  request -> game/event (every tap)", to_event),
        ("1 -> 3  request -> first game/state (ok taps)", to_state),
    ):
        s = summarise(values)
        if s is None:
            print(f"  {name:44} {0:>4}")
        else:
            n, median, p95, worst = s
            print(f"  {name:44} {n:>4} {median:>9.1f} {p95:>9.1f} {worst:>9.1f}")


if __name__ == "__main__":
    main()
