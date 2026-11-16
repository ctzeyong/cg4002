"""Show the whole pipeline on one screen that redraws in place.

It follows the relay's newest log file, and runs mosquitto_sub on the board over its
own SSH connection. It never talks to the relay or the board client while it runs.
run_all.ps1 opens it beside the two logs.

    python scripts/dashboard
    python scripts/dashboard --no-board         # laptop rows only, no SSH
    python scripts/dashboard --stop-pipeline ID # Ctrl-C also stops run ID's relay and board

Ctrl-C quits.
"""

import asyncio
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
# the laptop and board modules use flat imports
sys.path.insert(0, str(REPO / "laptop"))
sys.path.append(str(REPO / "ultra96"))  # appended, so laptop/framing.py still wins

# asyncssh's own wording when a login is rejected, which names the user
USER_PATTERN = re.compile(r"(for user )\S+")


def clean(text):
    """Return `text` with only printable ASCII, so outside input cannot move the cursor."""
    return "".join(c if " " <= c <= "~" else "?" for c in text)


def redact(text, config):
    """Return `text` with the SSH usernames hidden, since asyncssh names the user in some errors."""
    for user in (config.jump_user, config.board_user) if config is not None else ():
        if user:
            text = text.replace(user, "<username>")
    return USER_PATTERN.sub(r"\1<username>", text)


async def sleep_or_stop(stop, seconds):
    """Wait `seconds`, but return early if `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
