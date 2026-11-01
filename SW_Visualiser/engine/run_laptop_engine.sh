#!/bin/zsh
# Runs everything the phones need on this laptop for a two-phone board-engine test (no board, no gloves):
# a Mosquitto broker on the LAN, a retained "both gloves ready" status, and the game engine with our core.
# Ctrl-C stops all three. Needs mosquitto (brew install mosquitto). The first run makes a venv with aiomqtt.
#
#   ./run_laptop_engine.sh            # broker on port 1883
#   PORT=1884 ./run_laptop_engine.sh
set -e
HERE="${0:A:h}"
ROOT="${HERE:h:h}"
PORT="${PORT:-1883}"
WORK="$(mktemp -d)"
VENV="$HERE/.venv"

if [[ ! -x "$VENV/bin/python" ]]; then
  echo "making $VENV (once)..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q "aiomqtt>=2,<3"
fi
MOSQ="$(command -v mosquitto || echo /opt/homebrew/sbin/mosquitto)"
[[ -x "$MOSQ" ]] || { echo "mosquitto not found: brew install mosquitto"; exit 1; }

# the engine refuses to start on a rules file with no tower radius, so check first
"$VENV/bin/python" - <<PY
import json, sys
if json.load(open("$HERE/rules.json"))["TowerController"]["TowerRadius"] is None:
    sys.exit("rules.json has no TowerController.TowerRadius yet")
PY

printf 'listener %s 0.0.0.0\nallow_anonymous true\npersistence false\n' "$PORT" > "$WORK/mosquitto.conf"
"$MOSQ" -c "$WORK/mosquitto.conf" > "$WORK/mosquitto.log" 2>&1 &
BROKER=$!
trap 'kill $BROKER 2>/dev/null; rm -rf "$WORK"' EXIT INT TERM
sleep 1
kill -0 $BROKER 2>/dev/null || { echo "mosquitto did not start:"; cat "$WORK/mosquitto.log"; exit 1; }

# no gloves in this test, so say both are ready (retained, as the relay would)
(cd "$ROOT/Comms/ultra96" && "$VENV/bin/python" - <<PY
import asyncio, json, aiomqtt, envelope
async def main():
    async with aiomqtt.Client("127.0.0.1", $PORT, identifier="fake-gloves") as c:
        env = envelope.build_envelope({"player1": "ready", "player2": "ready"}, 1, source=envelope.GLOVES_SOURCE)
        await c.publish(envelope.GLOVES_TOPIC, env if isinstance(env, (str, bytes)) else json.dumps(env), qos=1, retain=True)
asyncio.run(main())
PY
)

IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo '<this laptop IP>')"
echo
echo "Broker for the phones:  $IP : $PORT   (same Wi-Fi as both phones)"
echo "Phones: Mqtt Connection > Broker Host = $IP, Port = $PORT, Connect On Start ON"
echo "Engine log below. Ctrl-C stops everything."
echo
cd "$ROOT/Comms/ultra96"
"$VENV/bin/python" game_engine.py --port "$PORT" --core-dir "$HERE" -v
