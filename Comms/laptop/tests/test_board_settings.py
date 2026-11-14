"""Tests for ultra96/board_settings.py, on a fake /sys/devices/system/cpu tree."""

import json
import threading

import pytest

import board_settings as bs

ORIGINAL = {"cpu0/online": "1", "cpu1/online": "1", "cpu2/online": "1", "cpu3/online": "1",
            bs.SETSPEED: "1199999", bs.GOVERNOR: "userspace",
            bs.AVAILABLE: "299999 399999 599999 1199999 "}


class FakeClocks:
    def __init__(self, mhz=100.0):
        self.fclk0_mhz = mhz


@pytest.fixture
def board(tmp_path):
    cpu = tmp_path / "cpu"
    for rel, value in ORIGINAL.items():
        (cpu / rel).parent.mkdir(parents=True, exist_ok=True)
        (cpu / rel).write_text(value + "\n")
    clocks = FakeClocks()
    settings = bs.Settings(cpu, tmp_path / "power_state.json", clocks=clocks,
                           start_guardian=False, alive=lambda pid: 7)
    return settings


def values(settings):
    return {rel: settings.read(rel) for rel in ORIGINAL}


@pytest.mark.parametrize("spec, wanted", [
    ("khz=399999+cores=2+pl=50", {"khz": 399999, "cores": 2, "pl": 50.0}),
    ("", {}), ("none", {}), ("baseline", {}), ("pl=37.5", {"pl": 37.5}),
])
def test_settings_are_read_from_one_string(spec, wanted):
    assert bs.parse(spec) == wanted
    assert bs.parse(bs.describe(wanted)) == wanted


@pytest.mark.parametrize("spec, reason", [
    ("wifi=off", "Wi-Fi stays on"), ("bt=off", "no Bluetooth"), ("governor=powersave", "only"),
    ("turbo=1", "unknown setting"), ("cores=two", "not a number"),
])
def test_what_is_left_alone_on_purpose_is_refused(spec, reason):
    with pytest.raises(ValueError, match=reason):
        bs.parse(spec)


def test_apply_then_restore_leaves_the_board_as_it_was(board):
    board.apply({"khz": 399999, "cores": 2, "pl": 50.0})

    assert board.read(bs.SETSPEED) == "399999"
    assert [board.read(f"cpu{n}/online") for n in range(4)] == ["1", "1", "0", "0"]
    assert board.clocks.fclk0_mhz == 50.0
    assert board.now() == {"khz": 399999, "cores": 2, "pl": 50.0}
    saved = json.loads(board.state_file.read_text())
    assert saved["files"] == {"cpu2/online": "1", "cpu3/online": "1", bs.SETSPEED: "1199999"}
    assert saved["pl_mhz"] == 100.0 and saved["settings"] == "khz=399999+cores=2+pl=50"

    assert board.restore() is True
    assert values(board) == ORIGINAL | {bs.AVAILABLE: ORIGINAL[bs.AVAILABLE].strip()}
    assert board.clocks.fclk0_mhz == 100.0
    assert not board.state_file.exists()
    assert board.restore() is False  # twice is fine


def test_one_core_never_takes_cpu0_offline(board):
    board.apply({"cores": 1})

    assert board.read("cpu0/online") == "1"
    assert [board.read(f"cpu{n}/online") for n in (1, 2, 3)] == ["0", "0", "0"]
    assert "cpu0/online" not in board.load_state()["files"]


def test_the_originals_are_saved_before_the_first_change(board, monkeypatch):
    seen = []
    write = board.write

    def spy(rel, value):
        seen.append(board.load_state())
        write(rel, value)

    monkeypatch.setattr(board, "write", spy)
    board.apply({"khz": 299999, "cores": 3})

    assert seen and all(s is not None and s["files"][bs.SETSPEED] == "1199999" for s in seen)


@pytest.mark.parametrize("wanted, reason", [
    ({"khz": 800000}, "not one of"), ({"cores": 0}, "1 to 4"), ({"cores": 5}, "1 to 4"),
    ({"pl": 150.0}, "never overclocked"), ({"pl": 5.0}, "between"),
])
def test_unsafe_values_change_nothing(board, wanted, reason):
    with pytest.raises(ValueError, match=reason):
        board.apply(wanted)
    assert values(board)[bs.SETSPEED] == "1199999" and not board.state_file.exists()


def test_khz_is_left_alone_under_another_governor(board):
    board.write(bs.GOVERNOR, "ondemand")
    with pytest.raises(ValueError, match="not userspace"):
        board.apply({"khz": 599999})


def test_a_failure_half_way_puts_back_what_had_changed(board, monkeypatch):
    write = board.write

    def failing(rel, value):
        if rel == bs.SETSPEED and str(value) != "1199999":
            raise OSError("busy")
        write(rel, value)

    monkeypatch.setattr(board, "write", failing)
    with pytest.raises(OSError):
        board.apply({"cores": 1, "khz": 299999})

    assert [board.read(f"cpu{n}/online") for n in range(4)] == ["1"] * 4
    assert not board.state_file.exists()


def test_a_value_that_cannot_be_put_back_keeps_the_state_file(board, monkeypatch):
    board.apply({"khz": 599999})
    monkeypatch.setattr(board, "write", lambda rel, value: (_ for _ in ()).throw(OSError("ro")))

    assert board.restore() is False
    assert board.state_file.exists()  # so the next start tries again


def test_the_next_start_puts_back_what_a_killed_run_left(board):
    board.apply({"khz": 299999, "cores": 1})
    state = board.load_state()
    state["pid"] = 999999  # as if another process made the change and was killed with -9
    board.state_file.write_text(json.dumps(state))
    board.alive = lambda pid: None if pid == 999999 else 7

    board.apply({"khz": 599999})

    assert board.read(bs.SETSPEED) == "599999"
    assert [board.read(f"cpu{n}/online") for n in range(4)] == ["1"] * 4
    board.restore()
    assert board.read(bs.SETSPEED) == "1199999"


def test_a_run_still_going_is_not_undone(board):
    board.apply({"khz": 299999})
    state = board.load_state()
    state["pid"] = 4242
    board.state_file.write_text(json.dumps(state))  # 4242 is alive with the same start time

    with pytest.raises(RuntimeError, match="in use by process 4242"):
        board.apply({"khz": 599999})
    assert board.read(bs.SETSPEED) == "299999"


def guard_in_thread(board, owner_alive):
    result = []
    thread = threading.Thread(target=lambda: result.append(bs.guard(
        board.load_state()["pid"], board.state_file, board.cpu, poll=0.01,
        alive=lambda pid: 7 if owner_alive[0] else None)))
    return thread, result


def test_the_guardian_puts_the_originals_back_when_its_process_dies(board, monkeypatch):
    monkeypatch.setattr(bs.signal, "signal", lambda *a: None)  # only the main thread may
    board.apply({"khz": 299999, "cores": 2})
    owner_alive = [True]
    thread, result = guard_in_thread(board, owner_alive)
    thread.start()
    thread.join(0.1)
    assert thread.is_alive() and board.read(bs.SETSPEED) == "299999"  # still in use

    owner_alive[0] = False  # kill -9
    thread.join(2)

    assert result and "died" in result[0]
    assert board.read(bs.SETSPEED) == "1199999"
    assert [board.read(f"cpu{n}/online") for n in range(4)] == ["1"] * 4
    assert not board.state_file.exists()


def test_the_guardian_leaves_once_its_process_put_them_back(board, monkeypatch):
    monkeypatch.setattr(bs.signal, "signal", lambda *a: None)
    board.apply({"khz": 599999})
    thread, result = guard_in_thread(board, [True])
    thread.start()
    board.restore()
    thread.join(2)

    assert result == ["nothing to put back"]


# --- what run_board's tuning flags use ---------------------------------------------


@pytest.mark.parametrize("mhz, khz", [(300, 299999), (400, 399999), (600, 599999), (1200, 1199999)])
def test_the_cpu_clock_is_asked_for_in_mhz(board, mhz, khz):
    assert board.khz_for(mhz) == khz


def test_a_cpu_clock_the_board_has_not_got_names_the_ones_it_has(board):
    with pytest.raises(ValueError, match="300, 400, 600, 1200"):
        board.khz_for(500)


def test_tuning_shows_the_board_as_it_was_while_settings_are_applied(board, tmp_path):
    """The monitor and the client each keep their own state file; 'was' comes from both."""
    pl_owner = bs.Settings(board.cpu, tmp_path / "pl_state.json", clocks=board.clocks,
                           start_guardian=False, alive=lambda pid: 7)
    files = (board.state_file, pl_owner.state_file)
    assert bs.tuning(board, files) == {
        "now": {"cpu_mhz": 1200, "cores": 4, "pl_mhz": 100.0},
        "was": {"cpu_mhz": 1200, "cores": 4, "pl_mhz": 100.0}}

    board.apply({"khz": 299999, "cores": 1})
    pl_owner.apply({"pl": 25.0})

    assert bs.tuning(board, files) == {
        "now": {"cpu_mhz": 300, "cores": 1, "pl_mhz": 25.0},
        "was": {"cpu_mhz": 1200, "cores": 4, "pl_mhz": 100.0}}

    board.restore()  # the monitor stops; the client's PL clock stays until it stops too
    assert bs.tuning(board, files)["now"] == {"cpu_mhz": 1200, "cores": 4, "pl_mhz": 25.0}
    pl_owner.restore()
    assert board.clocks.fclk0_mhz == 100.0


def test_putting_back_one_owner_leaves_the_others_settings(board, tmp_path):
    pl_owner = bs.Settings(board.cpu, tmp_path / "pl_state.json", clocks=board.clocks,
                           start_guardian=False, alive=lambda pid: 7)
    pl_owner.apply({"pl": 50.0})
    board.apply({"cores": 2})
    board.restore()

    assert board.clocks.fclk0_mhz == 50.0 and pl_owner.state_file.exists()
