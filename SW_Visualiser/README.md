# SW_Visualiser

The software visualiser for BridgeRush: a Unity AR app that renders the match on a real table. Two phones each track an AprilTag marker on the opponent's side, build a shared arena between the two devices, and draw towers, a bridge and units in AR. Players summon units by drawing gestures on the screen.

This folder holds the scripts I wrote for it (`script/`). Art packs, Unity `Library/` output and builds are not included.

- **AR arena tracking**: detect the opponent's AprilTag with `jp.keijiro.apriltag`, smooth the pose, hold it when the marker is briefly lost, and build a lane axis between the two devices. Towers, bridge and water are placed along that axis, with the floor height taken from AR plane detection.
- **Gesture input**: a stroke recogniser that classifies hand-drawn symbols (Circle, Caret, Arc, Z, Infinity, Triangle, Star) from touch points, plus a summon cursor to choose where a unit spawns.
- **Match logic**: a mana economy, unit spawning, lane movement, melee combat, tower damage and counter-damage, out-of-bounds damage, an optional enemy bot, and a 180 s match timer. Unit stats and prefabs are data-driven through ScriptableObjects.
- **Networking**: MQTT connection and sync so both phones agree on ready state, tower lock, countdown, gestures and game state.
- **Rendering and visuals**: unit and tower views, bridge, water and tower pedestal visuals, and a renderer that draws the match state in AR.
- **UI**: match HUD, unit card hand, setup screen, and a debug menu and tracking overlay for testing without a second player.

## How a match works

1. **Setup**: each phone connects to the MQTT broker, scans the floor, and marks itself ready.
2. **Tracking**: the phone detects the opponent's AprilTag and builds the lane between the two devices.
3. **Countdown**: once both players are ready, a synced countdown starts the match.
4. **Play**: mana regenerates over time. The player draws a symbol, picks a spot with the summon cursor, and the matching unit spawns and walks the lane. Units fight on contact and towers hit back.
5. **End**: the match runs on a timer (180 s by default) and tower HP decides the result.

## Script layout

| Folder | What it does |
| --- | --- |
| `script/Tracking/` | `AprilTagTracker` detects markers. `ArenaFrame` turns the device pose and opponent marker into a lane axis and world positions. `FloorHeightProvider`, `MatchSetup` and `TrackingDebugOverlay` support placement and debugging. |
| `script/Input/` | `GesturePad` collects strokes. `GestureRecognizer` classifies them. `SummonCursor` picks the spawn point. |
| `script/Match/` | `MatchState` is the core game logic, split into `.Economy` (mana), `.Combat`, `.Geometry` (lane, bridge, bounds) and `.Network`. `UnitState`, `UnitStatsTable` and `UnitPrefabCatalog` define the units (Warrior, Rogue, Mage, Tank, Hero). |
| `script/Networking/` | `MqttConnection` owns the broker connection. `MatchNetworkSync`, `PeerReadySync`, `TowerLockSync` and `MatchCountdown` keep both phones in step. `NetworkMessages` holds the payload types. |
| `script/Gameplay/` | `GameStateRenderer`, `UnitView`, `TowerController` and `SpellCaster`, plus `Environment/` for the bridge, water and tower pedestal visuals. |
| `script/UI/` | `MatchHudUI`, `CardHandUI`, `SetupScreen`, `DebugMenu` and shared UI helpers. |

## MQTT topics

| Topic | Purpose |
| --- | --- |
| `bridgerush/ready/player1`, `bridgerush/ready/player2` | Player ready state |
| `bridgerush/lock/player1`, `bridgerush/lock/player2` | Tower position lock |
| `bridgerush/match/countdown` | Synced match start |
| `bridgerush/game/gesture` | Gestures sent from the phone |
| `bridgerush/game/state` | Game state snapshots |

The broker host and port are set on the `MqttConnection` component (default `127.0.0.1:1883`) or from the in-app debug menu.

## Requirements

- Unity `6000.5.9f1`
- Packages: AR Foundation 6.5.0, ARCore / ARKit XR plugins 6.5.0, XR Interaction Toolkit 3.5.1, Universal RP 17.0.3, Input System 1.19.0, and `jp.keijiro.apriltag` (from GitHub).
- [M2Mqtt](https://github.com/eclipse/paho.mqtt.m2mqtt) in `Assets/Plugins/M2Mqtt` (not included here).
- An MQTT broker on the same network as both phones.

## Running it

1. Open a Unity project with the packages above and copy `script/` into its `Assets` folder.
2. Add M2Mqtt under `Assets/Plugins`.
3. Assign the scene references (ArenaFrame, MatchState, MqttConnection and so on) in the inspector. Tuning values such as mana, tower HP and lane size are serialized fields on `MatchState`.
4. Build to an iOS or Android device. Both players need a printed AprilTag on their side of the table.
