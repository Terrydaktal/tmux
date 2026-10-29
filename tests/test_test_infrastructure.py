from pathlib import Path
from types import SimpleNamespace

import pytest
import terminal_harness
from conftest import ROOT, mosh
from terminal_harness import Attachment, Emulator


def test_terminal_harness_is_local():
    assert Path(terminal_harness.__file__).resolve() == (
        ROOT / "tests/terminal_harness.py"
    )
    assert terminal_harness.ROOT == ROOT


def test_loopback_harness_is_local():
    assert Path(mosh.__file__).resolve() == ROOT / "tests/loopback_harness.py"


def test_termux_build_does_not_link_to_another_project():
    classes = ROOT / "build/termux/classes"
    assert not classes.is_symlink()
    assert classes.resolve().is_relative_to(ROOT / "build")
    assert (classes / "com/termux/terminal/Oracle.class").is_file()
    assert (ROOT / "tests/oracles/prepare_termux.py").is_file()
    assert (ROOT / "tests/oracles/java/com/termux/terminal/Oracle.java").is_file()


@pytest.mark.parametrize("kind", ["termux", "vte"])
def test_local_emulator_renders_terminal_output(kind):
    emulator = Emulator(kind, cols=83, rows=27)
    try:
        state = emulator.feed(b"\x1b[HLOCAL-TEST-HARNESS\r\n")
        state = emulator.until(lambda value: b"LOCAL-TEST-HARNESS" in value["screen"])
        assert b"LOCAL-TEST-HARNESS" in state["screen"]
    finally:
        emulator.close()



def test_emulator_waits_for_observable_state_not_a_fixed_delay():
    emulator = Emulator.__new__(Emulator)
    emulator.state = {"screen": b"pending"}
    queries = []

    def update(command):
        queries.append(command)
        emulator.state = {"screen": b"ready"}
        return emulator.state

    emulator.command = update
    assert emulator.until(lambda state: state["screen"] == b"ready")["screen"] == b"ready"
    assert queries == ["STATE"]


def test_quiet_pty_pump_observes_delayed_emulator_results():
    emulator = SimpleNamespace(state={"screen": b"pending", "replies": b""})

    def update(command):
        assert command == "STATE"
        emulator.state = {"screen": b"ready", "replies": b""}
        return emulator.state

    emulator.command = update
    attachment = Attachment.__new__(Attachment)
    attachment.term = emulator
    attachment.exited = False
    assert attachment.pump(duration=0)["screen"] == b"ready"
