# SW_Visualiser

The software visualiser for BridgeRush: a Unity AR app that renders the match on a real table. Two phones each track an AprilTag marker on the opponent's side, build a shared arena between the two devices, and draw towers, a bridge and units in AR. Players summon units by drawing gestures on the screen.

This folder holds the scripts I wrote for it (`script/`). Art packs, Unity `Library/` output and builds are not included.

- **AR arena tracking**: detect the opponent's AprilTag with `jp.keijiro.apriltag`, smooth the pose, hold it when the marker is briefly lost, and build a lane axis between the two devices. Towers, bridge and water are placed along that axis, with the floor height taken from AR plane detection.
- **Gesture input**: the real input is the glove/arm, classified on the Ultra96 and delivered over MQTT (`GloveGestureReceiver`; `AiGestureHook` only shows glove status and echoes gestures for latency), through one table, `GestureBindings`. The on-screen drawing pad was removed on 2026-10-21; a banner shows what each gesture did and why it failed. A summon cursor chooses where a unit spawns.
- **Match logic**: a mana economy, unit spawning, lane movement, melee combat, tower damage and counter-damage, out-of-bounds damage, an optional enemy bot, and a 180 s match timer. Unit stats and prefabs are data-driven through ScriptableObjects.
- **Networking**: an MQTT connection that follows the comms team's contract (MQTTnet, five-field envelope, de-duplication on `event_id`, automatic reconnect), and sync so both phones agree on ready state, tower lock, countdown and game state.
- **Rendering and visuals**: unit and tower views, bridge, water and tower pedestal visuals, and a renderer that draws the match state in AR.
- **Scale**: sized for a 2 m lane. The opponent tower matches your own tower's size, hanging below the worn tag by a tag-to-ground offset; units are 0.15 m; a spawn is valid anywhere inside your own territory.
- **Bubble capture**: capture bubbles sit on the opponent's tower. Holding the crosshair on one fills it; a full bubble reports `bubble_captured` to the Ultra96, which awards the mana. The defender's phone shows a warning while they are being locked. See `integration_update.md`.
- **UI**: match HUD, unit card hand, setup screen, a short result banner under the top bar, a settings window (gear button, top right: Haptics, Music, Sound, HOW TO PLAY pages, the DEBUG menu, Quit to main menu) and a tracking overlay for testing without a second player. The tutorial coach shows a looping animation of each motion and points at the card; after the last step it starts a fresh VS CPU match.

## How a match works

1. **Setup**: each phone connects to the MQTT broker, scans the floor, and marks itself ready.
2. **Tracking**: the phone detects the opponent's AprilTag and builds the lane between the two devices.
3. **Countdown**: once both players are ready, a synced countdown starts the match.
4. **Play**: mana regenerates over time. The player makes a gesture with the glove (or draws one on screen when testing), aims with the summon cursor, and the matching unit spawns and walks the lane. Units fight on contact and towers hit back.
5. **End**: the match runs on a timer (180 s by default) and tower HP decides the result.

## Script layout

| Folder | What it does |
| --- | --- |
| `script/Tracking/` | `AprilTagTracker` detects markers. `ArenaFrame` turns the device pose and opponent marker into a lane axis and world positions. `FloorHeightProvider`, `MatchSetup` and `TrackingDebugOverlay` support placement and debugging. |
| `script/Input/` | `GestureBindings` maps a gesture to a game action. `GesturePad` shows the result of glove gestures (run through `GloveGestureReceiver`) and the reason one failed; the on-screen drawing pad and shape recogniser were removed on 2026-10-21. `SummonCursor` picks the spawn point. `ShakeDetector` and `TowerShakeInput` lock/unlock the own tower when the phone is shaken (the Lock Tower button was removed). |
| `script/Match/` | `MatchState` is the core game logic, split into `.Economy` (mana), `.Combat`, `.Geometry` (lane, two bridges, bounds), `.Spells` (lightning strikes and the shared spell numbers) and `.Network`. `UnitState`, `UnitStatsTable` and `UnitPrefabCatalog` define the units (Warrior, Rogue, Mage, Tank, Hero). |
| `script/Networking/` | `MqttConnection` owns the broker connection and the envelope. `Glove/GloveGestureReceiver` turns glove events into actions. `Sync/` (`MatchNetworkSync`, `PeerReadySync`, `TowerLockSync`, `MatchCountdown`) keeps both phones in step. `Sync/BubbleSync` reports captures and streams lock progress. `NetworkMessages` holds the payload types. `Engine/` (`EngineLink`, `EngineLinkCore`, `EngineMessages`) is the phone's end of the Ultra96 game engine: it streams the tower position and crosshair on `viz/lane/{player}` (and reads the other player's `viz/lane` to draw their tower where they report it), sends taps and START on `game/request/{player}`, and checks `game/match`, `game/event` and `game/state`. It is still being tested and does nothing until `EngineLink.Activate()`, which `MatchNetworkSync` calls for every VS PLAYER match when `EngineLink` is wired (the board-client role, `MatchState.Engine.cs`; wiring steps in `integration_update.md` section 6). `Comms/` holds the comms team's `Envelope`, `SeenEvents` and `MqttConfig`, copied unchanged. `Comms/CommsVisualiser/` is the comms team's SSH-tunnel transport and AI gesture hook, copied unchanged from branch `vis-comms`. |
| `script/Resources/` | `BridgeRushTowerDepthClear.shader`, loaded by `TowerController` so towers are not hidden by AR occlusion. |
| `script/Gameplay/` | `GameStateRenderer`, `UnitView`, `SpawnEffect` (summon dust, smoke and sparks), `TowerController`, `ArDepthClear` (keeps towers and bubbles from being hidden by AR occlusion) and `SpellCaster`, plus `Environment/` for the bridge, water and tower pedestal visuals, and `Bubbles/` (`BubbleCaptureManager`, `CaptureBubble`, `BubbleReward`) for the capture mechanic. |
| `plugins/iOS/` | `BridgeRushHaptics.mm`, the native iOS code for damage vibration. It is not part of `script/`: copy it to `My project/Assets/Plugins/iOS/` (already done) and set it to iOS only in the Inspector. Needs a full iOS rebuild. |
| `script/UI/` | `MatchHudUI`, `CardHandUI`, `SetupScreen`, `TitleArt` (the wooden menu button), `CardArt` (card-style frames, gems, bars), `HowToPlayWindow`, `BubbleWarningUI`, `DebugMenu`, `OptionsWindow` and shared UI helpers. |
| `script/Tutorial/` | The tutorial mode: `TutorialFlow` (ten steps: shake to lock the tower, shake to unlock it, capture a bubble, each unit twice, then the two spells) and `TutorialCoach` (the panel that says which gesture to do next). Chosen with TUTORIAL on the setup screen. |

## Integration with the comms pipeline

The glove gesture reaches the phone as the comms team's AI event on `game/gesture/{player}`. Each phone subscribes to its own player's topic and plays the gesture through the same path as the touch pad, so on the Player 2 phone the existing request-to-host route still carries the aim.

| Topic | Direction | Used for |
| --- | --- | --- |
| `game/gesture/{player}` | pipeline to phone | Glove gesture. `payload.gesture` is one of circle, triangle, square, zigzag, arc, line. |
| `viz/ready/{player}` | phone to pipeline | Sent when the arena frame is locked. Payload: `status`, `player`, `lane_length_m`. Not retained yet. |
| `game/state` | host phone to client phone (retired for VS PLAYER: the board engine hosts it, a phone never does) | Game snapshot every tick, QoS 0. The snapshot layout is this project's own; it now also carries a `strikes` list (lightning telegraphs and fireball flights, with `kind`, `fromAlong`, `fromLat`, `burn`) so both phones draw the strike, and each unit carries `target` and `range` so the client can draw ranged bolts. |
| `bridgerush/game/request` | client phone to host phone | Spawn or spell request. A lightning or fireball request carries the aim as `hasAim`, `along`, `lat`. Not in the comms contract. |
| `viz/bubble_captured/{player}` | phone to pipeline | Bubble captured. Payload: `event`, `player`, `bubble_id`. Proposed topic, the Ultra96 awards the reward. |
| `bridgerush/bubble/playerN` | phone to phone | Lock progress stream: `targeted`, `progress`. Drives the defender's warning. Stub. |
| `bridgerush/ready/playerN`, `bridgerush/lock/playerN`, `bridgerush/match/countdown` | phone to phone | Lock-in, tower lock and countdown. Not in the comms contract. |

Every message on every topic is wrapped in the five-field envelope (`event_id`, `source`, `seq`, `ts`, `payload`), and incoming messages are checked by type and de-duplicated on `event_id`.

The broker address is set on the `MqttConnection` component (keep it `127.0.0.1` when using the Comms tunnel). The debug menu no longer has a broker field: its Comms category connects through `CommsTunnelBridge`.

### Still open

- **Bubble capture on the comms side.** `viz/bubble_captured/{player}` is a proposed topic; the Ultra96 must validate it, award the mana and feed it back. See the TODO in `BubbleSync.cs` and `integration_update.md`.
- **Who runs the game logic.** Here the Player 1 phone is the host. The comms notes assume the engine is on the laptop or the Ultra96 and phones only render. If that is decided, `MatchState` moves and `game/state` becomes the engine's output.
- **Glove bindings.** The glove sends the name of the unit or spell it recognised, and `GestureBindings` accepts exactly these (not case sensitive): warrior, rogue, mage, tank (or shield), hero, lightning, fireball. Anything else is rejected as unknown. The classifier on the board must emit these names; until it does, its old classes (arc, caret, circle, infinity, star, triangle, z) are rejected.
- **Phone-to-phone topics** above should be added to the comms topic contract, or moved onto contracted ones.
- **`viz/ready` lifecycle** (retained, last will, match id) is not implemented.
- **mTLS** is not implemented; it is only needed if the broker runs on the laptop.

## Requirements

- Unity `6000.5.9f1` (the comms visualiser uses `6000.6.0f1`).
- Packages: AR Foundation 6.5.0, ARCore / ARKit XR plugins 6.5.0, XR Interaction Toolkit 3.5.1, Universal RP 17.0.3, Input System 1.19.0, `com.unity.nuget.newtonsoft-json` 3.2.2, and `jp.keijiro.apriltag` (from GitHub).
- `MQTTnet.dll` (4.3.7, netstandard2.1) in `Assets/Plugins/MQTTnet`, and `Assets/link.xml` preserving MQTTnet and Newtonsoft. Both are in the comms repo at `comms/phone/demo-visualiser/Assets`.
- Player Settings: Internet Access set to Require (`ForceInternetPermission: 1`), or MQTTnet's raw sockets are blocked on Android.
- An MQTT broker on the same network as both phones.

## Running it

1. Open a Unity project with the packages above and copy `script/` into its `Assets` folder.
2. Add MQTTnet, `link.xml` and the Internet setting as described above.
3. Assign the scene references (ArenaFrame, MatchState, MqttConnection and so on) in the inspector. Tuning values such as mana, tower HP and lane size are serialized fields on `MatchState`.
4. Build to an iOS or Android device. Both players need a printed AprilTag on their side of the table.
5. To test without the glove, publish an event with `mqtt_client.py --publish-every` from the comms repo.
