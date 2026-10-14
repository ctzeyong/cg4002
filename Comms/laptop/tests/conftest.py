import pathlib
import sys

import pytest

_LAPTOP = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(_LAPTOP))
# Appended rather than inserted, so laptop/framing.py keeps winning the `import
# framing` race that test_framing.py depends on. This makes `import tcp_client`
# work from the Ultra96 tree while that client resolves framing to the laptop
# copy — which doubles as a check that the two duplicated copies stay
# wire-compatible, since a drift between them would fail these session tests.
sys.path.append(str(_LAPTOP.parent / "ultra96"))

import tcp_server  # noqa: E402 - needs the sys.path above


@pytest.fixture(autouse=True)
def reset_current_writer():
    """Clear tcp_server's module-global client between tests.

    It is a global precisely so a reconnecting board can evict its own stale
    session and so the uplink forwarder has one unambiguous destination, which
    also means it leaks across tests if left alone.
    """
    tcp_server._current = None
    yield
    tcp_server._current = None
