"""Watch the board's broker through mosquitto_sub, over the dashboard's own SSH connection."""

import asyncio
import collections
import json
import logging
import random
import shlex
import time

import asyncssh

import envelope  # the board's topic names, so the two never drift
import tunnel
from dashboard import clean, redact, sleep_or_stop

# mosquitto publishes $SYS every 10 s by default, so a healthy link is never quiet this long
BOARD_QUIET_S = 25.0
GESTURES_SHOWN = 3

CONNECT_TIMEOUT_S = 20
BACKOFF_MIN_S = 2.0
BACKOFF_MAX_S = 60.0
HEALTHY_RUN_S = 30.0
SUBSCRIBER_RETRY_S = 5.0
AGENT_TRIES = 20  # each waits 0.05 to 0.3 s, so about 3.5 s at most
# asyncssh 2.24 reads agent_path=None as the default agent, so name a pipe that doesn't exist
NO_AGENT_PIPE = r"\\.\pipe\comms-dashboard-no-agent"

CLIENTS_TOPIC = "$SYS/broker/clients/connected"
TOPICS = (
    envelope.GLOVES_TOPIC,
    envelope.gesture_topic("+"),
    "viz/ready/+",
    "viz/echo/+",
    CLIENTS_TOPIC,
)


def subscribe_args(host=envelope.LOOPBACK_HOST, port=envelope.PLAINTEXT_PORT):
    """Return the mosquitto_sub command line that prints every watched message as one line."""
    args = ["mosquitto_sub", "-h", host, "-p", str(port), "-I", "comms-dashboard", "-v"]
    for topic in TOPICS:
        args += ["-t", topic]
    return args


# exec, so the pty's hangup reaches mosquitto_sub itself when this connection closes
SUBSCRIBE_COMMAND = "exec " + shlex.join(subscribe_args())

log = logging.getLogger(__name__)


class Board:
    """Tracks what the board's broker says, read through mosquitto_sub."""

    def __init__(self, watched=True):
        self.watched = watched
        self.link = "starting" if watched else "not watched (--no-board)"
        self.link_ok = False
        self.note = ""
        self.last_message_at = None
        self.clients = None
        self.gloves = None
        self.gestures = {
            player: collections.deque(maxlen=GESTURES_SHOWN) for player in envelope.PLAYERS
        }
        self.ready_at = dict.fromkeys(envelope.PLAYERS)
        self.echo_at = dict.fromkeys(envelope.PLAYERS)
        self.bad_messages = 0

    def set_link(self, text, ok=False):
        self.link = clean(text)
        self.link_ok = ok

    def subscribed(self):
        """Forget what the broker will send again on a new subscription."""
        self.gloves = None
        self.clients = None
        self.note = ""

    def apply(self, line, now):
        """Update from one mosquitto_sub -v line, which is the topic, a space and the payload."""
        topic, _, payload = line.partition(" ")
        known = topic in (envelope.GLOVES_TOPIC, CLIENTS_TOPIC) or topic.startswith(
            ("game/gesture/", "viz/ready/", "viz/echo/")
        )
        if not known:
            self.note = clean(line)[:120]  # mosquitto_sub's own words, such as a connect error
            return
        self.last_message_at = now
        try:
            self._apply(topic, payload, now)
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
            self.bad_messages += 1

    def _apply(self, topic, payload, now):
        if topic == CLIENTS_TOPIC:
            if not payload.strip().isdigit():
                raise ValueError(payload)
            self.clients = int(payload)
            return
        if topic == envelope.GLOVES_TOPIC:
            states = json.loads(payload)["payload"]
            gloves = {player: states[f"player{player}"] for player in envelope.PLAYERS}
            if not all(isinstance(state, str) for state in gloves.values()):
                raise TypeError(gloves)
            self.gloves = {player: clean(state) for player, state in gloves.items()}
            return
        player = topic.rsplit("/", 1)[1]
        if not player.isdigit() or int(player) not in envelope.PLAYERS:
            return  # e.g. game/gesture/0, an event with no player
        player = int(player)
        if topic.startswith("game/gesture/"):
            gesture = json.loads(payload)["payload"]["gesture"]
            if not isinstance(gesture, str):
                raise TypeError(gesture)
            self.gestures[player].appendleft((clean(gesture), now))
        elif topic.startswith("viz/ready/"):
            self.ready_at[player] = now
        else:
            self.echo_at[player] = now


async def read_board(process, board):
    """Apply each line mosquitto_sub prints until it exits."""
    async for line in process.stdout:
        board.apply(line.rstrip("\r\n"), time.monotonic())


async def subscribe_until_stopped(conn, board, stop):
    """Run mosquitto_sub on the board, and start it again if it exits by itself.

    Raises:
        asyncssh.Error, OSError: the SSH connection failed.
    """
    while not stop.is_set():
        process = await conn.create_process(SUBSCRIBE_COMMAND, term_type="xterm")
        board.subscribed()
        board.set_link("connected", ok=True)
        reading = asyncio.create_task(read_board(process, board))
        stopping = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait({reading, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (reading, stopping):
                task.cancel()
            await asyncio.gather(reading, stopping, return_exceptions=True)
            process.close()
        if stop.is_set():
            return
        reading.result()  # re-raise a lost connection
        log.warning("mosquitto_sub ended: %s", board.note or "no output")
        board.set_link(f"subscriber ended, restarting in {SUBSCRIBER_RETRY_S:.0f}s")
        await sleep_or_stop(stop, SUBSCRIBER_RETRY_S)


async def open_agent(stop):
    """Open one ssh-agent connection for the whole run, and return it with its keys.

    The agent's pipe refuses a process that opens it at the same moment as another one.
    So retry only that, and never open it again. Returns (None, ()) if there is no agent.
    """
    for _ in range(AGENT_TRIES):
        agent = None
        try:
            agent = await asyncssh.connect_agent()
            if agent is None:
                return None, ()
            return agent, await agent.get_keys()
        except FileNotFoundError:
            return None, ()  # no agent running
        except (OSError, asyncssh.Error) as exc:
            if agent is not None:
                agent.close()
            log.info("ssh-agent busy (%s), trying again", exc)
            await sleep_or_stop(stop, random.uniform(0.05, 0.3))
            if stop.is_set():
                return None, ()
    log.warning("ssh-agent still busy, so each connect will open it itself")
    return None, ()


async def board_session(config, password, board, stop, keys=()):
    """Open both SSH hops and watch the broker until `stop` is set or the link fails."""
    options = {
        "keepalive_interval": tunnel.KEEPALIVE_INTERVAL_S,
        "keepalive_count_max": tunnel.KEEPALIVE_COUNT_MAX,
        "connect_timeout": CONNECT_TIMEOUT_S,
    }
    if keys:
        # sign through the agent connection already held, so a reconnect never opens its pipe
        options.update(client_keys=keys, agent_path=NO_AGENT_PIPE)
    async with asyncssh.connect(
        config.jump_host, username=config.jump_user, password=password, **options
    ) as jump:
        # no key or password for the board, hop 2 is key-only as in tunnel.py
        async with asyncssh.connect(
            config.board_host, username=config.board_user, tunnel=jump, **options
        ) as conn:
            log.info("connected to the board")
            await subscribe_until_stopped(conn, board, stop)


async def watch_board(config, password, board, stop, session=board_session, agent=open_agent):
    """Keep a board session running, reconnecting with backoff, until `stop` is set.

    A rejected login is never retried, since repeating a bad password could lock the NUS account.
    """
    board.set_link("opening ssh-agent")
    held, keys = await agent(stop)
    try:
        await _reconnect(config, password, board, stop, session, keys)
    finally:
        if held is not None:
            held.close()


async def _reconnect(config, password, board, stop, session, keys):
    backoff = BACKOFF_MIN_S
    while not stop.is_set():
        started = time.monotonic()
        board.set_link("connecting")
        try:
            await session(config, password, board, stop, keys)
        except asyncssh.PermissionDenied as exc:
            reason = redact(exc.reason or str(exc), config)
            board.set_link(f"login rejected, not retried: {reason}")
            log.error("login rejected: %s", reason)
            return
        except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
            reason = redact(str(exc) or type(exc).__name__, config)
            board.set_link(f"down: {reason}")
            log.warning("board session failed: %s", reason)
        if stop.is_set():
            return
        if time.monotonic() - started >= HEALTHY_RUN_S:
            backoff = BACKOFF_MIN_S
        board.set_link(f"{board.link}, retrying in {backoff:.0f}s")
        await sleep_or_stop(stop, backoff)
        backoff = min(backoff * 2, BACKOFF_MAX_S)
