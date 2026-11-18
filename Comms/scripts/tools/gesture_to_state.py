"""Time each glove spawn from the laptop to the first game state that holds its unit.

    python scripts/tools/gesture_to_state.py --board <tcp_client_mqtt_*.log>
        --capture <engine_game_*.log>

Read-only. For each glove spawn the engine accepted, it finds three things.

1. The AI event in the board client's log, which also gives the laptop's receipt of the packet.
2. The game/gesture message in the engine_game capture, with the AI event's event_id.
3. The first game/state in the matching engine_stream capture whose units hold the new unit.

Every time is on the board's clock, except the laptop's receipt, which comes from the board log's
heartbeat offset. Spells make no unit, so they are left out.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from card_tap_gap import captured  # noqa: E402
from latency_report import report, summarise  # noqa: E402


def spawns(board_log, game_log, stream_log):
    """Return each hop's times for the accepted glove spawns, and how many could not be joined."""
    with open(board_log, encoding="utf-8", errors="replace") as f:
        run = report(f.read().splitlines(), "B")
    ai_events = {e.event_id: e for e in run.events if e.event_id}

    gestures = {}
    accepted = []
    for t, topic, msg in captured(game_log):
        payload = msg.get("payload") if isinstance(msg, dict) else None
        if not isinstance(payload, dict):
            continue
        if topic.startswith("game/gesture/"):
            # QoS 1 can deliver a message twice, so keep the first arrival
            gestures.setdefault(msg.get("event_id"), t)
        elif topic == "game/event" and payload.get("input") == "glove" and payload.get("ok"):
            if isinstance(payload.get("unit_id"), int) and payload["unit_id"] >= 0:
                accepted.append(payload)

    # unit ids restart with each match, so a state counts only for its own match
    states = []
    for t, topic, msg in captured(stream_log):
        payload = msg.get("payload") if isinstance(msg, dict) else None
        if topic == "game/state" and isinstance(payload, dict):
            ids = {u.get("id") for u in payload.get("units") or [] if isinstance(u, dict)}
            states.append((t, payload.get("match_id"), ids))

    hops = {"laptop": [], "gesture": [], "state": [], "total": []}
    unjoined = 0
    for p in accepted:
        event = ai_events.get(p.get("gesture_event_id"))
        t_gesture = gestures.get(p.get("gesture_event_id"))
        if event is None or t_gesture is None or "laptop_rx_to_event" not in event.hops:
            unjoined += 1
            continue
        t_state = next(
            (t for t, match_id, ids in states
             if t >= t_gesture and match_id == p.get("match_id") and p["unit_id"] in ids),
            None,
        )
        if t_state is None:
            unjoined += 1
            continue
        to_state = (t_state - event.t_event) * 1000
        hops["laptop"].append(event.hops["laptop_rx_to_event"])
        hops["gesture"].append((t_gesture - event.t_event) * 1000)
        hops["state"].append(to_state)
        hops["total"].append(event.hops["laptop_rx_to_event"] + to_state)
    return hops, len(accepted), unjoined


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--board", type=Path, required=True, help="the board client's log")
    parser.add_argument("--capture", type=Path, required=True, help="an engine_game_*.log capture")
    args = parser.parse_args(argv)

    stream_log = args.capture.with_name(args.capture.name.replace("engine_game_", "engine_stream_"))
    if not stream_log.exists():
        raise SystemExit(f"no {stream_log.name} next to {args.capture.name}")

    hops, accepted, unjoined = spawns(args.board, args.capture, stream_log)
    joined = accepted - unjoined
    print(f"accepted glove spawns: {accepted}, joined: {joined}, not joined: {unjoined}")
    print(f"  {'hop':48} {'n':>4} {'median':>9} {'p95':>9} {'max':>9}  (ms)")
    for name, key in (
        ("laptop receives the packet, to the AI event", "laptop"),
        ("AI event, to game/gesture in the capture", "gesture"),
        ("AI event, to the first game/state with the unit", "state"),
        ("laptop receives the packet, to that game/state", "total"),
    ):
        s = summarise(hops[key])
        if s is None:
            print(f"  {name:48} {0:>4}")
        else:
            n, median, p95, worst = s
            print(f"  {name:48} {n:>4} {median:>9.1f} {p95:>9.1f} {worst:>9.1f}")


if __name__ == "__main__":
    main()
