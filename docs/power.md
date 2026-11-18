# Measuring and tuning the Ultra96's power

This supports the AI subsystem's power evaluation. Start the system as in the
[team README](../README.md#2-running-the-game), with the flags below added.

## Measuring the board's power

Add `--power` to the usual command:

```powershell
.\scripts\run_all.ps1 --sync --gesture-flag --vocab new --bitstream gesture_model.xsa --power
```

This also starts `Comms/ultra96/power_monitor.py` on the board. It reads the board's power rails
every 0.25 s and labels each reading:

- **playing**: a match is running on the board's engine (VS PLAYER), or a glove gesture was
  classified in the last 30 s. The second covers VS CPU, which the phone runs by itself, so the
  board's engine never starts a match;
- **idle**: anything else.

It only reads the rails, so it never touches the FPGA or the game. The Dashboard's **Power** row
updates every 5 s with the mean board power over the last 5 s, whether that was idle or playing, and
the four main rails. Below it, **This session** shows the idle and playing averages so far, the
difference, and the number of gestures:

```text
  Power        2.312 W now  playing  CPU 0.712  FPGA 0.219  DDR 0.062  3V3 0.410  3s ago
  This session idle 2.160 W over 2m     playing 2.300 W over 5m     playing +0.140 W  12 gestures
  Tuning       CPU 1200 MHz  4 cores  PL 100 MHz  board defaults
```

Ctrl-C in the Dashboard stops it with everything else. The Board pane then prints the session's totals, for example
`power this session: idle 2.160 W over 141 s, playing 2.300 W over 300 s, playing uses +0.140 W,
12 gesture(s)`, and copies that day's board logs, power files included, to
`Comms/logs/board/<date>/`:

| File | What it holds |
|---|---|
| `power_<time>Z.csv` | Every reading: `utc`, `state` (idle or playing), `match_phase`, `gestures` so far, each rail in W, `TOTAL` |
| `power_summary.csv` | One row per session: `gestures`, `idle_s` and `playing_s`, the mean power of the four main rails and `TOTAL` for each, and `playing_minus_idle_W` |
| `power_monitor_<time>Z.log` | Mean power for idle and playing every 30 s, then the session's totals |

The four main rails are `PSINT_FP` (the CPU cores), `INT` (the FPGA, where the model runs), `PSDDR`
(memory) and `3V3` (peripherals). The rails report in 1/32 W steps a few times a second, so a single
inference, under a millisecond, doesn't show on its own. Playing against idle shows what a running
match costs as a whole. Run the board idle for a while before the first match, so both have enough
readings. Keep VS CPU and VS PLAYER sessions apart when you compare: in VS PLAYER the board also
runs the match, and in VS CPU it only classifies gestures.

## Tuning the board

Add any of three flags to tune the board for the session. Each one also turns on `--power`:

```powershell
.\scripts\run_all.ps1 --sync --gesture-flag --vocab new --bitstream gesture_model.xsa --cpu-mhz 300 --cores 1 --pl-mhz 25
```

| Flag | Values | Set by |
|---|---|---|
| `--cpu-mhz` | `300`, `400`, `600` or `1200` (the board's default) | the power monitor, before its first reading |
| `--cores` | `1` to `4` (default 4). cpu0 always stays on | the power monitor |
| `--pl-mhz` | `10` to `100` (default 100) | the board client, just after it loads the overlay, since loading resets the clock |

Use `--sync` when you change `--pl-mhz`: the client only sets the clock as it starts, so attaching to
a running client leaves it as it was (the Board pane warns). Without these flags, the board is left
as it is.

The Dashboard's **Tuning** row, under Power, shows the board's actual settings, read back every 5 s.
Each changed setting also shows what it was:

```text
  Tuning       CPU 300 MHz (was 1200)  1 core (was 4)  PL 25 MHz (was 100)
```

On Ctrl-C, every setting goes back to what it was. The Board pane's power line names the settings
the session ran with, for example `..., at CPU 300 MHz, 1 core(s), PL 25 MHz`, and
`power_summary.csv` records them in `cpu_mhz`, `cores` and `pl_mhz`.

`Comms/ultra96/board_settings.py` makes the changes. They are all runtime settings: the CPU clock,
the cores left on and the FPGA clock `fclk0`, never above what it is now. It leaves Wi-Fi, the
governor and every service alone. Before anything changes, each original goes into a state file:
`~/comms/power_state.json` for the monitor and `~/comms/pl_state.json` for the client. A guardian
process puts them back if the process that made the change dies, even with `kill -9`. If both are
killed, the next run or `board_settings.py restore` puts them back. A reboot resets them too.

`Comms/ultra96/power_sweep.py` measures each setting in turn: 20 s idle with the overlay loaded, then
20 s of back-to-back inferences, checking every answer against the baseline's. Don't run it during a
game, since it loads its own overlay; it refuses to start while the board client is running.

```bash
sudo -n env HOME="$HOME" XILINX_XRT=/usr ~/comms-venv/bin/python ~/comms/power_sweep.py [khz=399999+cores=1 ...]
```

Results from 13 Nov (`new_gestures_all.xsa`, the overlay deployed before `gesture_model.xsa`; no
answer changed under any setting):

| Setting | Idle W | Inferring W | Inference median / p95 ms |
|---|---|---|---|
| baseline (1.2 GHz, 4 cores, PL 100 MHz) | 2.195 | 2.378 | 2.67 / 2.79 |
| `khz=299999` | 2.188 | 2.230 | 5.09 / 5.42 |
| `cores=1` | 2.150 | 2.341 | 2.67 / 2.96 |
| `pl=50` | 2.126 | 2.319 | 4.54 / 4.70 |
| `pl=25` | 2.094 | 2.258 | 8.27 / 8.43 |
| `khz=399999+cores=1+pl=50` | 2.094 | 2.154 | 6.17 / 7.47 |
| `khz=299999+cores=1+pl=25` | 2.050 | 2.087 | 10.73 / 13.04 |

A game is nearly all idle, since inference runs well under 1% of the time, so the idle column is the
one that counts. The CPU clock saves nothing at idle, because idle cores are already asleep. Turning
cores off and slowing the FPGA clock are what save power.
