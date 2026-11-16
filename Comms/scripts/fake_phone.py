"""Stand in for one player's phone against the game engine, for testing without Unity.

Streams viz/lane, presses START and taps cards as told, and logs every game/match and
game/event. --gesture instead sends one glove gesture exactly as the board's relay
would, then exits.

    python fake_phone.py --player 2 --start                      # an opponent that's always ready
    python fake_phone.py --player 2 --start --tap Warrior --every 3
    python fake_phone.py --player 1 --gesture warrior            # one glove gesture for player 1

There's no reconnect. A lost broker ends it, and running it again is a new phone with a
new source, which is how the engine's rejoin is tested.
"""

import argparse
import asyncio
import json
import logging
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# laptop/ first, as in run_board.py. Only framing.py is in both, and it isn't used here.
sys.path.insert(0, str(REPO / "laptop"))
sys.path.append(str(REPO / "ultra96"))

import aiomqtt  # noqa: E402

import ai_events  # noqa: E402
import envelope  # noqa: E402
import mqtt_config  # noqa: E402
import mqtt_publisher  # noqa: E402
from logging_setup import setup_logging  # noqa: E402
from mqtt_client import use_selector_event_loop  # noqa: E402

LOG_FILE = REPO / "logs" / "fake_phone.log"

LANE_HZ = 10  # as the real phone
START_RESEND_S = 2.0  # a START not yet counted is sent again this often
UNITS = ("Warrior", "Rogue", "Mage", "Tank", "Hero")
SPELLS = ("lightning", "fireball")
# Where the tower stands and the crosshair points, in lane units (along, lat). Each player's
# home anchor, and an aim inside their own territory.
HOME = {1: (0.0, 0.0), 2: (1.0, 0.0)}
AIM = {1: (0.25, 0.0), 2: (0.75, 0.0)}

log = logging.getLogger("fake_phone")


class Phone:
    """One fake player's messages and timing, with no I/O, so tests can drive it."""

    def __init__(self, player, lane_m=2.0, auto_start=False, tap=None, every=3.0):
        self.player = player
        self.lane_m = lane_m
        self.auto_start = auto_start
        self.tap = tap
        self.every = every
        self.session = uuid.uuid4().hex[:8]  # new per run, like a relaunched app
        self.source = f"fake-phone-{self.session}"
        self.match = {}  # the latest game/match payload
        self.running_match = ""  # the match this phone has seen running, so it has a frame
        self._seq = 0
        self._req_id = -1
        self._pending = None  # (action, match_id, request, sent_at) of an uncounted START
        self._last_tap = None

    def wrap(self, payload):
        """Wrap a payload as the phone's MqttConnection does, with its own source and seq from 1."""
        self._seq += 1
        return envelope.build_envelope(payload, self._seq, source=self.source, session=self.session)

    def on_match(self, payload):
        """Keep the latest game/match, and note when this phone's match starts running."""
        self.match = payload
        if payload.get("phase") == "running":
            self.running_match = payload.get("match_id", "")

    @property
    def match_id(self):
        match_id = self.match.get("match_id", "")
        return match_id if isinstance(match_id, str) else ""

    def lane(self):
        """Return this sample's viz/lane payload. Position and aim only once the match has run."""
        framed = self.match_id != "" and self.running_match == self.match_id
        along, lat = HOME[self.player] if framed else (0.0, 0.0)
        aim_along, aim_lat = AIM[self.player] if framed else (0.0, 0.0)
        return {
            "match_id": self.match_id,
            "hasLane": True,
            "lane_length_m": self.lane_m,
            "hasPosition": framed,
            "along": along,
            "lat": lat,
            "locked": True,
            "hasAim": framed,
            "aimAlong": aim_along,
            "aimLat": aim_lat,
        }

    def request(self, action, unit_type="", spell=""):
        """Return a new game/request payload with the next req_id."""
        self._req_id += 1
        has_aim = action in ("spawn", "spell")
        along, lat = AIM[self.player] if has_aim else (0.0, 0.0)
        return {
            "match_id": self.match_id,
            "req_id": self._req_id,
            "action": action,
            "unitType": unit_type,
            "spell": spell,
            "hasAim": has_aim,
            "along": along,
            "lat": lat,
        }

    def start_due(self, now):
        """Return the START or rematch to send now, or None.

        The same req_id is sent again every START_RESEND_S until game/match counts it, and a
        new match gets a new req_id.
        """
        action = {"waiting": "start", "ended": "rematch"}.get(self.match.get("phase"))
        counted = self.match.get(f"start{self.player}") is True
        if not self.auto_start or action is None or counted or not self.match_id:
            self._pending = None
            return None
        if self._pending is None or self._pending[:2] != (action, self.match_id):
            self._pending = (action, self.match_id, self.request(action), now)
            return self._pending[2]
        if now - self._pending[3] >= START_RESEND_S:
            self._pending = (*self._pending[:3], now)
            return self._pending[2]
        return None

    def tap_due(self, now):
        """Return the card tap to send now, or None. Taps only while the match runs."""
        if self.tap is None or self.match.get("phase") != "running":
            return None
        if self._last_tap is not None and now - self._last_tap < self.every:
            return None
        self._last_tap = now
        if self.tap in SPELLS:
            return self.request("spell", spell=self.tap)
        return self.request("spawn", unit_type=self.tap)


def flags(m, name):
    """Return e.g. "10" for start1 true and start2 false."""
    return "".join("1" if m.get(f"{name}{player}") is True else "0" for player in (1, 2))


def describe_match(m):
    """One line for a game/match payload."""
    reason = f" ({m['reason']})" if m.get("reason") else ""
    return (
        f"match {m.get('match_id')!r} {m.get('phase')}{reason} phones={flags(m, 'phone')} "
        f"gloves={flags(m, 'glove')} starts={flags(m, 'start')} winner={m.get('winner')}"
    )


def describe_event(e):
    """One line for a game/event payload."""
    if e.get("ok") is True:
        outcome = f"ok, unit {e.get('unit_id')}"
    else:
        outcome = f"refused {e.get('reason')}"
    what = f"{e.get('input')} {e.get('action')} {e.get('what')!r}"
    return f"event player {e.get('player')} {what}: {outcome}"


def payload_of(message):
    """Return the payload inside a message's envelope, or None if it isn't one."""
    try:
        payload = json.loads(message.payload)["payload"]
    except (ValueError, TypeError, KeyError):
        return None
    return payload if isinstance(payload, dict) else None


async def receive(client, phone):
    """Log every game/match and game/event, and keep the latest game/match."""
    async for message in client.messages:
        payload = payload_of(message)
        if payload is None:
            log.warning("skipping a message on %s that isn't an envelope", message.topic)
        elif message.topic.matches("game/match"):
            phone.on_match(payload)
            log.info("%s", describe_match(payload))
        else:
            log.info("%s", describe_event(payload))


async def send(client, phone, stop):
    """Send viz/lane at LANE_HZ, plus any START or tap that is due, until `stop` is set."""
    lane_topic = f"viz/lane/{phone.player}"
    request_topic = f"game/request/{phone.player}"
    while not stop.is_set():
        await client.publish(lane_topic, json.dumps(phone.wrap(phone.lane())), qos=0)
        now = time.monotonic()
        for request in (phone.start_due(now), phone.tap_due(now)):
            if request is not None:
                await client.publish(request_topic, json.dumps(phone.wrap(request)), qos=1)
                log.info("sent %s, req_id %d", request["action"], request["req_id"])
        try:
            await asyncio.wait_for(stop.wait(), timeout=1 / LANE_HZ)
        except asyncio.TimeoutError:
            pass


async def run(phone, host, port, stop=None):
    """Be the phone until `stop` is set or the broker connection drops."""
    if stop is None:
        stop = asyncio.Event()
    async with aiomqtt.Client(host, port, identifier=phone.source) as client:
        await client.subscribe("game/match", qos=1)
        await client.subscribe("game/event", qos=1)
        log.info("player %d as %s on mqtt://%s:%d", phone.player, phone.source, host, port)
        tasks = [
            asyncio.create_task(receive(client, phone)),
            asyncio.create_task(send(client, phone, stop)),
            asyncio.create_task(stop.wait()),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        stop.set()  # before cancelling, so no wait_for swallows the cancel
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for task in done:
            task.result()


async def send_gesture(host, port, player, name):
    """Publish one AI event for `player`, built and sent as the board's relay does."""
    env = envelope.build_envelope(ai_events._event(player, 0, name), 0)
    # its own client id, since the relay's would evict the real relay from the broker
    async with aiomqtt.Client(host, port, identifier=f"fake-phone-{envelope.SESSION_ID}") as client:
        await client.publish(
            envelope.gesture_topic(player), json.dumps(env), qos=mqtt_publisher.QOS_GAME_GESTURE
        )
    log.info("sent gesture %r for player %d as %s", name, player, env["event_id"])


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--player", type=int, choices=(1, 2), required=True)
    parser.add_argument("--host", help="broker host (default: MQTT_BROKER_HOST, else 127.0.0.1)")
    parser.add_argument("--port", type=mqtt_config.port_number, help="broker port (default 1883)")
    parser.add_argument("--lane", type=float, default=2.0, help="lane length to report, in metres")
    parser.add_argument("--start", action="store_true", help="press START, then rematch")
    parser.add_argument("--tap", choices=UNITS + SPELLS, help="tap this card while the match runs")
    parser.add_argument("--every", type=float, default=3.0, help="seconds between taps")
    parser.add_argument("--gesture", metavar="NAME", help="send one glove gesture and exit")
    return parser.parse_args(argv)


def main():
    args = parse_args()
    setup_logging(LOG_FILE)
    broker = mqtt_config.resolve_broker(args.host, args.port)
    use_selector_event_loop()
    try:
        if args.gesture:
            asyncio.run(send_gesture(broker.host, broker.port, args.player, args.gesture))
        else:
            phone = Phone(args.player, args.lane, args.start, args.tap, args.every)
            asyncio.run(run(phone, broker.host, broker.port))
    except KeyboardInterrupt:
        log.info("stopped")
    except (aiomqtt.MqttError, OSError) as exc:
        # OSError too, since on Windows a link lost mid-publish can stop the whole event loop
        raise SystemExit(f"broker connection failed: {exc}")


if __name__ == "__main__":
    main()
