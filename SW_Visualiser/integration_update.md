# Integration update: bubble capture (the first-person scale described below was reverted on 2026-10-20: tower and units are back to the small scale, the summon band is gone, and there is one bubble at a time)

Written for: the SW Visualiser owner (and teammates who need to know what the comms side must handle).

The live Unity project is `My project/` (its `Assets/Script/` was identical to `SW_Visualiser/script/` before this work). Changes were made in `My project/Assets/Script/` and mirrored into `SW_Visualiser/script/`, so the git diff and this README track them. `My project/` is excluded from git (`.git/info/exclude`). Nothing is committed.

**Status: written but not compiled or run.** Unity was not open, so none of it has been through the compiler or tested on a device. Expect to fix a typo or two on first open.

---

## 1. How the visualiser integrates with the comms side

### Today (already in the repo)

- **Transport.** `MqttConnection` (MQTTnet) wraps every message in the comms five-field envelope (`event_id`, `source`, `seq`, `ts`, `payload`), de-duplicates incoming messages on `event_id`, and reconnects automatically. The comms team's `Envelope.cs`, `SeenEvents.cs` and `MqttConfig.cs` live in `script/Networking/Comms/`. They are copied unchanged and must not be edited here; fix them in the comms repo and copy across. `script/Networking/Comms/CommsVisualiser/` (`Transport/` SSH tunnel to the Ultra96 broker, `Hook/` AI gesture to `GesturePad.PerformGesture`) comes from `Comms/phone/comms-visualiser` the same way. Not compiled or tested on a device.
- **Glove input, Ultra96 to phone.** The classifier's event arrives on `game/gesture/{player}`. `GloveGestureReceiver` builds itself at startup, subscribes to the local player's topic, and maps the gesture through `GestureBindings` to the same game action the touch pad triggers.
- **Phone to pipeline.** `viz/ready/{player}` is sent when the arena frame is locked (`PeerReadySync`). This is the pattern the new bubble topic copies.
- **Phone to phone.** `bridgerush/ready`, `bridgerush/lock`, `bridgerush/match/countdown` and `bridgerush/game/request` keep the two phones in step. They are not in the comms contract.
- **Game state.** Today the Player 1 phone is the host and runs `MatchState` (mana, units, combat on ticks), publishing `game/state` every tick. The comms notes assume the engine is on the laptop or Ultra96 and the phones only render. If that moves, `MatchState` moves and `game/state` becomes the engine's output.

### New in this update

| Topic | Direction | Payload | Status |
|---|---|---|---|
| `viz/bubble_captured/{player}` | phone to Ultra96 | `{"event":"bubble_captured","player":1,"bubble_id":2}` (QoS 1) | **Proposed.** Not in the contract yet. |
| `bridgerush/bubble/player{N}` | phone to opponent's phone | `{"targeted":true,"progress":0.42}` (QoS 0, about 5 Hz while it changes) | Stub. Phone-to-phone only. |

The phone only reports the bubble id. It does not send the reward, and it does not add mana. The Ultra96 owns the reward table.

### What the comms side must handle (also a TODO in `BubbleSync.cs`)

1. Subscribe to `viz/bubble_captured/+` and agree the topic name in `comms/notes/mqtt_topics.md`.
2. Validate: the match is running, the player is real, optionally rate-limit per `bubble_id` against the respawn cooldown. QoS 1 can deliver twice; the phone de-duplicates on `event_id`, the board should too.
3. Award that player's reward in the authoritative state (1 mana for now, a buff later).
4. Publish the result back so the phones show it. **Until the board feeds mana back, nothing changes on screen**, because mana is currently shown from the host phone's own snapshot. For offline testing use the mock toggle (section 4).

---

## 2. The original plan

1. **Scale the world to first person.** Opponent tower 1.65 m (one field), units 40% of player height (one field), a configurable tag-to-ground offset so the tower does not float, a slim semi-transparent tower, and units crossing the longer lane at the same game speed.
2. **Fix the spawn distance.** Summoning area roughly 1.5 to 2.5 m in front, cursor clamped to at least 1 m, check the camera near clip.
3. **Bubble capture (main feature).** Bubbles on the opponent's tower at different heights, parented to the tag anchor. Crosshair fill, slow drain with a 0.3 s grace window, configurable hold time (about 2.5 s), counted only while the tag is actually tracked, with a max lock distance and viewing angle. `OnBubbleCaptured(bubbleId)`, a reward type (mana now, buff later), report to the Ultra96 only, cooldown then respawn elsewhere, lock progress streamed to the opponent with a warning indicator, simple ring visuals.

Rules followed: all tunables are serialized fields, a mock mode is a toggle (default off), unrelated code is not refactored, no commits.

Defaults I chose where the plan left a question open: tower base is the tag height minus the offset (floor only as a fallback); own tower stays a small marker; the summon area's near edge is enforced in the cursor rather than by changing the territory in `MatchState`.

---

## 3. What changed

### Edited files

| File | Change |
|---|---|
| `Tracking/ArenaFrame.cs` | Exposes `HasOpponentPose`, `OpponentTagPosition` (smoothed 3D tag position, previously flattened away) and `IsFakeOpponent`. |
| `Gameplay/Towers/TowerController.cs` | New fields `opponentTowerHeightMeters` (1.65), `tagToGroundOffsetMeters`, `opponentTowerWidthMultiplier` (0.25), `opponentTowerTransparent`, `opponentTowerAlpha`. A `TagAnchor` object follows the tag; the opponent tower is its child, hung below by the offset. VS CPU's fake opponent stands on the floor, because its height is the camera's. `towerHeight` is now the own-tower marker only. `TowerRadius` is the larger of the two footprints. |
| `Gameplay/GameStateRenderer.cs` | `unitTargetHeightMeters` replaced by `unitHeightRatio` (0.4) times the opponent tower height. |
| `Input/SummonCursor.cs` | `minSpawnDistanceMeters` (1.0, hard clamp), `summonAreaNearMeters` (1.5) and `summonAreaFarMeters` (2.5, 0 = no limit). Aim is valid only inside that band and the territory. |
| `Match/MatchState.Economy.cs` | `MockAwardMana`, used only by the offline mock. |

### New files

| File | Purpose |
|---|---|
| `Gameplay/Bubbles/BubbleCaptureManager.cs` | Raycast from the camera centre, progress, grace, drain, gating (tag tracked, distance, angle), capture, cooldown, respawn on a different slot. Raises `OnBubbleCaptured(int)`. |
| `Gameplay/Bubbles/CaptureBubble.cs` | One bubble: sphere trigger collider, translucent orb, and a camera-facing progress ring. Colour changes from idle to locking to full. |
| `Gameplay/Bubbles/BubbleReward.cs` | Reward as a type (`Mana`, `Buff`) with amount and buff id. |
| `Networking/Sync/BubbleSync.cs` | Reports captures, streams and receives lock progress, and holds the mock toggle. |
| `UI/BubbleWarningUI.cs` | Defender's warning: a compact banner ("ENEMY LOCKING ON", hint, progress bar) that fades in. Builds itself on the canvas. |

### Findings that did not need a change

- **Unit speed already scales with lane length.** `SimulateTick` uses `totalLaneLength / secondsToCrossLane`, so units cross a longer lane in the same time. No change was made.
- **Camera clip planes.** The AR camera (XR Origin prefab) has near clip 0.1 m and far clip 20 m, and the scene does not override them. A 0.66 m unit at 1 m or more will not clip.

### Known gaps, to retune by hand (not changed)

These distances were tuned for 0.15 m units and live as serialized fields on `MatchState`: `engageRangeMeters` (0.8), `towerClearanceMeters` (0.07), `towerDisengageHysteresisMeters` (0.05), `spawnForwardOffsetMeters` (0.084), `lookAheadMeters` (0.1). With 0.66 m units, expect to raise `towerClearanceMeters` first, or units will stand inside the tower.

---

## 4. Unity editor setup

1. **Open the project** and let Unity import the new scripts (it generates their `.meta` files). Fix any compile errors in the Console first.
2. **Layer (optional but recommended).** Project Settings > Tags and Layers > add a layer named `Bubble` (for example index 6). Put that index in `Bubble Layer` on `BubbleCaptureManager`. Left at -1 the bubbles use Default and the raycast ignores everything else anyway.
3. **TowerController** (existing object in the scene): check `Opponent Tower Height Meters` 1.65, `Tag To Ground Offset Meters` (set to your tag's height above the floor, about 1.1 for a chest tag), and the width and transparency fields. Leave `Tower Height` as the own marker size.
4. **GameStateRenderer:** `Unit Height Ratio` 0.4. Leave `Tower Controller` empty (it finds one) or assign it.
5. **SummonCursor:** check `Min Spawn Distance` 1, `Summon Area Near` 1.5, `Far` 2.5.
6. **New object `Bubbles`** (empty GameObject). Add `BubbleCaptureManager` and assign: Aim Camera (the AR camera), Arena Frame, Match State, Tower Controller. Tune the slot arrays if you want different heights.
7. On the same object add `BubbleSync` and assign: Mqtt Connection, Bubble Capture Manager, Match State, Match Setup.
8. **BubbleWarningUI:** add it to the same object (or any), assign Bubble Sync and the Canvas.
9. **Test offline:** tick `Mock Ultra96 Offline` on `BubbleSync`, pick VS CPU, start a match, and look at a bubble for 2.5 s. Mana should rise by 1 and the red warning should show for your own lock. In the editor without an AR device, move the camera by hand or temporarily lower `Max Viewing Angle`/raise `Max Lock Distance` if needed. **Turn the mock off for a real match.**
10. **Test over MQTT:** with the mock off and a broker running, subscribe to `viz/bubble_captured/+` (for example with `mosquitto_sub`) and watch the capture arrive. Subscribe to `bridgerush/bubble/#` to watch the progress stream.
11. **Real two-phone test:** the defender's phone should show the warning while the attacker holds the crosshair on a bubble, and it should clear about 1.5 s after the attacker stops.

### Tune first

1. `tagToGroundOffsetMeters`: wrong value makes the tower float or sink.
2. `holdTimeSeconds` (2.5) and `drainPerSecond` (0.2): the feel of the capture.
3. `maxLockDistanceMeters` and `maxViewingAngleDegrees`: how forgiving it is in your room.
4. `graceSeconds` (0.3): raise it if head jitter keeps breaking the lock.
5. `towerClearanceMeters`, `engageRangeMeters`: so the bigger units stop at the tower and meet each other properly.
6. `respawnCooldownSeconds` (8) and `bubbleCount` (4).
7. `opponentTowerWidthMultiplier` and `opponentTowerAlpha`: how much of the real opponent shows through.

---

## 5. Open items

- Agree the `viz/bubble_captured/{player}` topic and payload with the comms team, and have the Ultra96 award the mana and feed it back.
- Decide who runs the game logic (still the Player 1 phone today); the bubble reward only reaches the screen once that path exists.
- The `bridgerush/bubble/player{N}` stream is a stub and should be added to the topic contract or moved onto a contracted topic.
- The mock mode awards mana only on the host or local phone; a client phone's mana is overwritten by the host's snapshots.
- Not compiled and not tested on a device.
