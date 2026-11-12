# Communications pipeline

This is a guide to the standalone communications subsystem. The selected transport is
**Configuration B**: the Ultra96 creates dummy AI-shaped events for transport testing and
publishes them to a Mosquitto broker running locally on the Ultra96.

This document follows the Comms-individual implementation under `team/Comms`. It does not
describe the later AI-Comms integration or depend on files under `team/AI`.
With the integration, `--gesture-flag` passes every SENSOR sample to the FPGA classifier:
see [Full pipeline](../../integration/full-pipeline.md).

## Table of contents

- [1. Configuration B architecture](#1-configuration-b-architecture)
  - [Protocol quick reference](#protocol-quick-reference)
  - [BLE GATT endpoints](#ble-gatt-endpoints)
  - [BLE fixed packets](#ble-fixed-packets)
  - [BLE file-transfer packets](#ble-file-transfer-packets)
  - [TCP framing and messages](#tcp-framing-and-messages)
  - [Current MQTT topics](#current-mqtt-topics)
  - [MQTT envelope and payloads](#mqtt-envelope-and-payloads)
  - [Current scope](#current-scope)
- [2. Run Configuration B](#2-run-configuration-b)
  - [All three panes at once: run_all.ps1](#all-three-panes-at-once-run_allps1)
- [3. Relevant Configuration B files](#3-relevant-configuration-b-files)
- [4. Why Configuration B was selected](#4-why-configuration-b-was-selected)
- [5. Configuration A: phone Mobile Hotspot and Tailscale](#5-configuration-a-phone-mobile-hotspot-and-tailscale)

## 1. Configuration B architecture

```text
FireBeetles (players 1 and 2)
  |  BLE: SENSOR packets up; CMD packets down; ECHO packets up
  v
Laptop
  - BLE central
  - TCP server on 127.0.0.1:8888
  - reverse SSH tunnel exposes that server on Ultra96 127.0.0.1:8888
  - no MQTT role in Configuration B
  |
  |  framed, full-duplex JSON over TCP
  v
Ultra96
  - tcp_client_mqtt.py reconnects to 127.0.0.1:8888
  - ai_events.py creates dummy AI-shaped events
  - mqtt_publisher.py publishes QoS 1 to Mosquitto on 127.0.0.1:1883
  |
  |  game/gesture/{player}
  v
Phone
  - opens its own two-hop SSH tunnel through stujump to the Ultra96
  - connects MQTTnet to the forwarded board broker
  - deduplicates messages by event_id
```

The laptop-to-Ultra96 TCP connection carries both directions. Sensor packets travel to the
board, while heartbeats and acknowledgements maintain liveness. The board publishes a dummy
event directly to its local broker instead of sending the event back to the laptop.

There are currently two ways to create a dummy gesture event:

- A laptop keypress sends a CMD to a FireBeetle. Its ECHO proves the complete communications
  path and creates a dummy event. Keys `c`, `t`, `s`, `z`, `a`, and `l` select a named gesture;
  any other key selects one randomly.
- `--event-every N` creates a dummy event every `N` seconds without requiring BLE traffic. This
  is useful for testing only the event-to-MQTT part of the path.

### Protocol quick reference

| Boundary | Transport and format | Main traffic |
|---|---|---|
| FireBeetle ↔ laptop | Encrypted BLE GATT; binary packets | `SENSOR`, `CMD`, `ECHO`; standalone file-transfer packets |
| Laptop ↔ Ultra96 | Full-duplex TCP through the reverse SSH tunnel; 4-byte length + UTF-8 JSON | Decoded BLE messages and heartbeat/acknowledgement |
| Ultra96 ↔ broker | MQTT on board loopback `127.0.0.1:1883` | QoS 1 gesture publication |
| Broker ↔ phone | MQTT carried inside the phone's two-hop SSH tunnel | Gesture subscription and ready announcement |

Each layer has its own framing and reliability rules. The BLE CRC ends at the laptop; the
laptop decodes the bytes and creates a new JSON message for TCP. MQTT then wraps an AI event in
a separate envelope with an `event_id` used for QoS 1 duplicate suppression.

### BLE GATT endpoints

Each player has one service and two characteristics. Replace `{player}` with `1` or `2`.

| Endpoint | UUID | Properties and direction | Use |
|---|---|---|---|
| Service | `b1160001-0000-4000-8000-00000000000{player}` | Advertised by FireBeetle | Identifies the player during scanning; player ID is not stored in packet bytes. |
| SENSOR | `b1160002-0000-4000-8000-00000000000{player}` | `READ`, `NOTIFY`, encrypted read; FireBeetle → laptop | Carries `SENSOR`, `ECHO`, and returned file-transfer packets. |
| CMD | `b1160003-0000-4000-8000-00000000000{player}` | Encrypted `WRITE`; laptop → FireBeetle | Carries `CMD` and outbound file-transfer packets. |

The firmware transmits only after the connection is encrypted and the negotiated MTU can carry
a 20-byte application packet (`MTU >= 23`, including ATT overhead). BLE's default MTU is 23, so
streaming starts as soon as the link is encrypted. SENSOR packets are emitted every 10 ms, giving
a target rate of 100 Hz per FireBeetle.

### BLE fixed packets

`SENSOR`, `CMD`, and `ECHO` use the same fixed 20-byte little-endian layout:

| Bytes | Size | Field | Details |
|---|---:|---|---|
| `0` | 1 | `type` | Packet type listed below. |
| `1` | 1 | `seq` | Unsigned 8-bit sequence; wraps `255 → 0`. SENSOR and CMD/ECHO use separate sequence spaces. |
| `2–3` | 2 | `ax` | Signed `int16`, little-endian. |
| `4–5` | 2 | `ay` | Signed `int16`, little-endian. |
| `6–7` | 2 | `az` | Signed `int16`, little-endian. |
| `8–9` | 2 | `gx` | Signed `int16`, little-endian. |
| `10–11` | 2 | `gy` | Signed `int16`, little-endian. |
| `12–13` | 2 | `gz` | Signed `int16`, little-endian. |
| `14–17` | 4 | Reserved | Two signed `int16` values. In a SENSOR packet, the first is the IMU status (`0` legacy, `1` OK, `2` missing, `3` recovering) and the second is the gesture flag. |
| `18–19` | 2 | CRC | CRC-16/CCITT-FALSE over bytes `0–17`, stored little-endian. Polynomial `0x1021`, initial value `0xFFFF`. |

| Type | Hex | Direction | Current behaviour |
|---|---:|---|---|
| `SENSOR` | `0x01` | FireBeetle → laptop | Free-running 100 Hz stream from one MPU6050 per player. It carries raw ±4 g / ±500 °/s counts, or zeros with an explicit missing/recovering status. |
| `CMD` | `0x03` | Laptop → FireBeetle | A keypress creates 8 fields. Field 0 contains the masked key code; the remaining IMU fields are random test data and the reserved fields are zero. |
| `ECHO` | `0x04` | FireBeetle → laptop | Reply to one CMD. It keeps the CMD sequence, changes the type, negates the first six fields, leaves the two reserved fields unchanged, and recomputes the CRC. |

The laptop validates packet length, CRC, and type before forwarding. A successful BLE GATT write
acknowledges only the transport write; receiving the application-level ECHO is the stronger proof
that the FireBeetle processed the CMD. CMDs are attempted once in the normal pipeline, so an ECHO
can be lost and this path should not be described as exactly-once delivery.

### BLE file-transfer packets

This was only used to fulfil individual demo rubrics and will not be used anywhere else.

### TCP framing and messages

Every TCP message is framed as:

```text
[4-byte unsigned big-endian JSON length][that many bytes of UTF-8 JSON]
```

The receiver uses exact-length reads because TCP may split or merge writes. JSON payloads are
capped at 1 MiB; invalid JSON or an oversized length closes the connection because framing can no
longer be trusted.

| Message | Direction in B | Required fields and meaning |
|---|---|---|
| `SENSOR` / `ECHO` | Laptop → Ultra96 | `type`, `node`, `seq`, `timestamp`, `fields`; fixed packets also include `imu` (`ax`…`gz`), `reserved`, and validated integer `crc`. `node` comes from the BLE connection and `timestamp` is the laptop receive time. |
| `HEARTBEAT` | Ultra96 → laptop every 1 s | `type`, `seq`, `ts`. Keeps the full-duplex session alive. |
| `HEARTBEAT_ACK` | Laptop → Ultra96 | `type`, `seq`, `t1`, `t2`; `t1` echoes the heartbeat timestamp and `t2` is the laptop reply timestamp. |
| `AI_EVENT` | Not sent over TCP in B | Configuration A only: dummy AI-shaped event with `type`, `player`, `seq`, `gesture`, and `timestamp`. In B the same event becomes the MQTT envelope payload directly on the Ultra96. |

Example decoded SENSOR message:

```json
{
  "type": "SENSOR",
  "node": 1,
  "seq": 42,
  "timestamp": 12345.678,
  "fields": [120, -4, 8190, 7, -2, 1, 1, 0],
  "imu": {"ax": 120, "ay": -4, "az": 8190, "gx": 7, "gy": -2, "gz": 1},
  "reserved": [1, 0],
  "crc": 19132
}
```

### Current MQTT topics

| Topic | Producer → consumer | QoS | Retained | Current use in Configuration B |
|---|---|---:|---|---|
| `game/gesture/{player}` | Ultra96 → phones | 1 | No | Live gesture event. Phones subscribe to `game/gesture/+`; player attribution comes from the final topic segment. |
| `status/gloves` | Ultra96 → phones | 1 | Yes, with a last will | Whether each glove is streaming: `{"player1": "ready", "player2": "down"}` inside the envelope. `ready` is at least 50 packets with an IMU reading in the last second; `down` is fewer than 25 (in between, a glove keeps its last state, so one near 50 Hz doesn't flip); `offline` means the board's client stopped or died (published on a clean stop, and by the broker as the client's will). Published on each change and again after every reconnect. The phone hook shows each change. |
| `viz/ready/{player}` | Phone → broker | 1 | No | Phone publishes an envelope whose inner payload is `{"status":"ready","player":N}` after subscribing. Diagnostic only in B: the current Ultra96 code is publish-only, so nothing consumes it. |
| `comms/ping` | Diagnostic client → same diagnostic client | Configurable, default 1 | No | Used only by `mqtt_ping.py` for broker round-trip measurements; not part of the game pipeline. |

QoS 1 is at-least-once, so a message can arrive more than once. The phone keeps a bounded set of
seen `event_id` values and suppresses duplicates. In Configuration B, gesture events older than
2 seconds are discarded before publication rather than displayed late.

`game/state`, `game/event`, `game/match`, and `viz/lane/{player}` are planned game-engine topics,
not currently used topics. `map/obstacles` was removed from the contract.

### MQTT envelope and payloads

Every current application topic carries a JSON envelope:

| Field | Type | Meaning |
|---|---|---|
| `event_id` | Non-empty string | Unique ID in `<source>:<session>:<seq>` form; used for deduplication. |
| `source` | String | Publisher role, such as `ultra96-relay` or `phone-visualiser-1`. |
| `seq` | Integer | Publisher-local application sequence. |
| `ts` | Number | Publisher wall-clock time in Unix seconds. |
| `payload` | Any JSON value | Message-specific content; routing information stays in the topic. |

Gesture message example:

```json
{
  "event_id": "ultra96-relay:3f2a1b9c:17",
  "source": "ultra96-relay",
  "seq": 17,
  "ts": 1792000000.0,
  "payload": {
    "type": "AI_EVENT",
    "player": 1,
    "seq": 17,
    "gesture": "warrior",
    "timestamp": 1792000000.0
  }
}
```

This standalone Comms implementation does not run the FPGA classifier. All `AI_EVENT` messages
are transport stand-ins: `gesture` is one of the classifier's seven role names (`warrior`,
`mage`, `fireball`, `rogue`, `lightning`, `hero`, `shield`), each a member of the Vis game's
`GestureSymbol` enum, picked by the pressed key or at random. The game engine prices every action itself, so the event carries no
game-state value.

### Current scope

The implemented live output is `game/gesture/{player}`. Actual AI classification is outside this
standalone Comms baseline. Under Configuration B the game engine moves onto the Ultra96 as its
own process next to the relay, on branch `engine-move`. It subscribes to the phones' topics and
publishes `game/state`, `game/event` and `game/match`. It has not run on the board yet. Who owns
which part is in [Game engine on the Ultra96](../../integration/engine-ownership.md).

The Configuration B proof client is
[`Comms/phone/tunnelled-visualiser`](../../../Comms/phone/tunnelled-visualiser/README.md). It is
separate from the team's main `SW_Visualiser`; integrating the transport into the main game
visualiser is a separate step.

## 2. Run Configuration B

Run both commands from `team/Comms` in separate Windows Terminal panes. Laptop-first is easier
to read, although either half may start first because the board client reconnects automatically.

Before starting, check that:

- `Comms/.env` exists and contains the jump-host and Ultra96 details from `.env.example`;
- `Comms/.venv` has the packages in `laptop/requirements.txt`;
- the Ultra96 has loopback-only Mosquitto and `~/comms-venv` configured;
- the laptop and phone can reach `stujump`. Without a registered SSH key a device needs the NUS
  VPN on any network, so the phone uses the VPN (and for consistency, NUS Wi-Fi), while the laptop (key registered) uses NUS Wi-Fi
  with the VPN off. Setup steps are in [`Comms/README.md`](../../../Comms/README.md);
- the FireBeetles are powered; and
- the tunnelled visualiser is installed and configured on the phone.

### Terminal 1: laptop relay

```powershell
cd <path-to-team>\Comms
.\scripts\run_pipeline.ps1 --nodes 1,2
```

This starts BLE, the TCP server, the reverse SSH tunnel, keyboard CMD input, and logging. It
does not start an MQTT client or broker on the laptop.

### Terminal 2: Ultra96 client

For the first run after code changes, sync the board files:

```powershell
cd <path-to-team>\Comms
.\scripts\run_board.ps1 --sync
```

For later runs, omit `--sync`:

```powershell
.\scripts\run_board.ps1
```

`--sync` copies `ultra96/*.py` to `~/comms/`, then restarts the board client. The launcher also
checks that the reverse tunnel and Mosquitto are listening before it tails the board log.

Useful variants:

| Command | Use |
|---|---|
| `.\scripts\run_board.ps1` | Normal communications/ECHO path. |
| `.\scripts\run_board.ps1 --event-every 2` | Publish a dummy event every two seconds for an MQTT-only diagnostic. |
| `.\scripts\run_board.ps1 --fetch-logs` | Copy today's UTC board logs into `Comms/logs/board/`. |
| `.\scripts\run_board.ps1 --stop` | Stop the Configuration B client on the Ultra96. |

Do not use `--event-every` as evidence that BLE or the phone display works. It only tests event
creation and the later MQTT path. Nor does it prove FPGA inference, because this standalone
implementation does not run the classifier ([Full pipeline](../../integration/full-pipeline.md) adds it). Broker acceptance is also not proof
of phone receipt; confirm the event in the phone's own `received` display/log.

### All three panes at once: run_all.ps1

`scripts/run_all.ps1` opens the relay, the board client, and a read-only status dashboard as
three panes in one Windows Terminal window, instead of the two panes above:

```powershell
cd <path-to-team>\Comms
.\scripts\run_all.ps1 --sync
```

Ctrl-C in the dashboard pane stops all three, each as if Ctrl-C had been pressed in its own
pane; closing the dashboard pane stops only the dashboard. Each pane still runs the same
command shown above, so any one of them can be closed and restarted on its own.

**Arguments to `run_all.ps1` go to the board pane only** — the same forwarding rule as
`run_board.ps1` above, not to the relay. The full flag table (`--sync`, `--stop`,
`--fetch-logs`, `--event-every`, `--gesture-flag`, `-v`, …) is in
[full-pipeline.md](../../integration/full-pipeline.md#all-three-panes-at-once-run_allps1),
since that's where `run_all.ps1` is actually run day to day.

## 3. Relevant Configuration B files

| Area | File | Responsibility |
|---|---|---|
| Launch | [`scripts/run_pipeline.ps1`](../../../Comms/scripts/run_pipeline.ps1) | Starts the laptop half. |
| Launch | [`scripts/run_board.ps1`](../../../Comms/scripts/run_board.ps1) and [`run_board.py`](../../../Comms/scripts/run_board.py) | Syncs, starts, stops, and tails the Ultra96 client. |
| Launch | [`scripts/run_all.ps1`](../../../Comms/scripts/run_all.ps1) | Opens both of the above plus the status dashboard as three panes in one window; see [§2](#all-three-panes-at-once-run_allps1). |
| Launch | [`scripts/dashboard/`](../../../Comms/scripts/dashboard) | Read-only status pane: tails the relay's newest log and runs `mosquitto_sub` on the board over its own SSH connection. Never talks to the relay or board client directly. Started by `run_all.ps1`, but can also run standalone (`python scripts/dashboard`). |
| Laptop | [`laptop/pipeline_board_broker.py`](../../../Comms/laptop/pipeline_board_broker.py) | Configuration B orchestration; deliberately has no MQTT role. |
| Laptop | [`laptop/ble_client.py`](../../../Comms/laptop/ble_client.py) | BLE connections, packet queues, CMD/ECHO handling. |
| Laptop | [`laptop/tcp_server.py`](../../../Comms/laptop/tcp_server.py) | Framed TCP server, heartbeats, and the active board connection. |
| Laptop | [`laptop/tunnel.py`](../../../Comms/laptop/tunnel.py) | Supervised reverse SSH tunnel to the Ultra96. |
| Ultra96 | [`ultra96/tcp_client_mqtt.py`](../../../Comms/ultra96/tcp_client_mqtt.py) | Configuration B TCP client and MQTT event source. |
| Ultra96 | [`ultra96/tcp_client.py`](../../../Comms/ultra96/tcp_client.py) | Shared receiver, heartbeat, dummy SENSOR/ECHO triggers, and reconnect logic; also Configuration A's entry point. |
| Ultra96 | [`ultra96/ai_events.py`](../../../Comms/ultra96/ai_events.py) | Dummy gesture names (the Vis enum's), key mapping, and the `AI_EVENT` schema. |
| Ultra96 | [`ultra96/mqtt_publisher.py`](../../../Comms/ultra96/mqtt_publisher.py) | QoS 1 publishing, reconnects, bounded queue, and stale-event expiry. |
| Ultra96 | [`ultra96/envelope.py`](../../../Comms/ultra96/envelope.py) | MQTT topic and stable message envelope. |
| Phone | [`phone/tunnelled-visualiser`](../../../Comms/phone/tunnelled-visualiser/README.md) | Nested SSH tunnel, MQTT subscription, deduplication, and proof UI. |
| Phone | [`phone/comms-visualiser`](../../../Comms/phone/comms-visualiser/README.md) | The team app's drop-in. Nested SSH tunnel with reconnects and a rebuild when nothing answers, the gloves' status line and the latency echo. |
| Tests | [`laptop/tests`](../../../Comms/laptop/tests) | Software tests for BLE, TCP, MQTT, launchers, and transport behaviour. |

The actual classifier and its label-to-game-action mapping must be integrated separately. The
`gesture` field is currently dummy transport data, though its names are already the game's.

## 4. Why Configuration B was selected

Configuration B was chosen for the target campus setup because it gave the shorter and more
reliable measured path, while keeping the laptop focused on BLE and TCP:

- A board-created event is published on the board instead of crossing the SSH tunnel back to a
  laptop broker.
- The laptop does not need to run Mosquitto, expose a laptop-hosted broker over the phone's
  Mobile Hotspot, or act as an MQTT relay.
- In the 5 October campus comparison, B was about 44 ms median end-to-end versus about 79 ms for
  A, with no event loss in either run.
- In the 6 October campus session, B lost no events; A lost three events and its board link stayed
  down for the final four minutes.
- Usual NUS VPN does not allow LAN access, i.e phones cannot connect to the broker even though they are connected to the same hotspot (LAN). A TA only found the fix in Week 7, which was to use Tailscale VPN instead.

These measurements cover the standalone Comms dummy-event path, not classifier performance.
They justify B for the network that was tested; they do not prove that MQTT-on-board is
universally faster. The tested A path used a phone hotspot and Tailscale through a DERP relay.
Configuration B also has a real operational cost: every phone needs its nested SSH connection and
jump-host credentials, and the Ultra96 broker must be available. While the broker is down, each
phone rebuilds its tunnel about once a minute, and each rebuild is two logins on the shared jump
host.

## 5. Configuration A: phone Mobile Hotspot and Tailscale

The supported Configuration A topology uses a phone's Mobile Hotspot as the laptop-to-phone
network and Tailscale for the laptop-to-Ultra96 path. Mosquitto and the MQTT relay run on the
laptop:

```text
FireBeetles --BLE--> laptop pipeline.py <--TCP over SSH/Tailscale--> Ultra96 tcp_client.py
                         |
                         | MQTT through the hotspot LAN
                         v
              visualiser phone (hotspot owner)
```

The board still receives sensor data and creates the same dummy AI-shaped events. The difference
is the event sink: `tcp_client.py` sends each event back over TCP, then `laptop/pipeline.py` wraps
and publishes it to the laptop's broker. The launcher binds Mosquitto to the laptop's current
private IP address on the phone hotspot and prints that address for the visualiser. Use the
printed address each run because the hotspot lease may change.

Before running A:

- turn on the phone's Mobile Hotspot and join it from the laptop;
- connect Tailscale on the laptop and Ultra96, with `TAILSCALE_BOARD_HOST` set in `Comms/.env`;
- NOTE: disconnect Cisco AnyConnect;
- install Mosquitto in its default Windows path and stop its Windows service if it already holds
  port 1883; and
- enable the Windows firewall rule `B16 MQTT 1883 local subnet` so the phone can reach the
  laptop broker.

The laptop launcher validates the network, starts a temporary Mosquitto instance on loopback and
the hotspot lease, opens the Tailscale SSH tunnel, prints the broker address for the phone, and
cleans up the broker and tunnel when the relay exits.

### Terminal 1: laptop broker and relay

```powershell
cd <path-to-team>\Comms
.\scripts\run_pipeline_a_hotspot.ps1 --nodes 1,2
```

### Terminal 2: Configuration A board client

```powershell
cd <path-to-team>\Comms
.\scripts\run_board_a_hotspot.ps1 --sync
```

After the first synchronized run, omit `--sync`.

Never mix the pairs:

| Configuration | Laptop | Ultra96 |
|---|---|---|
| B | `run_pipeline.ps1` | `run_board.ps1` / `tcp_client_mqtt.py` |
| A | `run_pipeline_a_hotspot.ps1` | `run_board_a_hotspot.ps1` / `tcp_client.py` |

A mixed pair can leave every process apparently healthy while the phone receives no event.
