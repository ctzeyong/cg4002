# Comms drop-in for the Vis app

1. **`Transport/`** connects your app to the MQTT broker on the Ultra96.
   It runs a two-hop SSH tunnel from the phone and supervises it, and rebuilds it when nothing
   answers through it. It calls **your** `MqttConnection.Connect()` through it. Your client is
   not changed.
2. **`Hook/`** shows the gloves' status from the board and echoes each gesture back for
   latency. It doesn't act on gestures. Your `GloveGestureReceiver` does that, through
   `GestureBindings`.

Comms owns this folder. Copy it into your project. Don't edit it there: tell Comms, and the
fix goes into `Comms/phone/comms-visualiser/`.

```
comms-visualiser/
  Shared/MqttConfig.cs        topic names, ports, backoff (both halves use it)
  Transport/                  the SSH tunnel: delete it if the broker is on the laptop
    CommsTunnelBridge.cs      the tunnel and its reconnects, and the form and log your debug
                              menu draws with DrawDetails()
    SshMqttTunnel.cs, TunnelConfig.cs, link.xml, Plugins/SshNet/*.dll (5)
  Hook/                       works with any broker
    AiGestureHook.cs          the gloves' status line and the latency echo
    GloveStatusParser.cs      reads status/gloves
    GestureEcho.cs            the latency echo (§5)
    Envelope.cs, SeenEvents.cs
```

**The order:** import (§1) → wire the scene (§2) → build to each iPhone → per-phone setup (§3)
→ bring-up (§4). §5 and §6 are reference for when something looks wrong.

## 1. Import (once, on your Mac)

1. Copy the whole `comms-visualiser` folder into `Assets/`.
2. **Package Manager → + → Add package by name:** `com.unity.nuget.newtonsoft-json`, if it
   isn't there already.
3. **Don't add** `System.Memory.dll` or `System.Buffers.dll`. Unity already has them, and a
   second copy causes duplicate-type errors. No `.asmdef` either: the folder compiles into
   the default assembly next to your scripts.
4. **Build to each iPhone as usual.** If M2Mqtt fails to load, set **Player Settings → Other
   Settings → Api Compatibility Level** to **.NET Framework**. `Transport/link.xml` stops
   IL2CPP stripping SSH.NET, so leave it where it is.

## 2. Scene wiring

- **`MqttConnection`:** untick **Connect On Start**. Leave the broker at `127.0.0.1`, port
  `1883`. That's the phone's end of the tunnel.
- **A new GameObject** (any name) with two components:
  - `CommsTunnelBridge` → **Mqtt Connection:** your `MqttConnection`.
  - `AiGestureHook` → **Mqtt Connection**, **Match Setup:** yours.
- **`GloveGestureReceiver`** builds itself at startup, so it needs no wiring.
- **If the broker is on the laptop** (the alternative setup, not used): delete `Transport/`, keep only
  `AiGestureHook`, set `MqttConnection`'s broker to the laptop's address and tick Connect
  On Start again. The hook doesn't change.

## 3. Per-phone setup (each iPhone, once)

Comms sends the real values privately. They aren't written anywhere in the repo.

- **NUS Cisco AnyConnect VPN connected,** on every network, campus Wi-Fi included. The jump host accepts a
  password only from inside the VPN.
- In the bridge's form, which your debug menu draws:

  | Field | Value | Saved? |
  |---|---|---|
  | Host (hop 1) | pre-filled jump host | yes |
  | NUS username | `<nusnet-id>` | yes |
  | Password (hop 1) | your NUS password | **no**: typed every launch |
  | Jump host SHA256 | copy from the private message, press **Paste** | yes |
  | Board host | `<board-host>` | yes |
  | Username / Password (hop 2) | the board's | yes |
  | Board SHA256 | copy from message and **Paste** | yes |
  | Board broker port | `1883` | yes |

- **Keep the app in front.** The bridge stops the screen from auto-locking while the app runs
  (players wearing gloves don't touch it), but switching apps still suspends it, and iOS closes
  a background app's sockets. The bridge rebuilds the tunnel when the app comes back, but a running match may
  not survive it.

## 4. Bring-up, every launch

1. VPN on → open the app → type the NUS password → **Connect**.
2. Wait for **`MQTT_CONNECTED`** in the bridge's log. The form hides while the bridge runs.
3. **Both phones show `MQTT_CONNECTED` before anyone locks in or presses start.**
4. Choose **VS PLAYER** and **select the player who matches your glove:** glove N → "Player N".
   The match runs on the board's game engine.
   `GloveGestureReceiver` listens on `game/gesture/N` for the player you selected. Choose
   wrong, and your glove's gestures act on your opponent's phone.
5. **Keep the crosshair on your own half** when you gesture. Spawns need valid aim, just
   like a drawn stroke. Spells don't.

## 5. What you see

- **The bridge's log** (in the debug menu, last 15 lines, hosts and username masked). The
  full lines go to Xcode's console with the tag `[comms-bridge]`: run the app from Xcode and
  read the debug area at the bottom. Those lines are **not** masked, so they are for Comms
  only, sent privately.
  `JUMP_CONNECTING` → `JUMP_CONNECTED` → `BOARD_CONNECTED` → `PAYLOAD_FORWARD_READY` →
  `BROKER_OK` → `MQTT_CONNECTING` → `MQTT_CONNECTED`.
- **The hook's line** (just under it, for 5 s, tag `[ai-hook]` in Xcode): each change in the
  gloves' status from the board (`status/gloves`), for example
  `gloves: player 1 ready, player 2 down` once the phone connects, then
  `gloves: player 2 down` if that glove stops streaming mid-match. `offline` means the
  board's client is not running. The hook subscribes to this topic by itself, so there is
  nothing to wire.
- **The latency echo** (not shown): once a player is selected, the hook answers every gesture
  on `viz/echo/{player}` so the board can time the phone hop. Nothing to wire. An `echo: …`
  line means a gesture had no `seq` or `timestamp`. An echo only shows that the phone got the
  gesture, not that the game acted on it.
- **Gestures** show the same way as a card tap: green, or red with the reason. In Xcode: `GloveGestureReceiver: 'warrior' -> SpawnWarrior performed`.
- **A name the game doesn't know** shows nothing on screen. Xcode shows
  `GloveGestureReceiver: no action bound to glove gesture '<name>'`.
- **A repeated `event_id`** is dropped by `MqttConnection` before any handler sees it. Nothing
  shows; only its `Duplicates` count goes up.

### If it doesn't connect

| The log shows | Meaning | Next |
|---|---|---|
| `TUNNEL_FAILED: … No authentication method configured` | The hop 1 password field was empty | Type it, Connect. It isn't saved |
| Stuck at `JUMP_CONNECTING`, then a timeout | No route to the jump host | Is the VPN connected? |
| An authentication failure on hop 1 | NUS credentials | Retype them |
| `… host key SHA256:… REJECTED` | Fingerprint pasted wrong, or the key really changed | Paste again. If it still fails, ask Comms before trusting a new key |
| Key-exchange / "algorithm negotiation" error, `TypeLoadException`, `MissingMethodException` | IL2CPP stripped SSH.NET, or a DLL is missing | Check `Transport/link.xml` and `Transport/Plugins/SshNet/` are under `Assets/`. Send Comms the stack from Xcode |
| Any other exception before `JUMP_CONNECTED` | An SSH.NET failure on iOS | Send Comms the Xcode stack |
| `NO_BROKER` twice, then `NEST_REBUILD` and the tunnel building again, over and over | The tunnel comes up but nothing answers through it. Either the board's broker is down or the tunnel is broken | Tell Comms |
| `BROKER_OK`, `MQTT_CONNECTING`, then `MQTT_CONNECT_FAILED` | The broker answers, your client doesn't connect | Broker must be `127.0.0.1:1883`. Look for `MqttConnection:` errors in Xcode |
| `RESUMED after Ns: rebuilding the nest` | The app was in the background | Normal. Wait for `MQTT_CONNECTED` |

## 6. The debug menu

Its Comms section shows the bridge's status, the form and the log. Its **Connect** /
**Disconnect** button starts and stops the bridge.
