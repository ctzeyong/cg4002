# Measuring latency

How the latency figures in the final report (Section 8.1) were measured, and how to repeat them.
Every figure comes from log lines that the programs already write during a normal session. The only
extra step is two captures of the Ultra96's broker. The scripts in `Comms/scripts/tools` only read
the logs, so they can be run at any time afterwards.

## What is timed

| Figure | From | To | Clock | Script |
|---|---|---|---|---|
| Laptop to Ultra96 | The laptop receives a glove's end-of-gesture packet | The board client receives it | Laptop and board, joined with the heartbeat offset | `session_stats.py` |
| Classification | The board client receives the packet | The AI event is made | Board | `session_stats.py` |
| Phone round trip | The capture receives a `game/gesture` | The capture receives that phone's copy on `viz/echo/N` | Board | `latency_report.py --echo` |
| BLE round trip | The laptop logs a vibration command | The laptop logs the glove's echo of it | Laptop | `session_stats.py` |
| Gesture to game state | The laptop receives the end-of-gesture packet | The first `game/state` that holds the new unit | Board, plus the heartbeat offset for the first step | `gesture_to_state.py` |

`session_stats.py` also prints the classifier call on its own, as `t_infer_ms`. That call covers
decoding the sample, stacking the 150-sample window, converting it to fixed point, the DMA transfer,
the FPGA's forward pass and the softmax.

Three parts of the path are not timed. The glove has no clock shared with the laptop, so the glove
to laptop hop is estimated as half the BLE round trip. The time the glove's detector takes to decide
that a move has ended, and the time a phone takes to draw a frame, are not measured.

## Clocks

- **Laptop and board.** The board client measures its clock offset from the laptop with each
  heartbeat round trip, and uses the sample with the shortest round trip in the last 30 s. It logs
  the offset in its regular `uplink rx` line, and each AI event is given the latest value.
  `session_stats.py` prints the worst bound on that offset. It was 3.5 ms on 12 November.
- **Phones.** The team's app writes no log the scripts can read. So each phone sends every gesture
  straight back on `viz/echo/N`, and the capture on the Ultra96 times both messages on one clock.
  `latency_report.py --echo` also estimates the one-way legs, but their bound was about 40 ms on
  12 November, as large as the legs themselves. Quote the round trip.
- **Line stamps on Windows.** The BLE round trip uses the laptop's log-line stamps, and on some
  machines that clock only moves every 15.6 ms. `session_stats.py` prints how many different
  millisecond values the stamps took. A number close to 1000 means the clock is fine.

Percentiles are nearest-rank in every script.

## Recording a session

1. Start the system as in the [team README](../README.md#2-running-the-game).
2. In a shell on the Ultra96, start the two captures in the board's log folder, so that
   `--fetch-logs` copies them back with the other logs:

   ```bash
   d=~/comms/logs/$(date -u +%Y-%m-%d); mkdir -p "$d"; ts=$(date -u +%Y%m%dT%H%M%SZ)
   setsid nohup mosquitto_sub -h 127.0.0.1 -i cap-eng-game -q 1 -F '%U %t %p' -t 'game/match' -t 'game/event' -t 'game/request/+' -t 'game/gesture/+' -t 'status/gloves' -t 'viz/echo/+' > "$d/engine_game_$ts.log" 2>&1 < /dev/null &
   setsid nohup mosquitto_sub -h 127.0.0.1 -i cap-eng-stream -q 0 -F '%U %t %p' -t 'game/state' -t 'viz/lane/+' > "$d/engine_stream_$ts.log" 2>&1 < /dev/null &
   pgrep -af cap-eng
   ```

   You should see two `mosquitto_sub` lines. `setsid nohup` keeps them running if the shell drops.
3. Play as usual. Gestures and vibrations are recorded without anything else to do.
4. Stop the captures with `pkill -f cap-eng`, then stop the system.
5. From `Comms`, copy the board's logs and captures to `logs\board\<date>\`. Dates are in UTC.

   ```powershell
   .\scripts\run_board.ps1 --fetch-logs
   ```

   The laptop's own log is already in `logs\<date>\`.

## Running the scripts

Run these from `Comms` with its virtual environment. Each one prints n, median, p95 and max for each
step, and its docstring describes the join in detail.

```powershell
.venv\Scripts\python scripts\tools\session_stats.py --board logs\board\<date> --relay <folder> --since <HHMMSS>
.venv\Scripts\python scripts\tools\latency_report.py --config B --echo <engine_game capture> --board <board client log>
.venv\Scripts\python scripts\tools\gesture_to_state.py --board <board client log> --capture <engine_game capture>
```

- `session_stats.py` reads every laptop log in the `--relay` folder for the BLE round trip and the
  packet loss. Copy the session's `pipeline_board_broker_*.log` into a folder of its own and pass
  that, or other runs from the same day are pooled in. `--since` only filters the board logs.
- The board client's log is `tcp_client_mqtt_<time>Z.log`. Use the one from the same session as
  the capture.
- `gesture_to_state.py` finds the `engine_stream` capture by the `engine_game` capture's name, so
  keep the pair together.

## Comparing the broker placements

The report's broker placement comparison ran before the gloves had IMUs. The
Ultra96 made a dummy event after a random number of sensor packets, and each event was timed from
the laptop to an Android phone. `scripts/tools/phone_clock.py` measures the phone's clock offset over
USB before and after a run, and `latency_report.py --phone` joins the phone's log to the board's.
Both scripts' docstrings give the commands.
