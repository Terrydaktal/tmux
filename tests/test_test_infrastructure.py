from pathlib import Path

import pytest
import terminal_harness
from conftest import ROOT, mosh
from terminal_harness import Emulator


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
        assert b"LOCAL-TEST-HARNESS" in state["screen"]
    finally:
        emulator.close()
