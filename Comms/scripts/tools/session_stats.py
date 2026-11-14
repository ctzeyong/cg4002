"""The numbers for one day's runs under B, from the logs alone: latency, BLE round trip, packet loss.

    python scripts/tools/session_stats.py --board logs/board/2026-11-05 --relay logs/2026-11-05 --since 030600

Read-only. Prints three sections:

1. Laptop receipt -> AI event, per board log and pooled. Each board log goes through
   latency_report.report() and summarise(), the same join and nearest-rank p95 as
   latency_report.py --config B --board, so the per-run rows match that tool's printout.
2. The BLE round trip laptop -> glove -> laptop: the relay's "CMD seq=N" line (logged just
   before the write) to its "VIBRATE seq=N echoed" line, per node. Both are log-line stamps
   (time.time()), so the section prints how many distinct millisecond values the stamps took:
   on Windows that clock can step every 15.6 ms.
3. Packet loss per glove session, from the "supervisor stopped" totals, and the seconds with 5 or
   more packets missed, from the per-second rate lines' cumulative missed=.

--since keeps board logs whose name stamp (HHMMSS) is at or after it, so runs made from another
laptop can be left out. Method and the numbers: notes/latency.md 3.14.
"""

import argparse
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from latency_report import report, summarise  # noqa: E402

HOPS = (
    ("laptop_rx_to_board_rx", "1 -> 3  laptop rx -> board rx"),
    ("board_rx_to_event", "3 -> 4  board rx -> AI event"),
    ("inference", "  in 3 -> 4: inference (t_infer_ms)"),
    ("laptop_rx_to_event", "1 -> 4  laptop rx -> AI event"),
    ("event_to_publish_acked", "side   AI event -> publish acked"),
)
SLOW_MS = (50, 200)

STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3})Z ")
NAME_STAMP = re.compile(r"_(\d{6})Z\.log$")
CMD = re.compile(r"\[ble_client\] node (\d+): CMD seq=(\d+) fields")
ECHOED = re.compile(r"\[ble_client\] node (\d+): VIBRATE seq=(\d+) echoed")
RATE = re.compile(r"\[ble_client\] node (\d+): n=\d+ seq=\d+ rate=([\d.]+) Hz .* missed=(\d+)")
STOPPED = re.compile(
    r"\[ble_client\] node (\d+): supervisor stopped after (\d+) packets \(missed=(\d+) .*dropped=(\d+)"
)
BAD_SECOND = 5  # packets missed in one rate line's second


def name_stamp(path):
    m = NAME_STAMP.search(path.name)
    return m.group(1) if m else None


def line_time(line):
    """(epoch seconds, 'HH:MM:SS') from a log line's own stamp, or None."""
    m = STAMP.match(line)
    if not m:
        return None
    t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return t.timestamp() + int(m.group(2)) / 1000, m.group(1)[11:]


def row(label, values):
    stats = summarise(values)
    if stats is None:
        return f"{label:40} {0:5d}"
    n, median, p95, worst = stats
    return f"{label:40} {n:5d} {median:9.3f} {p95:9.3f} {worst:9.3f}"


def latency(board_logs, out):
    print("== 1. laptop receipt -> AI event (latency_report.report, per board log)", file=out)
    pooled = defaultdict(list)
    slow = []
    totals = defaultdict(int)
    worst_bound = 0.0
    for path in board_logs:
        run = report(path.read_text(encoding="utf-8", errors="replace").splitlines(), "B")
        rtts = [e.rtt for e in run.events if e.rtt is not None]
        bound = max(rtts) / 2 * 1000 if rtts else None
        one_to_four = [e.hops["laptop_rx_to_event"] for e in run.events if "laptop_rx_to_event" in e.hops]
        stats = summarise(one_to_four)
        bound_text = f"{bound:.3f}" if bound is not None else "none"
        median_text = f"{stats[1]:.3f}" if stats else "-"
        print(f"  {path.name}: {len(run.events)} events, offset bound {bound_text} ms, 1 -> 4 median {median_text}", file=out)
        if bound is not None and run.events:
            worst_bound = max(worst_bound, bound)
        for name, value in vars(run.counts).items():
            totals[name] += value
        for e in run.events:
            for key, _ in HOPS:
                if key in e.hops:
                    pooled[key].append(e.hops[key])
            hop = e.hops.get("laptop_rx_to_board_rx")
            if hop is not None and hop > SLOW_MS[0]:
                slow.append((path.name, e.player, e.t_board_rx, hop))
    print(f"\n  pooled over {len(board_logs)} board logs; worst offset bound {worst_bound:.3f} ms", file=out)
    print(f"  {'hop':40} {'n':>5} {'median':>9} {'p95':>9} {'max':>9}  (ms)", file=out)
    for key, label in HOPS:
        print("  " + row(label, pooled[key]), file=out)
    missing = {k: v for k, v in totals.items() if v and k in (
        "triggers_without_event", "events_without_trigger", "events_without_offset",
        "published_unmatched", "events_without_published")}
    print(f"  missing links: {missing or 'none'}", file=out)
    n = len(pooled["laptop_rx_to_board_rx"])
    for limit in SLOW_MS:
        count = sum(1 for _, _, _, hop in slow if hop > limit)
        print(f"  1 -> 3 over {limit} ms: {count} of {n}", file=out)
    print(f"  1 -> 3 over {SLOW_MS[1]} ms (board clock, UTC):", file=out)
    for name, player, t_rx, hop in slow:
        if hop > SLOW_MS[1]:
            at = datetime.fromtimestamp(t_rx, timezone.utc).strftime("%H:%M:%S.%f")[:12]
            print(f"    {name} player {player} {at} {hop:.1f} ms", file=out)


def ble_and_loss(relay_logs, out):
    rtt = defaultdict(list)
    unpaired = 0
    sessions = []
    rate_seconds = defaultdict(dict)  # (file, node) -> {second: (missed, rate)}
    print("\n== 2. BLE round trip, CMD logged -> VIBRATE echo logged (relay logs)", file=out)
    for path in relay_logs:
        sent = {}
        ms_values = set()
        prev_missed = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stamped = line_time(line)
            if stamped is None:
                continue
            t, second = stamped
            ms_values.add(round(t * 1000) % 1000)
            if m := CMD.search(line):
                sent[(m.group(1), m.group(2))] = t
            elif m := ECHOED.search(line):
                start = sent.pop((m.group(1), m.group(2)), None)
                if start is not None:
                    rtt[m.group(1)].append((t - start) * 1000)
            elif m := RATE.search(line):
                node, missed = m.group(1), int(m.group(3))
                before = prev_missed.get(node)
                # missed= restarts at 0 after a reconnect, so a drop is a new session
                if before is not None and missed >= before:
                    rate_seconds[(path.name, node)][second] = (missed - before, float(m.group(2)))
                prev_missed[node] = missed
            elif m := STOPPED.search(line):
                sessions.append((path.name, m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))))
        unpaired += len(sent)
        print(f"  {path.name}: {len(ms_values)} distinct millisecond values in the line stamps", file=out)
    print(f"  {'node':40} {'n':>5} {'median':>9} {'p95':>9} {'max':>9}  (ms)", file=out)
    for node in sorted(rtt):
        print("  " + row(f"glove {node}", rtt[node]), file=out)
    print("  " + row("both", [v for values in rtt.values() for v in values]), file=out)
    print(f"  CMDs with no echo logged: {unpaired}", file=out)

    print("\n== 3. Packet loss per glove session (supervisor totals)", file=out)
    with_packets = [s for s in sessions if s[2] > 0]
    for name, node, received, missed, dropped in with_packets:
        share = 100 * missed / (received + missed)
        print(f"  {name} glove {node}: received {received}, missed {missed} ({share:.2f}%), dropped {dropped}", file=out)
    lossless = sum(1 for s in with_packets if s[3] == 0)
    print(f"  sessions with packets: {len(with_packets)}, with none missed: {lossless}, "
          f"dropped by the relay in all: {sum(s[4] for s in with_packets)}", file=out)

    print(f"\n  seconds with {BAD_SECOND}+ packets missed (per-second rate lines)", file=out)
    bad = defaultdict(set)
    for (name, node), seconds in sorted(rate_seconds.items()):
        hits = {s: v for s, v in seconds.items() if v[0] >= BAD_SECOND}
        if not hits:
            continue
        rates = [rate for _, rate in hits.values()]
        print(f"  {name} glove {node}: {len(hits)} s, rate in them median {statistics.median(rates):.1f} Hz, "
              f"min {min(rates):.1f} Hz", file=out)
        for s in hits:
            bad[(name, node)].add(s)
    both = 0
    total = 0
    for (name, node), seconds in bad.items():
        total += len(seconds)
        if node != "1":
            continue
        other = bad.get((name, "2"), set())
        for s in seconds:
            h, m, sec = (int(x) for x in s.split(":"))
            base = h * 3600 + m * 60 + sec
            near = {f"{(base + d) // 3600:02d}:{(base + d) % 3600 // 60:02d}:{(base + d) % 60:02d}" for d in (-1, 0, 1)}
            if near & other:
                both += 1
    print(f"  in all: {total} bad glove-seconds; glove 1 seconds with glove 2 also bad within 1 s: {both}", file=out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--board", type=Path, required=True, help="folder of tcp_client_mqtt_*.log")
    parser.add_argument("--relay", type=Path, required=True, help="folder of pipeline_board_broker_*.log")
    parser.add_argument("--since", default="000000", help="HHMMSS: board logs stamped at or after it")
    args = parser.parse_args(argv)
    board_logs = sorted(
        p for p in args.board.glob("tcp_client_mqtt_*.log") if (name_stamp(p) or "") >= args.since
    )
    relay_logs = sorted(args.relay.glob("pipeline_board_broker_*.log"))
    if not board_logs or not relay_logs:
        raise SystemExit("no board or relay logs found")
    latency(board_logs, sys.stdout)
    ble_and_loss(relay_logs, sys.stdout)


if __name__ == "__main__":
    main()
