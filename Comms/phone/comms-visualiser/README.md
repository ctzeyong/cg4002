# Comms drop-in for the Vis app

1. **`Transport/`** connects your app to the MQTT broker on the Ultra96 (configuration B).
   It runs a two-hop SSH tunnel from the phone and supervises it, then calls **your**
   `MqttConnection.Connect()` through it. Your client is not changed.
2. **`Hook/`** makes an AI event from the board act like a stroke drawn on that player's
   phone. It calls `GesturePad.PerformGesture`, so mana, aim and cooldowns work exactly as
   for touch.

Comms owns this folder. Copy it into your project. Don't edit it there: tell Comms, and the
fix goes into `Comms/phone/comms-visualiser/`. The design is in Comms' notes,
`vis_integration.md` §4.

```
comms-visualiser/
  Shared/MqttConfig.cs        topic names, ports, backoff (both halves use it)
  Transport/                  configuration B only: delete it for fallback A
    CommsTunnelBridge.cs      the tunnel, the reconnect, the on-screen form and log
    SshMqttTunnel.cs, TunnelConfig.cs, link.xml, Plugins/SshNet/*.dll (5)
  Hook/                       works with any broker
    AiGestureHook.cs          subscribes, maps the name onto GestureSymbol, calls PerformGesture
    GestureEventParser.cs, Envelope.cs, SeenEvents.cs
    GestureEcho.cs            the latency echo (§5)
```

**The order:** import (§1) → wire the scene (§2) → build to each iPhone → per-phone setup (§3)
→ bring-up (§4) → the tests, V1a → V1b → V2 (§7). §5 and §6 are reference for when something
looks wrong.

## 1. Import (once, on your Mac)

1. Copy the whole `comms-visualiser` folder into `Assets/`.
2. **Package Manager → + → Add package by name:** `com.unity.nuget.newtonsoft-json`, if it
   isn't there already.
3. **Your edit:** add `PerformGesture` to `GesturePad.cs` (below). Until you do, the hook
   doesn't compile, and because the folder shares your scripts' assembly, nothing else in
   the project does either.
4. **Don't add** `System.Memory.dll` or `System.Buffers.dll`. Unity already has them, and a
   second copy causes duplicate-type errors. No `.asmdef` either: the folder compiles into
   the default assembly next to your scripts.
5. **Build to each iPhone as usual.** If M2Mqtt fails to load, set **Player Settings → Other
   Settings → Api Compatibility Level** to **.NET Framework**. `Transport/link.xml` stops
   IL2CPP stripping SSH.NET, so leave it where it is.

### The one code change, in `GesturePad.cs`

`TryPerformAction` is private, so add a public entry point that reuses your feedback label:

```csharp
public bool PerformGesture(GestureSymbol symbol, string source)
{
    bool ok = TryPerformAction(symbol);
    ShowFeedback(ok ? $"{symbol} ({source}) accepted"
                    : $"{symbol} ({source}) rejected - no match, aim or mana", ok ? acceptedColor : rejectedColor);
    return ok;
}
```

On a client phone this already does the right thing: `SpawnUnit` sends the request to the
host with this phone's aim.

## 2. Scene wiring

- **`MqttConnection`:** untick **Connect On Start**. Leave the broker at `127.0.0.1`, port
  `1883`. That's the phone's end of the tunnel.
- **A new GameObject** (any name) with two components:
  - `CommsTunnelBridge` → **Mqtt Connection:** your `MqttConnection`.
  - `AiGestureHook` → **Mqtt Connection**, **Match Setup**, **Gesture Pad:** yours.
- [Not used for now] **Fallback A** (broker on the laptop, no SSH): delete `Transport/`, keep only
  `AiGestureHook`, set `MqttConnection`'s broker to the laptop's address and tick Connect
  On Start again. The hook doesn't change.

## 3. Per-phone setup (each iPhone, once)

Comms sends the real values privately. They aren't written anywhere in the repo.

- **NUS Cisco AnyConnect VPN connected,** on every network, campus Wi-Fi included. The jump host accepts a
  password only from inside the VPN.
- In the bridge's form at the top of the screen:

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
2. Wait for **`MQTT_CONNECTED`** on the screen. The form then collapses to one status line
   (**Show** opens the log again).
3. **Both phones show `MQTT_CONNECTED` before anyone locks in or presses start.** `ready`
   and `lock` aren't retained, so a phone that connects late never hears them. And a start
   pressed while MQTT is down runs that phone's countdown alone.
4. Choose **VsPlayer** and **select the player who matches your glove:** glove N → "Player N".
   Player 1's phone is the host, where the match runs.
   The hook listens on `game/gesture/N` for the player you selected. Choose wrong, and your
   glove's gestures act on your opponent's phone.
5. **Keep the crosshair on your own half** when you gesture. Spawns need valid aim, just
   like a drawn stroke. Spells don't.

## 5. What you see

- **The bridge's log** (top of the screen, last 15 lines, hosts and username masked). The
  full lines go to Xcode's console with the tag `[comms-bridge]`: run the app from Xcode and
  read the debug area at the bottom. Those lines are **not** masked, so they are for Comms
  only, sent privately.
  `JUMP_CONNECTING` → `JUMP_CONNECTED` → `BOARD_CONNECTED` → `PAYLOAD_FORWARD_READY` →
  `BROKER_OK` → `MQTT_CONNECTING` → `MQTT_CONNECTED`.
- **The hook's line** (just under it, for 5 s, tag `[ai-hook]` in Xcode):
  `listening on game/gesture/1` the first time a player is picked, `now acting on
  game/gesture/2` on each later switch, `no player selected: events are ignored` after a
  debug reset, or why an event was dropped (`unknown gesture 'square'`,
  `duplicate … ignored`, `ignored game/gesture/2: this phone listens on game/gesture/1`).
  It also shows each change in the gloves' status from the board (`status/gloves`), for
  example `gloves: player 1 ready, player 2 down` once the phone connects, then
  `gloves: player 2 down` if that glove stops streaming mid-match. `offline` means the
  board's client is not running. The hook subscribes to this topic by itself, so there is
  nothing to wire.
- **The latency echo** (not shown): once a player is selected, the hook answers every gesture
  on `viz/echo/{player}` so the board can time the phone hop. Nothing to wire. An `echo: …`
  line means a gesture had no `seq` or `timestamp`.
- **Your own feedback label:** `Circle (glove) accepted`, or `rejected - no match, aim or
  mana`.
- **A note on switching players:** after a switch, the phone stays subscribed to the old
  topic too, since `MqttConnection` has no unsubscribe. The hook drops those events with an
  `ignored …` line, so this is expected.

### If it doesn't connect

| The log shows | Meaning | Next |
|---|---|---|
| `TUNNEL_FAILED: … No authentication method configured` | The hop 1 password field was empty | Type it, Connect. It isn't saved |
| Stuck at `JUMP_CONNECTING`, then a timeout | No route to the jump host | Is the VPN connected? |
| An authentication failure on hop 1 | NUS credentials | Retype them |
| `… host key SHA256:… REJECTED` | Fingerprint pasted wrong, or the key really changed | Paste again. If it still fails, ask Comms before trusting a new key |
| Key-exchange / "algorithm negotiation" error, `TypeLoadException`, `MissingMethodException` | IL2CPP stripped SSH.NET, or a DLL is missing | Check `Transport/link.xml` and `Transport/Plugins/SshNet/` are under `Assets/`. Send Comms the stack from Xcode |
| Any other exception before `JUMP_CONNECTED` | SSH.NET on iOS, the one untested part | Send Comms the Xcode stack |
| `PAYLOAD_FORWARD_READY`, then `NO_BROKER` repeating | The tunnel is fine; the board's broker isn't answering | Tell Comms |
| `BROKER_OK`, `MQTT_CONNECTING`, then `MQTT_CONNECT_FAILED` | The broker answers, your client doesn't connect | Broker must be `127.0.0.1:1883`. Look for `MqttConnection:` errors in Xcode |
| `RESUMED after Ns: rebuilding the nest` | The app was in the background | Normal. Wait for `MQTT_CONNECTED` |

## 6. Two debug-menu caveats

- **Its Connect button** is safe before the tunnel is up (the connect is refused). With the
  tunnel up and the broker down, it leaks a spinning M2Mqtt thread until the app restarts.
  The bridge probes the broker first; the button can't. Avoid it while the bridge runs.
- **Its Disconnect button** doesn't stick: the bridge reconnects within a backoff step.

## 7. Testing, in order

The three stages need only the board switched on (Mosquitto starts on boot). The exception is
the last step of V2, which Comms runs with you.

### Reaching the board

From your Mac, with the VPN on:

```bash
ssh -J <nusnet-id>@<jump-host> xilinx@<board-host>
```

`<jump-host>` is the value pre-filled in the form's Host field. The board host and both
passwords are in Comms' private message. Everything below only runs MQTT clients, so nothing
on the board changes.

For every new SSH session you reconnect, paste this helper onto the board: `ev <gesture> [player] [event_id]` publishes one
hand-made AI event, shaped like the board's real ones. Leave out `event_id` and each call gets
a fresh one, so repeats aren't deduped.

```bash
ev() { p=${2:-1}; mosquitto_pub -h 127.0.0.1 -q 1 -t "game/gesture/$p" -m "{\"event_id\":\"${3:-manual:$(date +%s%N)}\",\"source\":\"manual\",\"seq\":0,\"ts\":$(date +%s).0,\"payload\":{\"type\":\"AI_EVENT\",\"player\":$p,\"seq\":0,\"gesture\":\"$1\",\"timestamp\":$(date +%s).0}}"; }
```

The gesture names are the classifier's: `warrior mage fireball rogue lightning hero shield`.
Each is a member of your `GestureSymbol` enum. `shield` is the one your enum maps to Tank.

### V1a — one iPhone connects

1. Bring up one phone (§4, steps 1–2). You should see `MQTT_CONNECTED`. If not, see the table in §5.
2. **Phone → board:** on the board, run `mosquitto_sub -h 127.0.0.1 -t 'debug/#' -v`.
   You should see about one `debug/heartbeat` line a second, carrying your device name. Your
   `DebugMenu` sends these on its own whenever MQTT is connected, at its
   **Mqtt Heartbeat Interval Seconds** (default 1). Ctrl-C to stop.
3. **Board → phone:** select Player 1, then run `ev warrior`. Your feedback banner should show
   `glove -> Warrior`. Outside a match it turns red and says why. That's fine: it proves the
   event arrived.

### V1b — both iPhones

1. Bring up both phones. **Both must show `MQTT_CONNECTED` before either selects or presses
   start** (§4, step 3).
2. VsPlayer: phone 1 selects Player 1, phone 2 selects Player 2. Ready, lock and countdown
   should stay in sync. A match runs, and phone 2's units and tower HP follow phone 1's.
3. Mid-match, turn the VPN off on one phone for about 10 s, then on again.
   - You should see `MQTT_LOST (nest down)`, then `MQTT_CONNECTED` again on its own,
     without a relaunch.
   - Note what the match did meanwhile. Don't fix it; just tell Comms.
   - If the log shows "address already in use", it clears within about 30 s as the retries
     continue.
4. Say whether the match feels laggy through the tunnel.

### V2 — AI events act like drawn strokes

Run these in a VsPlayer match, with each crosshair on its own half. Send one at a time and
wait for mana: a red banner for mana is not a failure.

| On the board, run this | Expected |
|---|---|
| `ev warrior 1` | A Warrior spawns on phone 1's side, visible on both phones |
| `ev warrior 2` | Phone 2 sends its request, and the enemy Warrior shows on both phones |
| `ev rogue 1`, `ev mage 1`, `ev shield 1`, `ev hero 1` | Rogue, Mage, Tank, Hero |
| `ev lightning 1`, `ev fireball 1` | Lightning Strike, Fireball |
| `ev hero 2`, watching phone 1 | Phone 1 does nothing. Phone 2 acts |
| `ev warrior 1 rep:1`, then the same again | One spawn. The second shows `duplicate rep:1 ignored`. To repeat this test, use a new id (`rep:2`, …) |
| `ev warrior 1` with the aim off your own half, or outside a match | A red banner saying why, and nothing else happens |
| `ev circle 1`, `ev 3 1` | An `unknown gesture` line, and nothing else happens |

Then hold each phone the way it will be held on Thursday: **most AI spawns must be accepted,
not refused for aim.**

**Through the FireBeetles (Comms runs it; remote is fine).** This needs no gestures and no
IMU. Comms has both FireBeetles next to the laptop, because BLE reaches only a few metres. The
laptop and board pipeline run, and Comms presses keys on the laptop. Your phones stay wherever
you are, on the VPN, as for the tests above, so a call is enough to say what was pressed. Each
key travels to a FireBeetle and back, then to the board, which publishes the event.
- A letter goes to both players, and both phones act. `w m f r l h s` pick warrior, mage,
  fireball, rogue, lightning, hero and shield; any other letter picks one at random.
- `1` or `2` (a random gesture) goes to that player only, and only that phone acts.

### What to send Comms afterwards

- Each check's result. For a failure, add the on-screen log lines around it.
- For any failure, the Xcode console lines tagged `[comms-bridge]` and `[ai-hook]`. They hold
  real hostnames, so send them privately. Never commit them or post them in a shared channel.
