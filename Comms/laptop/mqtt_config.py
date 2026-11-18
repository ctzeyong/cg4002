"""Configuration for the MQTT leg: laptop relay <-> phone visualisers.

Both open decisions come down to one value, `host`: where the broker runs (laptop
vs Ultra96) and which network it runs on (hotspot vs SoC wifi). Neither has to be
settled before the leg is built. Nothing else in the codebase may hardcode a
broker address; take it from `resolve_broker()`.

Resolution order is CLI argument > environment variable > default, so the address
can change on demo day with an env var and no edit. The default is loopback rather
than a LAN address on purpose: the address measured on 2026-09-09 (10.160.77.244)
is a per-session DHCP lease from the phone hotspot and will not survive to the
demo. Re-read it, never bake it in.

See Comms Initial Design Report Section 4.5.
"""

import argparse
import math
import os
from dataclasses import dataclass
from pathlib import Path

# Loopback until the broker location is settled. Everything on this leg is
# testable against 127.0.0.1; the phone only replaces the *subscriber*.
DEFAULT_HOST = "127.0.0.1"

PLAINTEXT_PORT = 1883
TLS_PORT = 8883  # mTLS listener

# Topic contract with the visualiser. Changing a name is a design-report change,
# not a local edit.
#
# Both carry a player id in the last segment, so each phone can subscribe to its
# own player and the laptop can tell the two apart without inspecting payloads.
TOPIC_GAME_GESTURE = "game/gesture/{player}"  # laptop -> phones: AI gesture events
TOPIC_VIZ_READY = "viz/ready/{player}"        # phones -> laptop: visualiser is up
TOPIC_VIZ_READY_FILTER = "viz/ready/+"        # the subscription form of the above

# Used when an outbound event carries no usable player id. Publishing under a
# visibly wrong id beats dropping a real AI event or guessing player 1: a phone on
# `game/gesture/+` still receives it, and it reads as odd in the log.
UNATTRIBUTED_PLAYER = 0

# Topic used only by mqtt_ping.py. Kept outside the game/ and map/ namespaces on
# purpose, so a latency run never lands in front of the visualiser.
TOPIC_PING = "comms/ping"

# QoS 1 (at-least-once) for both game topics, per the reliability design. It can
# deliver the same message twice, which is why receivers dedupe on the envelope's
# event_id; see mqtt_client.SeenEvents.
QOS_GAME_GESTURE = 1
QOS_VIZ_READY = 1

# The broker declares a client dead after 1.5x this with no traffic, so 15 s
# means a silently-dropped phone is noticed in ~22 s. Lower costs almost
# nothing (a 2-byte PINGREQ) and is what makes the reconnect demo quick.
KEEPALIVE_S = 15

# Stable per-role client identifiers. The broker evicts an existing client with
# the same identifier, so two processes must never share one. That failure shows
# up as an unexplained reconnect loop rather than an error.
CLIENT_ID_LAPTOP_RELAY = "laptop-relay"
CLIENT_ID_PING = "comms-ping"

# Same backoff shape as ble_client.run_node and tunnel.run_tunnel: double on a fast
# failure, reset after a healthy run, cap at 60 s.
MIN_BACKOFF_S = 2.0
MAX_BACKOFF_S = 60.0
GOOD_RUN_S = 60.0

# mTLS material. Named here so the swap to TLS is a config change and
# not a code change; none of these files exist yet.
CERT_DIR = Path(__file__).resolve().parent.parent / "certs"
CA_CERT = CERT_DIR / "ca.crt"
CLIENT_CERT = CERT_DIR / "laptop-relay.crt"
CLIENT_KEY = CERT_DIR / "laptop-relay.key"


MIN_PORT = 1
MAX_PORT = 65535


# argparse `type=` validators, shared by mqtt_client.py and mqtt_ping.py. They live
# here so both entry points reject bad values the same way, and reject them with a
# usage message at startup rather than as an obscure error once the broker is
# connected: `--count 0` would otherwise report "no pings returned", which reads as
# a network fault.
def _whole_number(value, label):
    """Coerce `value` to an int, refusing anything not already whole.

    `int(1.5)` truncates to 1 without complaint, so a caller passing a float would
    silently get a different number than they asked for, e.g. `port_number(1.5)`
    becoming port 1. Non-integral floats are rejected instead. bool is rejected
    too, since it subclasses int and True would otherwise pass as 1.

    The CLI never reaches these cases: argparse hands over strings and
    `int("1.5")` already raises. This guards the programmatic callers.

    Raises:
        ValueError: not a number, a boolean, or a non-integral one.
    """
    if isinstance(value, bool):
        raise ValueError(f"{label} must not be a boolean: {value!r}")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} is not a number: {value!r}")
    if isinstance(value, float) and number != value:
        raise ValueError(f"{label} must be a whole number: {value!r}")
    return number


def port_number(value):
    """Validate a TCP port, as an argparse type or on its own.

    Raises:
        argparse.ArgumentTypeError: not a whole number, or outside 1-65535.
    """
    try:
        port = _whole_number(value, "port")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))
    if not MIN_PORT <= port <= MAX_PORT:
        raise argparse.ArgumentTypeError(f"port out of range {MIN_PORT}-{MAX_PORT}: {port}")
    return port


def positive_int(value):
    """Validate a count of at least 1.

    Raises:
        argparse.ArgumentTypeError: not a whole number, or below 1.
    """
    try:
        number = _whole_number(value, "count")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {number}")
    return number


def non_negative_float(value):
    """Validate a duration in seconds; 0 is allowed and usually means "off".

    Rejects nan and inf as well as negatives. `float("nan") < 0` is False, so a nan
    would pass a bare sign check and then make every comparison downstream false:
    an interval that never elapses, a linger that never expires.

    Raises:
        argparse.ArgumentTypeError: not a number, not finite, or negative.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"not a number: {value!r}")
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError(f"must be finite and non-negative, got {number}")

    return number


def gesture_topic(player):
    """Concrete publish topic for one player, e.g. `game/gesture/1`.

    Kept here rather than formatted at the call site so the `{player}` template
    and the code that fills it cannot drift apart.
    """
    return TOPIC_GAME_GESTURE.format(player=player)


def player_from_topic(topic):
    """Pull the player id off the end of a `viz/ready/<player>` topic.

    The laptop subscribes with a `+` wildcard, so the player arrives in the topic
    rather than in the payload. That is deliberate: the relay can attribute a
    message without parsing anything the game engine owns.

    Returns:
        The player id as an int, or None if the last segment is not one. A phone
        publishing to a malformed topic is the visualiser's bug, not a reason to
        drop the session, so callers log and carry on.
    """
    last = str(topic).rsplit("/", 1)[-1]
    try:
        return int(last)
    except ValueError:
        return None


@dataclass(frozen=True)
class BrokerConfig:
    """Where the broker is and how to talk to it.

    Passed explicitly into every client rather than read from module globals, so
    two of these can run side by side in one process. That is what a test needs,
    and the Ultra96-vs-laptop latency comparison too.
    """

    host: str
    port: int
    tls: bool = False

    def describe(self):
        """One line for the startup log.

        Worth logging on every run: when the phone cannot connect, the first
        question is always which address the laptop actually bound, and the
        second is whether TLS is on at one end only.
        """
        return f"{'mqtts' if self.tls else 'mqtt'}://{self.host}:{self.port}"


def resolve_broker(host=None, port=None, tls=False):
    """Resolve the broker address: CLI argument > environment > default.

    The environment layer is what lets the demo-day address change without an
    edit or a rebuild:

        $env:MQTT_BROKER_HOST = "10.160.77.244"   # PowerShell
        export MQTT_BROKER_HOST=10.160.77.244     # bash

    Args:
        host: explicit host, usually an argparse value. None falls through.
        port: explicit port. None falls through to MQTT_BROKER_PORT, then to
            the port implied by `tls`.
        tls: select the mTLS listener rather than the plaintext one.

    Raises:
        ValueError: MQTT_BROKER_PORT is not a number, or the resolved port is
            outside 1-65535 whichever layer it came from.
    """
    resolved_host = host or os.environ.get("MQTT_BROKER_HOST") or DEFAULT_HOST

    if port is None:
        env_port = os.environ.get("MQTT_BROKER_PORT")
        if env_port:
            try:
                port = int(env_port)
            except ValueError:
                raise ValueError(f"MQTT_BROKER_PORT is not a number: {env_port!r}")
            source = "MQTT_BROKER_PORT"
        else:
            port = TLS_PORT if tls else PLAINTEXT_PORT
            source = "default port"
    else:
        # Programmatic callers can pass anything; the env branch above is always
        # a string and argparse has already applied port_number to --port.
        port = _whole_number(port, "port")
        source = "port"

    # Range-checked on every path, not just the CLI one. argparse already applies
    # port_number to --port, but the environment layer bypasses argparse entirely
    # and is the layer most likely to be edited under time pressure on demo day.
    if not MIN_PORT <= port <= MAX_PORT:
        raise ValueError(f"{source} out of range {MIN_PORT}-{MAX_PORT}: {port}")

    return BrokerConfig(host=resolved_host, port=port, tls=tls)


def tls_params():
    """Build aiomqtt TLS parameters from the cert paths above.

    Always returns parameters, so call it only when TLS is wanted. Callers write
    `tls_params() if broker.tls else None`, which supplies the None aiomqtt expects
    for a plaintext connection at the call site rather than here. Kept in this file
    so the mTLS swap touches config and nothing else.

    Raises:
        FileNotFoundError: TLS was requested before the certs were generated.
            Explicit, because the alternative is a TLS handshake failure that
            reads like a network problem.
    """
    import aiomqtt

    missing = [str(p) for p in (CA_CERT, CLIENT_CERT, CLIENT_KEY) if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "TLS requested but cert material is missing: " + ", ".join(missing)
        )

    return aiomqtt.TLSParameters(
        ca_certs=str(CA_CERT),
        certfile=str(CLIENT_CERT),
        keyfile=str(CLIENT_KEY),
    )
