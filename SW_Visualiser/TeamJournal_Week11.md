Week 11

This week the Visualiser grew from showing glove input into a full two-player game, and had to follow changes from Comms and AI. Everything is written and compiles, but has not yet been run on two phones.

Integration Activities

| No. | Subsystem(s) | Activity |
| --- | --- | --- |
| 1 | Visualiser | Add two bridges, aimed Lightning, Fireball, a ranged Mage and units that chase enemies |
| 2 | Visualiser, Comms | Extend phone-to-phone messages: the host sends each unit's facing and a list of spell strikes in `game/state`, and a spell request carries the aim |
| 3 | Visualiser, AI, Comms | Rebind glove gestures to the AI team's new classes |
| 4 | Visualiser | Add sound, music, a vibration pulse when your tower is hit, and double mana in the last minute |

Challenges / Issues and Solutions

Player 2's units faced the wrong way. Player 2's phone only draws the host's updates. Reading the code, we found the host never sent unit facing, so the client guessed from movement. The client's smoothing clock also never ran, so units were drawn one update behind, and turns snapped. The host now sends its real facing, the client clock runs and turning is smoothed. Not yet confirmed on two phones.

Lightning aim and delay. The glove event carries no aim, so a strike lands where the crosshair points on arrival, after the delay through the glove, Bluetooth, Ultra96, tunnel and phone. We have not measured this delay and events have no timestamp, so for now a 0.5 s telegraph locks the target at cast and warns the defender. A cast with no valid aim is refused and costs no mana.

Gesture names changed twice. The AI team's retrained model uses new classes, so the bindings changed twice this week. We agreed the glove will send the unit or spell name instead. The board still emits the old names, which the Visualiser now rejects.

Two phones can disagree. Spell numbers were typed in two places and could drift, so they now live in one block that the host also uses for a client's cast. Each phone scales the arena from its own measured lane, so a client's strike ring may still differ in size from the real hit area.

Decisions Made

- The game logic is meant to run on the Ultra96. Until it is moved there, the Player 1 phone hosts and runs it as a stand-in.
- The glove sends unit and spell names, and spells aim with the phone's crosshair.
- The new message fields are our own formats and still need adding to the topic contract.

Additional To-Dos for Following Week

- First two-phone test on the same build: shared units and spells, Player 2's facing, and tap-to-result delay.
- Measure the glove-to-crosshair delay and ask Comms for gesture timestamps.
- Get the board to emit the new gesture names and add the new fields to the contract.
- Agree with Comms when the game logic moves from the Player 1 phone to the Ultra96. The new bridge, chasing and spell logic would have to move with it.
