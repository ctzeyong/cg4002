# Game engine on the Ultra96: who owns what

This page records who owns each part of the game engine now that it runs on the Ultra96, and
what each side may change without asking the other. It covers branch `engine-move`.

> **Current status (2 Nov 2026):** proposed by Comms, for Vis to confirm at the E0 sign-off. Vis's
> core and the phone's side are on this branch since 1 Nov. The message contract (topics, fields
> and types) is added to this page at the sign-off. Nothing here has run on the board or a phone yet.

## Architecture overview

```text
Phones (Unity app)
  viz/lane/N, game/request/N, viz/bubble_captured/N
       |
       | MQTT, through the SSH tunnel
       v
Ultra96: game_engine.py
  Comms: MQTT client, message checks, whether each phone and glove is there
  Session: the match flow (start, countdown, pause, end, rematch); Vis decides these rules
  Vis's core (match.py + rules.json): the match itself (units, spells, mana, towers)
       |
       | game/match, game/state, game/event
       v
Phones

Board relay (Comms): game/gesture/N, status/gloves --> game_engine.py
```

The engine is its own process on the board, next to the relay. Vis's core is a pure Python
module with no clock and no networking. `game_engine.py` calls it, passing in the time and each
player's position and aim.

## Responsibilities

| Owner | Files or part | Purpose |
|---|---|---|
| Vis | [`SW_Visualiser/engine/match.py`](../../SW_Visualiser/engine/) | The match rules: units, spells, mana, combat, towers, the gesture-to-action table |
| Vis | [`SW_Visualiser/engine/rules.json`](../../SW_Visualiser/engine/rules.json) | Every tuning value, exported from the scene |
| Vis | [`SW_Visualiser/script/Networking/Engine/`](../../SW_Visualiser/script/Networking/Engine/) | The phone's end of the engine (`EngineMessages`, `EngineLink`, `EngineLinkCore`). It checks every message, re-sends START, shows the link state, and drops MQTT when snapshots stop. Comms wrote `EngineLink.ReconnectMqtt`, which connects MQTT again after that drop when no tunnel is running, so tell Comms before changing it |
| Vis | The match-flow parts of `Session` in [`Comms/ultra96/game_engine.py`](../../Comms/ultra96/game_engine.py), listed below | When a match starts, pauses, resumes, ends and rematches |
| Comms | [`Comms/ultra96/engine_wire.py`](../../Comms/ultra96/engine_wire.py) | Read and check every incoming message, and build every outgoing one |
| Comms | `Engine`, the MQTT tasks, `run` and `main` in `game_engine.py` | The MQTT client, reconnects, the retained `game/match` and its last will, duplicate removal, exit codes |
| Comms | The link parts of `Session`, listed below | Whether each phone and glove is there, and whether a phone's aim is recent enough to use |
| Comms | [`Comms/ultra96/run_engine.sh`](../../Comms/ultra96/run_engine.sh), [`Comms/scripts/run_board.py`](../../Comms/scripts/run_board.py) | Start, restart and stop the engine on the board |
| Comms | [`Comms/scripts/fake_phone.py`](../../Comms/scripts/fake_phone.py), [`Comms/laptop/tests/stub_core/`](../../Comms/laptop/tests/stub_core/) | Test the engine without Unity or the real core |
| Comms | [`Comms/phone/comms-visualiser/Transport/CommsTunnelBridge.cs`](../../Comms/phone/comms-visualiser/Transport/CommsTunnelBridge.cs) | The phone's SSH tunnel. It reconnects MQTT after a drop and rebuilds the tunnel after two failed broker probes. Vis copies it into the app |

`Session` sits in Comms' file only because it needs the clock and the link state, and the core
must stay free of both. The match-flow rules in it are game design. Comms wrote their first version
to get the move working, and **Vis maintains them from then on**: changing them, fixing them and
keeping their tests up to date. The two halves of `Session` are kept apart by function, not by file,
because the match flow reads the link state throughout.

### `Session`: the match flow (Vis)

Vis maintains these:

- the START gate and what it waits for (`_gate`);
- the countdown (`COUNTDOWN_S`, `_start`, `_back_to_waiting`);
- when a missing phone or glove pauses the match (`_missing`), what must be steady before it
  resumes (`_resumable`), and how long a pause may last (`PAUSE_CAP_S`);
- how a match ends and how a rematch starts (`_end`, `_new_match`, `_press`, `_counts_press`);
- which phone is bound to each player, and when another phone may take over (`_move_binding`).

### `Session`: the links (Comms)

Comms maintains these:

- removing duplicate messages (`Seen`, `DEDUPE_CAP`, `REQ_DEDUPE_CAP`);
- each phone's stream and whether it is still sending (`Stream`, `_fresh`, `_there`,
  `_current_source`, `PHONE_FRESH_S`);
- whether a phone's aim is recent enough for a glove gesture (`_aim_stream`, `AIM_FRESH_S`). After a
  gap, the aim counts again only once the phone has streamed steadily for `RESUME_S`, since a
  recovering link delivers its old samples in a burst. Whether the aim is in the right place for the
  action is the core's check (`bad_aim`), which is Vis's;
- the gloves' state, which is forgotten when the MQTT connection drops (`on_gloves`, `on_mqtt_lost`);
- the tick timing (`on_tick`, `MAX_CATCHUP_TICKS`);
- turning any error from the core, or a result the phones couldn't read, into an `engine error`
  end of match (`_call_core`, `_good_result`, `_fail`).

## Change guide

Vis may change any engine file, `Session` and `engine_wire.py` included, without asking first.
Run the tests before merging. Two kinds of change also need Comms, because they reach past the
engine:

| Change | Why Comms needs to know |
|---|---|
| A topic, or a field's name, type or meaning, in any message | The board's `engine_wire.py` and the phones' `EngineMessages.cs` must change together, or the phones reject every message. Comms also debugs the link, so it needs to know what crosses it |
| `RESUME_S`, `AIM_FRESH_S`, `PHONE_FRESH_S` or `TICK_HZ` on the board, or `StallRebuildS` on the phone | They decide how a dropped link is noticed and how much traffic goes through the tunnel. `RESUME_S` also keeps old samples from a recovering link out of glove aims. `PAUSE_CAP_S` is sized from how fast a phone notices a dead link and rebuilds its tunnel. A change can look fine in play and still break link-loss handling |

For anything else, tell Comms after the change, so both sides keep the same picture of the code.

**Tests.** Run `pytest` from `Comms/laptop`. The engine's tests are
[`test_game_engine.py`](../../Comms/laptop/tests/test_game_engine.py) (`Session`, on the stub core
and a fake clock, and a check that the committed `match.py` and `rules.json` start the engine),
`test_engine_wire.py` (the messages) and `test_game_engine_broker.py` (MQTT, on a real Mosquitto).
Vis's own are in `SW_Visualiser/engine/`: `test_match.py` and `test_integration.py` for `pytest`,
and the C# checks in `csharp_checks/`. A changed match-flow rule makes the tests for the old rule
fail. Update those tests in the same commit.
