"""The independent oracle must read VTE's retained ring after an ED 3 purge."""

import re

from terminal_harness import Emulator


def test_history_readout_after_repeated_purges():
    term = Emulator("vte", cols=80, rows=24)
    try:
        term.feed(b"old-history\r\n" * 100)
        for _ in range(3):
            term.feed(
                b"\033[H\033[2J\033[3J\033[H"
                + b"".join(f"retained-{i:04d}\r\n".encode() for i in range(80))
            )
            state = term.command("STATE")
            assert b"old-history" not in state["text"]
            assert list(
                map(int, re.findall(rb"retained-(\d{4})", state["text"]))
            ) == list(range(80))
    finally:
        term.close()
