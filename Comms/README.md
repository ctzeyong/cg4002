# Comms

Covers how to set up and run **Configuration B**, the configuration chosen. Packet formats, MQTT
topics, why B was chosen and the Configuration A alternative are in
[`docs/subsystem/comms/README.md`](../docs/subsystem/comms/README.md).

## Table of contents

- [Overview](#overview)
- [Network rule](#network-rule)
- [One-time setup](#one-time-setup)
  - [Laptop (Windows)](#laptop-windows)
  - [Ultra96](#ultra96)
  - [FireBeetles](#firebeetles)
  - [Phone](#phone)
- [Running Configuration B](#running-configuration-b)

## Overview

```text
FireBeetle 1 ─┐
FireBeetle 2 ─┴─ BLE ─> Laptop ── TCP, reverse SSH tunnel via stujump ──> Ultra96
                                                                            │ Mosquitto on the
Phone ─────────────── its own SSH tunnel via stujump ───────────────────────┘ board's loopback
```

- **Laptop:** BLE central for both FireBeetles. Forwards their packets to the Ultra96 over TCP.
  It has no MQTT role.
- **Ultra96:** receives the packets, creates gesture events (dummy events in this baseline) and
  publishes them to a Mosquitto broker on its own loopback.
- **Phone:** opens its own two-hop SSH tunnel (stujump, then the Ultra96) and subscribes to the
  broker through it.

The broker listens on the board's loopback only, so it can only be reached through SSH.

| Folder | Contents |
|---|---|
| `firebeetle/` | Arduino sketch for both FireBeetles; `#define PLAYER_ID 1` or `2` |
| `laptop/` | The relay: BLE, TCP server, SSH tunnel. Tests in `laptop/tests/` |
| `ultra96/` | Board client and MQTT publisher. Also the game engine being moved to the board (`game_engine.py`, `run_engine.sh`), not finished yet |
| `phone/tunnelled-visualiser/` | Unity test client for Configuration B |
| `phone/demo-visualiser/` | Configuration A's client (not used in B) |
| `scripts/` | Launchers. B is `run_pipeline.ps1` + `run_board.ps1`. `fake_phone.py` stands in for one player's phone when testing the engine without Unity |

Comments such as `notes/latency.md 3.13` point to Comms' design notes, which are kept outside this
repo. Ask Comms for the section.

## Network rule

The laptop and every phone each log in to stujump (the SoC student jump host) themselves.
**Without a registered SSH key, a device needs the NUS VPN, whatever network it is on.** A
registered key lets it skip the VPN, but only on NUS Wi-Fi. stujump asks for the NUS password
either way; the key does not replace it.

| Device | Logs in to stujump with | Network |
|---|---|---|
| Laptop | Registered key + NUS password | NUS Wi-Fi, VPN off. Off campus: VPN on |
| Phone | NUS password only (no key) | NUS VPN on, on any network (but for consistency, NUS Wi-Fi) |

## One-time setup

### Laptop (Windows)

1. **Python venv.** From `Comms/`, using Python 3.10:
   ```powershell
   py -3.10 -m venv .venv
   .\.venv\Scripts\pip install -r laptop\requirements.txt
   ```
2. **SSH key.** It must be at the default path `~\.ssh\id_ed25519`, or ssh never offers it.
   ```powershell
   ssh-keygen -t ed25519
   ```
3. **Register the key with stujump.** SoC manages these keys centrally, so adding the key to
   stujump's `authorized_keys` does nothing. Register it through SoC's skeys service instead.
   **This only works from the SoC campus network, not over the VPN.**
   ```powershell
   ssh <nusnet-id>@<skeys-host>    # at the prompt type: jump
   # paste the single line from ~\.ssh\id_ed25519.pub, then Enter, then Ctrl+D
   ```
   `jump` replaces any key registered before. `show` lists the current one.
4. **Load the key into ssh-agent** so you aren't asked for its passphrase on every run. Run the
   first two lines in an **elevated** PowerShell, and `ssh-add` in a normal one:
   ```powershell
   Set-Service ssh-agent -StartupType Automatic
   Start-Service ssh-agent
   ssh-add $env:USERPROFILE\.ssh\id_ed25519
   ```
5. **Add the key to the Ultra96.** The tunnel logs in to the board by key only, never by
   password. `<jump-host>` and `<board-host>` are `JUMP_HOST` and `BOARD_HOST` from step 6:
   ```powershell
   $key = (Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub" -Raw).Trim()
   ssh -J <nusnet-id>@<jump-host> xilinx@<board-host> `
     "mkdir -p ~/.ssh; chmod 700 ~/.ssh; echo '$key' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys"
   ```
   Check it: `ssh -J <nusnet-id>@<jump-host> xilinx@<board-host> hostname` should ask only for
   the stujump password.
6. **`.env`.** Copy `.env.example` to `.env` and fill in the jump user, jump host, board host and
   port. Leave `JUMP_PASSWORD` empty and the relay asks for it once at startup. Otherwise, fill it and you won't be prompted at startup. `.env` is
   gitignored; never commit it.

### Ultra96

The board is already setup.

### FireBeetles

Build `firebeetle/firebeetle.ino` in the Arduino IDE with NimBLE-Arduino 2.x. Set
`PLAYER_ID` to 1 or 2 before flashing each board. Keep exactly one `.ino` in that folder. The
first connection pairs automatically. If the GATT layout ever changes, remove the board in
Windows Bluetooth settings so Windows doesn't keep using its cached copy.

### Phone

1. Build `phone/tunnelled-visualiser` in Unity 6000.6.0f1 (Android, IL2CPP, ARM64) and install
   the APK.
2. In the app, fill in the jump host, NUS username, board host and board password. Turn **off**
   "Use private keys": the phone logs in to stujump by password, and turning it on without key
   files fails.
3. First run only: turn on "accept unknown host keys and print their fingerprints", connect,
   copy both printed fingerprints into the "Expected SHA256 fingerprint" fields, then turn the
   toggle off.

The app saves everything except the NUS password, which you type each launch.

## Running Configuration B

Before starting, 
1. Ensure the laptop is connected to NUS Wi-Fi with ssh keys configured
2. The phone visualiser is connected to NUS Wi-Fi with VPN

Open two Windows Terminal panes in `Comms/`. Each one asks for the NUS password once if it is not configured in `.env`. Either
can start first, since the board client keeps reconnecting, but laptop-first is easier to
follow.

```powershell
# Pane 1: laptop relay (BLE, TCP server, tunnel). Press keys here to send test commands.
.\scripts\run_pipeline.ps1 --nodes 1,2

# Pane 2: board client. Use --sync the first time after changing anything in ultra96/.
.\scripts\run_board.ps1 --sync
```

Then press Connect in the phone app. Events appear in the app's own log; that log, not the
broker, is the proof the phone received them. `.\scripts\run_board.ps1 --stop` stops the board
client, and the game engine if it is running. The other options are in the
[docs](../docs/subsystem/comms/README.md#2-run-configuration-b).

## Testing the model in the comms pipeline (laptop, before bitstream)

Run a trained `.h5` through the **real comms pipeline on the laptop** — glove → BLE → laptop → TCP →
classifier → event — with the model doing inference **locally instead of on the FPGA**. This validates it
under live streaming / offset conditions (not just the offline test set) before converting to a bitstream.

### What changed in the comms code

| file | change |
|---|---|
| `AI/board/keras_stream.py` | **new.** `GestureClassifier` subclassing `wk10_integration`'s, with the pynq/FPGA backend swapped for a local Keras `.h5` (`_KerasBackend`). Reuses the ring buffer, decode, end-of-gesture, softmax and `CONF_THRESHOLD`; maps class ids → game roles; feeds a 6-ch model directly, computes the 4 feature channels for a 10-ch model. No pynq import. |
| `Comms/ultra96/gesture.py` | `load_classifier(ai_h5=None)` — one added branch: if `ai_h5` is set, return the Keras classifier instead of the FPGA/stub path. |
| `Comms/ultra96/tcp_client.py` | `run()` / `main()` gain a `--ai-h5 PATH` flag (implies `--gesture-flag`). |

Without `--ai-h5` nothing changes — `wk10_integration.py`, the stub and the FPGA path are untouched.

### One-time: ML deps in the comms venv

`tcp_client.py --ai-h5` needs TensorFlow + qkeras. From `Comms/`:
```powershell
.\.venv\Scripts\pip install tensorflow tf-keras qkeras
```
(`keras_stream` sets `TF_USE_LEGACY_KERAS=1` for qkeras automatically.)

### Quick check — classify in the pipeline, no broker needed

Fastest way to confirm the model works live. Two Windows Terminal panes:
```powershell
# Terminal 1 (Comms/): laptop relay, no SSH tunnel (the "board" is local), one glove
.\scripts\run_pipeline.ps1 --no-tunnel --nodes 1
```
```powershell
# Terminal 2 (Comms/ultra96/): local board client, Keras model in place of the FPGA
python tcp_client.py --host 127.0.0.1 --port 8888 --ai-h5 "C:\Users\USER\Desktop\NUS\y4s1\CG4002\Model\baseline\ablation\models\new_lean_s1.h5"
```
Draw a gesture. The **client pane (Terminal 2) logs each classified gesture + confidence** — that's the
output to read. The glove buzzes (VIBRATE) on each recognized gesture, and Terminal 1 shows the sensor
`rate ~100 Hz` and the VIBRATE CMD. (Terminal 1 is the config-B laptop, which has no MQTT role, so it
won't drive a visualiser — for the model check you read the client pane.)

**When there is no event.** A flagged motion gives no AI event (no TCP event, no MQTT, no VIBRATE) when
the glove's detector rejected it (the glove buzzes twice), the model's top class is its no-gesture class
`noise` (8-class models), or the top probability is below `CONF_THRESHOLD` in
`AI/board/wk10_integration.py`. The client pane says which:
```text
no AI event: node 1's glove rejected its flagged gesture of 570 samples
no AI event: node 1's flagged gesture of 84 samples gave no result (noise p=0.94)
no AI event: node 1's flagged gesture of 61 samples gave no result (low confidence fireball p=0.55 < 0.70)
no AI event: node 1's flagged gesture of 12 samples gave no result (buffer not full: 12/150 samples)
```
A 7-class or an 8-class `.h5` both work with `--ai-h5` (the output size is read from the model).

**Never mix config-A and config-B launch scripts** (`run_pipeline_a` pairs with the config-A client; the chosen deployment is config B).
