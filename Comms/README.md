# Comms subsystem guide

Details of the Communications subsystem: how the gloves, the laptop, the
Ultra96 and the phones are linked, the packet and message formats, and where the MQTT broker
runs. How to run the game, and the game engine's topics, are in the
[team README](../README.md).

The code is in this folder: the glove firmware (`firebeetle/`), the laptop relay (`laptop/`), the
Ultra96 programs (`ultra96/`), the phone code (`phone/`: the Unity app's drop-in Comms scripts and two test clients) and
the start-up scripts (`scripts/`).
Testing a model on the laptop and recording training data over BLE are in
[docs/model-development.md](../docs/model-development.md).

## Table of contents

- [1. Architecture](#1-architecture)
  - [Protocol quick reference](#protocol-quick-reference)
  - [BLE GATT endpoints](#ble-gatt-endpoints)
  - [BLE fixed packets](#ble-fixed-packets)
  - [BLE file-transfer packets](#ble-file-transfer-packets)
  - [TCP framing and messages](#tcp-framing-and-messages)
  - [Current MQTT topics](#current-mqtt-topics)
  - [MQTT envelope and payloads](#mqtt-envelope-and-payloads)
  - [Game engine](#game-engine)
- [2. Broker location](#2-broker-location)
  - [Why the broker runs on the Ultra96](#why-the-broker-runs-on-the-ultra96)
  - [Alternative evaluated: broker on the laptop](#alternative-evaluated-broker-on-the-laptop)
  - [Mutual TLS: built, not used](#mutual-tls-built-not-used)

## 1. Architecture

```text
FireBeetles (players 1 and 2)
  |  BLE: SENSOR packets up; CMD packets down; ECHO packets up
  v
Laptop
  - BLE central
  - TCP server on 127.0.0.1:8888
  - reverse SSH tunnel exposes that server on Ultra96 127.0.0.1:8888
  - no MQTT role
  |
  |  framed, full-duplex JSON over TCP
  v
Ultra96
  - tcp_client_mqtt.py reconnects to 127.0.0.1:8888
  - gesture.py feeds each glove's samples to AI's classifier on the FPGA
  - ai_events.py turns each result into an AI_EVENT
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
board, while heartbeats and acknowledgements maintain liveness. The board publishes each
event directly to its local broker instead of sending it back to the laptop.

With `--gesture-flag`, the board makes an event for each gesture the glove flags and AI
classifies. Two test sources make dummy events instead:

- A laptop keypress sends a CMD to a FireBeetle. Its ECHO proves the complete communications
  path and creates a dummy event. Keys `w`, `m`, `f`, `r`, `l`, `h` and `s` select a named gesture;
  any other key selects one randomly.
- `--event-every N` creates a dummy event every `N` seconds without requiring BLE traffic. This
  is useful for testing only the event-to-MQTT part of the path.

### Protocol quick reference

| Boundary | Transport and format | Main traffic |
|---|---|---|
| FireBeetle ↔ laptop | Encrypted BLE GATT; binary packets | `SENSOR`, `CMD`, `ECHO`; file-transfer packets (individual test only) |
| Laptop ↔ Ultra96 | Full-duplex TCP through the reverse SSH tunnel; 4-byte length + UTF-8 JSON | Decoded BLE messages and heartbeat/acknowledgement |
| Ultra96 ↔ broker | MQTT on board loopback `127.0.0.1:1883` | QoS 1 gesture publication |
| Broker ↔ phone | MQTT carried inside the phone's two-hop SSH tunnel | Gestures, glove status and the game engine's topics |

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

This was only used for the individual subsystem test and is not used in the final system.

### TCP framing and messages

Every TCP message is framed as:

```text
[4-byte unsigned big-endian JSON length][that many bytes of UTF-8 JSON]
```

The receiver uses exact-length reads because TCP may split or merge writes. JSON payloads are
capped at 1 MiB; invalid JSON or an oversized length closes the connection because framing can no
longer be trusted.

| Message | Direction | Required fields and meaning |
|---|---|---|
| `SENSOR` / `ECHO` | Laptop → Ultra96 | `type`, `node`, `seq`, `timestamp`, `imu` (`ax`…`gz`), `reserved`, and validated integer `crc`. An `ECHO` also carries `fields`, all eight values as one list, because the Ultra96 reads the pressed key from `fields[0]`. `node` comes from the BLE connection and `timestamp` is the laptop receive time. |
| `HEARTBEAT` | Ultra96 → laptop every 1 s | `type`, `seq`, `ts`. Keeps the full-duplex session alive. |
| `HEARTBEAT_ACK` | Laptop → Ultra96 | `type`, `seq`, `t1`, `t2`; `t1` echoes the heartbeat timestamp and `t2` is the laptop reply timestamp. |
| `AI_EVENT` | Not sent over TCP | Laptop-broker setup only (section 2): the AI event, with `type`, `player`, `seq`, `gesture`, and `timestamp`. Here the same event becomes the MQTT envelope payload directly on the Ultra96. |

Example decoded SENSOR message:

```json
{
  "type": "SENSOR",
  "node": 1,
  "seq": 42,
  "timestamp": 12345.678,
  "imu": {"ax": 120, "ay": -4, "az": 8190, "gx": 7, "gy": -2, "gz": 1},
  "reserved": [1, 0],
  "crc": 19132
}
```

### Current MQTT topics

| Topic | Producer → consumer | QoS | Retained | Current use |
|---|---|---:|---|---|
| `game/gesture/{player}` | Ultra96 → game engine and phones | 1 | No | Live gesture event. Phones subscribe to `game/gesture/+`; player attribution comes from the final topic segment. |
| `status/gloves` | Ultra96 → game engine and phones | 1 | Yes, with a last will | Whether each glove is streaming: `{"player1": "ready", "player2": "down"}` inside the envelope. `ready` is at least 50 packets with an IMU reading in the last second; `down` is fewer than 25 (in between, a glove keeps its last state, so one near 50 Hz doesn't flip); `offline` means the board's client stopped or died (published on a clean stop, and by the broker as the client's will). Published on each change and again after every reconnect. The phone hook shows each change. |
| `viz/ready/{player}` | Phone → broker | 1 | No | Phone publishes an envelope whose inner payload is `{"status":"ready","player":N}` after subscribing. Diagnostic only: the current Ultra96 code is publish-only, so nothing consumes it. |
| `comms/ping` | Diagnostic client → same diagnostic client | Configurable, default 1 | No | Used only by `mqtt_ping.py` for broker round-trip measurements; not part of the game pipeline. |

QoS 1 is at-least-once, so a message can arrive more than once. The phone keeps a bounded set of
seen `event_id` values and suppresses duplicates. Gesture events older than
2 seconds are discarded before publication rather than displayed late.

The game engine's topics (`game/state`, `game/event`, `game/match`, `game/request/{player}`,
`viz/lane/{player}` and `viz/bubble_captured/{player}`) and the latency echo `viz/echo/{player}`
are listed in the [team README](../README.md#1-system-architecture).

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

`gesture` is one of the classifier's seven role names (`warrior`, `mage`, `fireball`, `rogue`,
`lightning`, `hero`, `shield`), each a member of the Vis game's `GestureSymbol` enum. A dummy
event picks one by the pressed key, or at random. The game engine prices every action itself, so the event carries no
game-state value.

### Game engine

The game engine runs on the Ultra96 as its own process next to the board client
(`ultra96/game_engine.py`, started by `run_board.ps1`). It subscribes to the phones' topics and
publishes `game/state`, `game/event` and `game/match`. Those topics are listed in the
[team README](../README.md#1-system-architecture).

The team's app reaches the board through
[`Comms/phone/comms-visualiser`](phone/comms-visualiser/README.md).
[`Comms/phone/tunnelled-visualiser`](phone/tunnelled-visualiser/README.md) is a
smaller test client for the transport alone.

## 2. Broker location

### Why the broker runs on the Ultra96

The broker was put on the Ultra96 for the target campus setup because it gave the shorter and more
reliable measured path, while keeping the laptop focused on BLE and TCP:

- A board-created event is published on the board instead of crossing the SSH tunnel back to a
  laptop broker.
- The laptop does not need to run Mosquitto, expose a laptop-hosted broker over the phone's
  Mobile Hotspot, or act as an MQTT relay.
- In the 5 October campus comparison, the board broker was about 44 ms median end-to-end versus
  about 79 ms for the laptop broker (see below), with no event loss in either run.
- In the 6 October campus session, the board broker lost no events; the laptop broker lost three
  events and its board link stayed down for the final four minutes.
- Usual NUS VPN does not allow LAN access, i.e phones cannot connect to the broker even though they are connected to the same hotspot (LAN). A TA only found the fix in Week 7, which was to use Tailscale VPN instead.

These measurements were taken on the dummy-event path, before the classifier was added.
They justify the board broker for the network that was tested; they do not prove that MQTT-on-board is
universally faster. The tested laptop-broker path used a phone hotspot and Tailscale through a DERP relay.
The board broker also has a real operational cost: every phone needs its nested SSH connection and
jump-host credentials, and the Ultra96 broker must be available. While the broker is down, each
phone rebuilds its tunnel about once a minute, and each rebuild is two logins on the shared jump
host.

### Alternative evaluated: broker on the laptop

The design report calls this setup configuration A, and the final system configuration B.
It was measured against the final system but is not used in it. It is kept so that
comparison can be re-run.

The game cannot be played in this setup. The game engine is not started and the board
publishes nothing, so the team's app receives nothing; AI events reach only the Comms test
visualiser, `phone/demo-visualiser`. Its last full run was the 6 October comparison, and its
board launcher was last checked on 15 October. Both were before the engine moved to the Ultra96
and the packet shrank to 20 bytes. Since then it has been kept working by the unit tests only.

The supported topology uses a phone's Mobile Hotspot as the laptop-to-phone
network and Tailscale for the laptop-to-Ultra96 path. Mosquitto and the MQTT relay run on the
laptop:

```text
FireBeetles --BLE--> laptop pipeline.py <--TCP over SSH/Tailscale--> Ultra96 tcp_client.py
                         |
                         | MQTT through the hotspot LAN
                         v
              visualiser phone (hotspot owner)
```

The board still receives sensor data and creates the same AI events. The difference
is the event sink: `tcp_client.py` sends each event back over TCP, then `laptop/pipeline.py` wraps
and publishes it to the laptop's broker. The launcher binds Mosquitto to the laptop's current
private IP address on the phone hotspot and prints that address for the visualiser. Use the
printed address each run because the hotspot lease may change.

Before running it:

- turn on the phone's Mobile Hotspot and join it from the laptop;
- connect Tailscale on the laptop and Ultra96, with `TAILSCALE_BOARD_HOST` set in `Comms/.env`;
- NOTE: disconnect Cisco AnyConnect;
- install Mosquitto in its default Windows path and stop its Windows service if it already holds
  port 1883; and
- build `Comms/phone/demo-visualiser` in Unity 6000.6.0f1 for Android and install it on the
  hotspot phone. In the app, enter the broker address the launcher prints and press **Connect**;
- add a Windows firewall rule so the phone can reach the laptop broker. In an elevated
  PowerShell, once:
  `New-NetFirewallRule -DisplayName "B16 MQTT 1883 local subnet" -Direction Inbound -Protocol TCP -LocalPort 1883 -RemoteAddress LocalSubnet -Profile Any -Action Allow`.
  The launcher warns if it is missing.

The laptop launcher validates the network, starts a temporary Mosquitto instance on loopback and
the hotspot lease, opens the Tailscale SSH tunnel, prints the broker address for the phone, and
cleans up the broker and tunnel when the relay exits.

#### Terminal 1: laptop broker and relay

```powershell
cd <path-to-team>\Comms
.\scripts\run_pipeline_a_hotspot.ps1 --nodes 1,2
```

#### Terminal 2: board client

```powershell
cd <path-to-team>\Comms
.\scripts\run_board_a_hotspot.ps1 --sync
```

After the first synchronized run, omit `--sync`.

Never mix the pairs:

| Setup | Laptop | Ultra96 |
|---|---|---|
| Final system | `run_pipeline.ps1` | `run_board.ps1` / `tcp_client_mqtt.py` |
| Laptop broker | `run_pipeline_a_hotspot.ps1` | `run_board_a_hotspot.ps1` / `tcp_client.py` |

A mixed pair can leave every process apparently healthy while the phone receives no event.

### Mutual TLS: built, not used

The final system does not use TLS on MQTT. The broker listens only on the Ultra96's loopback,
and the phones reach it through their SSH tunnels, so SSH already encrypts and authenticates
every MQTT byte.

Mutual TLS was built for an earlier laptop-broker setup, in which the phone joined the laptop's
own Wi-Fi access point at `192.168.137.1`. `laptop/make_certs.ps1` issues the certificates,
`laptop/verify_certs.ps1` checks them, and `--tls` on `mqtt_client.py`, `pipeline.py` and
`mqtt_ping.py` connects to the broker's 8883 listener. It was tested on 17 September on the
laptop only: the broker accepted a valid client certificate and refused both a missing one and
one from another CA, the relay published over TLS, and the test visualiser received those
messages in the Unity Editor. It never ran on a phone or with the Ultra96.

The laptop-broker setup above runs plaintext and cannot use it (see
`laptop/mosquitto_hotspot.conf`). The comparison that chose the Ultra96 broker therefore ran without
TLS. TLS only adds work, so adding it to the laptop-hosted broker could not have reversed that choice.
mTLS has not been run since 17 September, so treat `--tls` as unmaintained.
