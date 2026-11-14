# CG4002 B16: BridgeRush

BridgeRush is a two-player augmented-reality game. Each player wears a glove with a motion sensor
and plays by making gestures. Gestures are classified on an Ultra96 board's FPGA. The game engine
also runs on the Ultra96, and each player's iPhone draws the match on the table in AR.

This page is the entry point. It covers the system as a whole, how to run it, and the one-time
setup. Each subsystem and each integration has its own detailed page, linked below.

## Contents

1. [System architecture](#1-system-architecture)
2. [Running the game](#2-running-the-game)
3. [One-time setup](#3-one-time-setup)
4. [Repository layout](#4-repository-layout)
5. [Tests](#5-tests)
6. [Troubleshooting](#6-troubleshooting)
7. [Further documentation](#7-further-documentation)
8. [Appendix A: every command and flag](#appendix-a-every-command-and-flag)
9. [Appendix B: actions, units and spells](#appendix-b-actions-units-and-spells)

## 1. System architecture

```text
 Glove 1, Glove 2  (FireBeetle ESP32 + MPU6050, one per player)
   | reads the IMU at 500 Hz, flags the samples that are part of a gesture,
   | sends a 26-byte packet every 10 ms
   |
   |  BLE, encrypted                                ^ VIBRATE after each classified gesture
   v                                                |
 Laptop relay  (Comms/laptop, run_pipeline.ps1)
   | checks each packet, works out the player, forwards it as framed JSON
   |
   |  TCP through a reverse SSH tunnel (via the NUS jump host)
   v
 Ultra96
   board client  (Comms/ultra96/tcp_client_mqtt.py)
     | per-player window of IMU samples
     | AI's classifier on the FPGA  (AI/board/wk10_integration.py + .xsa overlay)
     | publishes each gesture on game/gesture/N, and each glove's state on status/gloves
     v
   Mosquitto broker  (loopback only, 127.0.0.1:1883)
     ^ |
     | v
   game engine  (Comms/ultra96/game_engine.py + SW_Visualiser/engine/match.py)
     | runs the match at 10 ticks a second: mana, units, spells, towers, the result
     |
     |  MQTT, through each phone's own two-hop SSH tunnel
     v
 iPhone 1, iPhone 2  (Unity app, SW_Visualiser + the Comms drop-in)
   track the AprilTags and the lane, draw the match from the engine's snapshots,
   send their position and aim, START and card taps back to the engine
```

**Which part does what:**

| Part | Owner | Folder | Job |
|---|---|---|---|
| Glove firmware | Hardware, Comms | `Comms/firebeetle/` | Reads the IMU, detects gestures, streams packets over BLE, buzzes the motor |
| Laptop relay | Comms | `Comms/laptop/` | BLE to both gloves, TCP to the board, the SSH tunnel |
| Board client | Comms | `Comms/ultra96/` | Receives packets, runs AI, publishes gestures and glove status |
| Gesture classifier | AI | `AI/board/` | Turns a window of IMU samples into one of the gesture names, on the FPGA |
| Game engine | Comms (shell), Vis (rules) | `Comms/ultra96/game_engine.py`, `SW_Visualiser/engine/` | Runs the match. Who owns which part: [engine-ownership.md](docs/integration/engine-ownership.md) |
| Phone app | Vis | `SW_Visualiser/script/` | AR tracking, drawing, the HUD and menus |
| Phone link | Comms | `Comms/phone/comms-visualiser/` | The SSH tunnel and MQTT hook that Vis copies into the app |

**How the parts connect.** The laptop has no MQTT role. Everything on MQTT goes through the board's
own broker, which listens on loopback only. The phones reach it through SSH.

| Link | Transport | Protected by |
|---|---|---|
| Glove to laptop | BLE notifications, about 100 packets a second per glove | BLE pairing and encryption |
| Laptop to Ultra96 | One TCP connection, length-prefixed JSON, both directions | Reverse SSH tunnel through the jump host |
| Ultra96 to phones | MQTT, QoS 1 (`game/state` and `viz/lane` at QoS 0, since the next one replaces a lost one) | Each phone's two-hop SSH tunnel. The broker is never exposed |

**MQTT topics** (N is the player, 1 or 2):

| Topic | From, to | What |
|---|---|---|
| `game/gesture/N` | board client, to the engine | One classified gesture |
| `status/gloves` | board client, to the engine and phones | Each glove `ready` or `down`. Retained |
| `viz/lane/N` | phone N, to the engine and the other phone | Lane length, the player's position and aim, 10 times a second |
| `game/request/N` | phone N, to the engine | START, rematch, card taps, God mode |
| `viz/bubble_captured/N` | phone N, to the engine | A captured mana bubble |
| `game/match` | engine, to the phones | The match phase, and why it is waiting or paused. Retained |
| `game/state` | engine, to the phones | A snapshot of the match every tick |
| `game/event` | engine, to the phones | The result of each action, accepted or refused with a reason |
| `viz/echo/N` | phone N | Echoes each gesture it receives, for latency measurement |

Every message carries the same envelope: `event_id`, `source`, `seq`, `ts`, `payload`. Both ends
drop repeats by `event_id`. Field-level contracts are in code: `Comms/ultra96/engine_wire.py` on
the board and `SW_Visualiser/script/Networking/Engine/EngineMessages.cs` on the phone must change
together.

Players make seven moves: chop, jabs, draw a circle, shield raise, draw a star, zig-zag and forward
punch. AI sends each as the name of its action: `warrior`, `rogue`, `mage`, `shield` (a Tank),
`hero`, `lightning` and `fireball`. The full table is in [Appendix B](#appendix-b-actions-units-and-spells).

This setup is **Configuration B**. Configuration A, with the broker on the laptop over a phone
hotspot, is kept as a fallback: [Comms subsystem guide, section 5](docs/subsystem/comms/README.md#5-configuration-a-phone-mobile-hotspot-and-tailscale).

## 2. Running the game

Do the [one-time setup](#3-one-time-setup) first.

### Before each run

- The laptop is on NUS Wi-Fi. Both phones have the NUS VPN on.
- Both gloves are charged and on. Any other FireBeetle flashed as player 1 or 2 is off, since the
  laptop connects to whichever answers first.
- Both AprilTags are printed and placed.
- `team` is on `main` and up to date: `git switch main` then `git pull`.

### 1. Start the laptop and the board

In PowerShell, from `team\Comms`:

```powershell
.\scripts\run_all.ps1 --sync --gesture-flag --vocab new --bitstream new_gestures_all.xsa
```

This opens one Windows Terminal window with three panes. The **Relay** pane (top right) connects to
both gloves and opens the tunnel. The **Board** pane (bottom right) starts the board client and the
game engine on the Ultra96. The **Dashboard** (left) sums up both. If `.env` has no
`JUMP_PASSWORD`, each pane asks for the jump-host password once.

- `--sync` copies the current board code, the engine and AI's model to the Ultra96, then restarts
  both programs. Use it on the first run after any change. Without it, the board keeps running
  what it already has.
- `--gesture-flag` turns classification on.
- `--vocab new --bitstream new_gestures_all.xsa` load AI's current model, which gives the gesture
  names above.

Every launcher's flags are in [Appendix A](#appendix-a-every-command-and-flag).

You should see:

- **Relay:** `tunnel open`, then for each glove `connected` and `subscribed`, then about
  `rate=~100 Hz … missed=0` once a second.
- **Board:** `synced …` lines, `STARTED`, `ENGINE: started`, `classifier: AI`, then
  `gloves: player1=ready player2=ready`.
- **Dashboard:** both gloves streaming, the tunnel open, both gloves `ready` on the board.

### 2. Start the phones

On each iPhone:

1. NUS VPN on. Open the app, type the NUS password, press **Connect**, and wait for
   `MQTT_CONNECTED`. Both phones must be connected before anyone chooses a player.
2. Open **OPTIONS**. The row must read **`VS PLAYER engine: BOARD (next match)`**. If it says HOST
   PHONE, tap it. The phone remembers this.
3. Choose **VS PLAYER**, then the player that matches your glove: glove 1 is **Player 1**.
4. Lock in on the tag, then press **START**.

The match counts down for 3 s once both players have pressed START, both phones are streaming and
both gloves are `ready`. If it doesn't start, the screen says what it is waiting for.

### 3. Play

- Gesture with the glove, or tap a card. Keep the crosshair on your own half, or a unit is refused.
  What each gesture does, and what it costs: [Appendix B](#appendix-b-actions-units-and-spells).
- The glove buzzes once when a gesture starts, and again when the board has classified it.
- Stay inside the arena. After 3 s outside, your tower loses HP.
- **God mode** (OPTIONS) keeps your mana and tower HP full, for demos and testing.
- After a match, both players press **PLAY AGAIN** for a rematch. If an app was restarted, it shows
  **START** instead, which works the same.

### 4. Stop

Press **Ctrl-C in the Dashboard**. It stops the relay and the board client, and the board client
stops the engine. Then, from `team\Comms`, copy the board's logs to `Comms/logs/board/<date>/`:

```powershell
.\scripts\run_board.ps1 --fetch-logs
```

`.\scripts\run_board.ps1 --stop` stops the board client and the engine on their own.

### Watching the engine

The dashboard doesn't show the engine. To follow it, open a shell on the board (the `ssh` command
from [one-time setup](#laptop-windows), step 5) and run:

```bash
tail -F "$(ls -t ~/comms/logs/$(date -u +%Y-%m-%d)/run_engine_*.log | head -1)"
```

It logs each phase (`waiting`, `countdown`, `running`, `paused`, `ended`) and every action's result.

### Measuring the board's power

Add `--power` to the usual command:

```powershell
.\scripts\run_all.ps1 --sync --gesture-flag --vocab new --bitstream new_gestures_all.xsa --power
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

#### Tuning the board

Add any of three flags to tune the board for the session. Each one also turns on `--power`:

```powershell
.\scripts\run_all.ps1 --sync --gesture-flag --vocab new --bitstream new_gestures_all.xsa --cpu-mhz 300 --cores 1 --pl-mhz 25
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

Results from 13 Nov (`new_gestures_all.xsa`; no answer changed under any setting):

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

## 3. One-time setup

### Laptop (Windows)

Follow [Comms/README.md, "One-time setup"](Comms/README.md#one-time-setup), Laptop steps 1 to 6:
the Python 3.10 virtual environment in `Comms\.venv`, an SSH key registered with the jump host, the
key in ssh-agent, the key on the Ultra96, and `Comms\.env` from `.env.example`. `.env` holds hosts
and user names, so it is gitignored. Never commit it.

`run_all.ps1` also needs **Windows Terminal** (`wt.exe`). Without it, start
`.\scripts\run_pipeline.ps1` and `.\scripts\run_board.ps1 …` in two terminals instead.

### Ultra96

The board is already set up. It has:

- Mosquitto on `127.0.0.1:1883`, loopback only. The board launcher warns if it is missing or open
  wider;
- `~/comms-venv`, the Python environment both programs run in. The board client runs under `sudo`,
  since the FPGA needs root;
- PYNQ and XRT for AI's overlay;
- `~/comms/`, which `--sync` fills with the board code, the engine (`~/comms/engine/`) and AI's files.

Logs go to `~/comms/logs/<UTC date>/`. The board is NUS hardware, so keep any change reversible and
tell the team first.

### Gloves (FireBeetle ESP32)

1. Install the Arduino IDE, the ESP32 board package and **NimBLE-Arduino 2.x**.
2. Open `Comms/firebeetle/firebeetle.ino`. Set `#define PLAYER_ID` to 1 or 2 for that glove, and
   flash it. Set it back to the committed value afterwards, so `git status` stays clean.
3. Keep exactly one `.ino` in that folder, since the IDE builds every `.ino` it finds there.
4. The first connection to the laptop pairs by itself. Only a glove re-flashed with a different
   `PLAYER_ID` needs removing from Windows Bluetooth settings first.
5. The motor driver is on GPIO 2, which the ESP32 also reads at boot. If a glove won't flash or
   boot, disconnect the driver's wire while flashing.

What a healthy glove prints over serial, and the IMU wiring, are in
[full-pipeline.md, "Flash the gloves"](docs/integration/full-pipeline.md#1-flash-the-gloves) and
[hardware-comms.md](docs/integration/hardware-comms.md).

### AI model

Nothing to install by hand. `AI/board/` holds the classifier (`wk10_integration.py`) and the FPGA
overlays. `run_board.ps1 --sync` copies the classifier and the overlay named by `--bitstream` to the
board. How the board calls AI: [ai-comms.md](docs/integration/ai-comms.md).

### Phone app (iPhone)

1. **Build the app** in Unity from `SW_Visualiser/`. Packages and versions:
   [SW_Visualiser/README.md, "Requirements"](SW_Visualiser/README.md#requirements). Build for iOS
   through Xcode.
2. **Import the Comms drop-in** and wire it into the scene:
   [Comms/phone/comms-visualiser/README.md](Comms/phone/comms-visualiser/README.md), sections 1
   and 2. When the drop-in changes, copy it again, then rebuild.
3. **Set up each phone once:** the NUS VPN, and the tunnel form in the debug menu (jump host, NUS
   user, board host, both SSH fingerprints). Same README, section 3. Comms sends the real values
   privately. They are never written in the repo.
4. **Print both AprilTags** for the tracker: family tagStandard41h12, IDs 0 and 1, at the size the
   tracker is calibrated for (ask Vis). With the wrong tags, the lane comes out the wrong length and
   every spawn is refused `bad_aim`.

## 4. Repository layout

| Folder | What is in it |
|---|---|
| `AI/` | Training pipeline (`gesture_pipeline/`), and the board classifier and FPGA overlays (`board/`) |
| `Comms/firebeetle/` | Glove firmware, including Hardware's gesture detector |
| `Comms/laptop/` | Laptop relay, its tests (`tests/`) and `requirements.txt` |
| `Comms/ultra96/` | Board client, game engine shell, and their helpers |
| `Comms/scripts/` | Launchers: `run_all.ps1`, `run_pipeline.ps1`, `run_board.ps1`, and the dashboard |
| `Comms/phone/comms-visualiser/` | The drop-in for the phone app: SSH tunnel and MQTT hook |
| `Hardware/` | Glove CAD, data collection, and sensor test sketches |
| `SW_Visualiser/script/` | The phone app's scripts |
| `SW_Visualiser/engine/` | The game rules (`match.py`, `rules.json`) that run on the Ultra96, and their tests |
| `docs/` | Subsystem and integration guides ([index](docs/README.md)) |

## 5. Tests

| What | Where to run it | Command |
|---|---|---|
| Comms: relay, board client, engine shell | `Comms/laptop` | `..\.venv\Scripts\python -m pytest` |
| Game rules on the board | `SW_Visualiser/engine` | `python -m pytest` (with the same environment) |
| Phone message parsers, outside Unity | `SW_Visualiser/engine/csharp_checks` | `python ../dump_wire_cases.py CASES.json`, then `dotnet run -- CASES.json BUILT.json` (needs the .NET 10 SDK) |

`Comms/laptop/tests/test_record_ble.py` also needs `numpy`, which the Comms environment doesn't
install. Add `--ignore=tests/test_record_ble.py`, or install `numpy`.

## 6. Troubleshooting

| You see | Likely cause |
|---|---|
| Player 2's match ends at once, or Player 1 never starts | A phone is on HOST PHONE. Set both to BOARD in OPTIONS (section 2, step 2). The host-phone game can't run while the board engine runs, since both send `game/state` |
| `waiting for player N's phone` | That phone isn't streaming. Check its link, and that it is on BOARD |
| `player N glove not ready` | That glove isn't streaming at least 50 packets a second. Check the Relay pane and the Dashboard |
| `player 1 lane not measured` | Player 1's phone hasn't measured the lane. Check the tags |
| Every spawn refused `bad_aim` | The crosshair is off your half, or the tags are wrong |
| Board pane says `StubAI` | The board client found no FPGA. Tell AI before going on |
| Board pane says `WARN: nothing listening` | The laptop's tunnel isn't up. Check the Relay pane |
| Board pane says `WARN: no broker on 1883` | Start Mosquitto on the board: `sudo systemctl start mosquitto` |
| A phone stays connected but nothing updates | Its tunnel has stalled. It drops the link after 5 s without a snapshot and rebuilds the tunnel by itself, usually within about 30 s. If not, use the debug menu's Comms Disconnect and Connect, or relaunch the app |
| Gestures arrive but the game ignores them | The board has the old model, whose names the game doesn't know. Run again with `--sync --vocab new --bitstream new_gestures_all.xsa` |

Hop-by-hop expected log lines, from the glove to the board's MQTT publish, are in
[full-pipeline.md](docs/integration/full-pipeline.md#what-a-healthy-run-should-show). That page
predates the board engine: its phone steps describe the host-phone game.

## 7. Further documentation

- [docs/README.md](docs/README.md): the index of subsystem and integration guides.
- [Comms subsystem guide](docs/subsystem/comms/README.md): packet formats, TCP framing, the MQTT
  envelope, and both configurations.
- [Game engine ownership](docs/integration/engine-ownership.md): who maintains each part of the
  engine, and which changes need the other team.
- [Full pipeline](docs/integration/full-pipeline.md), [Hardware-Comms](docs/integration/hardware-comms.md)
  and [AI-Comms](docs/integration/ai-comms.md): each integration's contract and change guide.
- [System diagram](docs/b16-system-diagram.html): the whole system on one page.

## Appendix A: every command and flag

Run the launchers from `team\Comms` in PowerShell. Each `.ps1` uses `Comms\.venv`.

### A.1 `scripts\run_all.ps1`: everything in one window

```powershell
.\scripts\run_all.ps1 [arguments for run_board.ps1]
```

It opens the Dashboard, Relay and Board panes, each starting once the one before it has logged in.
**Every argument goes to the Board pane** (A.3). The Relay pane always runs `run_pipeline.ps1` with no
arguments. To give the relay a flag (A.2), start `run_pipeline.ps1` yourself instead. Ctrl-C in the
Dashboard stops all three.

| Run | Command |
|---|---|
| Normal run, after any change | `.\scripts\run_all.ps1 --sync --gesture-flag --vocab new --bitstream new_gestures_all.xsa` |
| Normal run, nothing changed | `.\scripts\run_all.ps1 --gesture-flag --vocab new --bitstream new_gestures_all.xsa` |

### A.2 `scripts\run_pipeline.ps1`: the laptop relay

```powershell
.\scripts\run_pipeline.ps1 [flags]
```

| Flag | Effect |
|---|---|
| `--nodes 1` or `--nodes 1,2` | Connect only to these gloves. Default: both. With one glove, use `--nodes`, or the relay keeps scanning for the missing one |
| `--no-tunnel` | Don't open the SSH tunnel. For a board client on the same laptop |
| `-v`, `--verbose` | Log every packet. Slows the relay, so not for throughput runs |
| `--debug-libs` | Also log the BLE and SSH libraries' internals. Their lines can include host names and addresses |
| `--tcp-host`, `--tcp-port` | The relay's TCP server address. The defaults match the tunnel |

Press Esc in the Relay pane to stop the relay on its own.

### A.3 `scripts\run_board.ps1`: the board client and the engine

```powershell
.\scripts\run_board.ps1 [its own flags] [board client flags]
```

Its own flags:

| Flag | Effect |
|---|---|
| (none) | Start the board client and the engine, or attach to them if they are running |
| `--sync` | Copy `Comms/ultra96/*.py`, the engine (`SW_Visualiser/engine/`) and AI's files to the board, then restart both. Needed after any change. The client keeps the flags it was started with until the next `--sync` |
| `--stop` | Stop the board client and the engine, then exit |
| `--fetch-logs [YYYY-MM-DD]` | Copy that UTC day's board logs to `Comms/logs/board/<date>/`, then exit. Default: today |
| `--port N` | Use tunnel port `N` instead of `.env`'s `TUNNEL_PORT` |
| `--power` | Also record the board's power, split into idle and playing. See [Measuring the board's power](#measuring-the-boards-power) |
| `--cpu-mhz N`, `--cores N`, `--pl-mhz MHZ` | Tune the board for the session; each turns on `--power`, and every setting is put back on Ctrl-C. See [Tuning the board](#tuning-the-board) |

Board client flags, passed on to `Comms/ultra96/tcp_client_mqtt.py`:

| Flag | Effect |
|---|---|
| `--gesture-flag` | Classify the glove's flagged gestures with AI. Without it, no gesture is classified |
| `--bitstream NAME` | The FPGA overlay to load, from `AI/board/`. Required with `--gesture-flag`. Current: `new_gestures_all.xsa` |
| `--vocab old` or `--vocab new` | The model's labels. Must match the overlay. `new` gives the seven game names. Default: `old` |
| `-v`, `--verbose` | Log every packet |
| `--host`, `--broker-host`, `--broker-port` | The tunnel's board end and the broker's address. The defaults are right |

### A.4 The game engine, on the board

The engine has no flags of its own in normal use. `run_board.ps1` starts `~/comms/run_engine.sh`,
which runs `game_engine.py` and restarts it after a crash: after 2 s, doubling up to 60 s. Exit code 3
means it fails the same way every time, for example a bad `rules.json`, so the loop stops.

```bash
~/comms-venv/bin/python ~/comms/game_engine.py [--host 127.0.0.1] [--port 1883] [--core-dir ~/comms/engine] [-v]
```

| Flag | Effect |
|---|---|
| `--host`, `--port` | The broker. Default: the board's own, on loopback |
| `--core-dir DIR` | The folder with `match.py` and `rules.json` |
| `-v`, `--verbose` | Debug logging |

### A.5 Board shell commands

Open a shell on the board: `ssh -J <nusnet-id>@<jump-host> xilinx@<board-host>`.

| Purpose | Command |
|---|---|
| Follow the engine | `tail -F "$(ls -t ~/comms/logs/$(date -u +%Y-%m-%d)/run_engine_*.log \| head -1)"` |
| Why the match is waiting or paused | `mosquitto_sub -h 127.0.0.1 -t game/match -C 1 -W 3` |
| What is running | `pgrep -af 'tcp_client_mqtt\|run_engine\|game_engine\|power_monitor'` |
| Stop only the engine | `pkill -f 'run_engine\.sh'; pkill -f 'game_engine\.py'` |
| Watch the gestures and glove status | `mosquitto_sub -h 127.0.0.1 -v -t 'game/gesture/+' -t status/gloves` |
| Send one gesture by hand | `mosquitto_pub -h 127.0.0.1 -q 1 -t game/gesture/1 -m '{"event_id":"manual-1","source":"manual","seq":0,"ts":0,"payload":{"type":"AI_EVENT","player":1,"seq":0,"gesture":"warrior","timestamp":0}}'`. Change `event_id` each time, since repeats are dropped |

### A.6 Other tools

| Command | Use |
|---|---|
| `python scripts\dashboard [--no-board]` | The dashboard on its own. `--no-board` shows the laptop rows only, with no SSH |
| `python scripts\fake_phone.py --player N [--lane M] [--start] [--tap CARD --every S] [--gesture NAME]` | Stand in for a phone against the engine, without Unity. `--host`/`--port` pick the broker |
| `python scripts\tools\latency_report.py` | Per-hop latency from the run logs. See its `--help` |
| `scripts\run_*_a*.ps1` | Configuration A, the fallback. See the [Comms subsystem guide](docs/subsystem/comms/README.md#5-configuration-a-phone-mobile-hotspot-and-tailscale) |

## Appendix B: actions, units and spells

Values are from `SW_Visualiser/engine/rules.json` (version `scene-checked-3nov-r3-tower-body`), which
the board engine uses. Distances are for the 2 m reference lane, and scale with the lane the phones
measure.

### B.1 Gestures and cards

Make the move with the gloved hand. AI's model recognises it and sends its name, and the engine does
the action. Each move does the same as tapping its card. A spawn needs the crosshair on your own half.
A spell can be aimed anywhere along the island.

| Move | Name sent | Action | Mana | What it does |
|---|---|---|---|---|
| Chop | `warrior` | Spawn a Warrior | 2 | 150 HP, 20 damage a second, close range. Crosses the lane in 10 s |
| Jabs | `rogue` | Spawn a Rogue | 3 | 120 HP, 35 damage a second, close range. The fastest: 6 s |
| Draw a circle | `mage` | Spawn a Mage | 4 | 90 HP, 30 damage a second, attacks from 0.35 m away. 12 s |
| Shield raise | `shield` | Spawn a Tank | 6 | 700 HP, 25 damage a second, close range. The slowest: 15 s |
| Draw a star | `hero` | Spawn a Hero | 8 | 450 HP, 70 damage a second, close range. 12 s |
| Zig-zag | `lightning` | Cast Lightning | 5 | Strikes the aim point 0.5 s later. Within 0.2 m: 180 damage to units, half at the edge, 60 to a tower |
| Forward punch | `fireball` | Cast Fireball | 6 | Flies from your tower at 1.2 m/s (at least 0.4 s). Within 0.12 m: 120 damage to units, half at the edge, 40 to a tower. Then the spot burns for 4 s, 12 damage a second to units in it |

The moves are AI's training set: [README_new_gestures.md](AI/gesture_pipeline/README_new_gestures.md).
Any other movement should come out as `noise`, which the engine refuses as an unknown gesture.

### B.2 Other actions

| Action | How | Effect |
|---|---|---|
| START | The START button, once both phones are connected and locked in | The match counts down 3 s once both players have pressed it and both gloves are ready |
| Capture a bubble | Hold the crosshair on the bubble at the other tower for 1.5 s | +2 mana. Then 1 s before the next capture |
| Rematch | **PLAY AGAIN** after a match | A new match starts once both players press it |
| God mode | OPTIONS, then `God mode` | Your mana and tower HP stay full, and actions cost nothing. Debug only. It lasts until switched off, across rematches |

### B.3 Match rules

| Rule | Value |
|---|---|
| Match length | 180 s |
| Tower HP | 1000 each. A tower hits back at units attacking it, 15 damage a second |
| Mana | Start at 5, at most 10, +1 a second. Double speed in the last 60 s |
| Out of the arena | After 3 s outside in a row, your tower loses 20 HP a second. Coming back in restarts the count |
| Winner | The player whose tower is still standing. At time up, the tower with more HP. Equal HP is a draw |
| Paused | A phone silent for 1 s, or a glove `down`, pauses the match. It resumes after 1 s of both back. A pause over 180 s ends the match with no winner |

### B.4 Why an action is refused

The phone shows the short text on the card and the long text in the banner.

| Reason | On the card | Meaning |
|---|---|---|
| `no_mana` | NO MANA | Not enough mana |
| `bad_aim` | BAD AIM | A spawn not on your own half, or a spell off the island |
| `tower_out_of_arena` | OUT OF ARENA | You are standing outside the arena |
| `not_running` | PAUSED | The match hasn't started, is paused, or is over |
| `phone_not_connected` | NO PHONE | A glove gesture, but the engine has had no recent aim from that player's phone |
| `unknown_gesture` | UNKNOWN | The gesture isn't one of the names in B.1 |
| `wrong_source` | ANOTHER PHONE | A second app is playing as the same player |
| `cooldown` | (not shown) | A bubble captured again within 1 s |
| `engine_error` | ERROR | The engine hit an error and ended the match. Check the engine log |
