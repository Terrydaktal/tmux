"""Synthetic program only: no shell startup, network, or user clipboard."""

import os
import signal
import sys
import tty
from pathlib import Path

tty.setraw(0)
log = Path(sys.argv[1]).open("ab", buffering=0)


def resized(*_):
    size = os.get_terminal_size(0)
    log.write(f"SIZE={size.columns}x{size.lines}\n".encode())


signal.signal(signal.SIGWINCH, resized)
resized()
os.write(1, b"\x1b[?2004hREADY\r\n")
while data := os.read(0, 4096):
    log.write(b"INPUT=" + data.hex().encode() + b"\n")
    if data == b"H":
        os.write(1, b"".join(f"history-{i:04d}\r\n".encode() for i in range(300)))
        os.write(1, b"HISTORY-END\r\n")
    elif data == b"B":
        os.write(
            1,
            (
                "\x1b[H\x1b[2Jprefix \u4e2d\u6587 one tail\r\n"
                "prefix \u4e2d\u6587 two tail\r\n"
                "prefix \u4e2d\u6587 three tail\r\nBOX-READY\r\n"
            ).encode(),
        )
    elif data == b"C":
        rows = []
        styles = ("\x1b[1;33m", "\x1b[3m", "\x1b[4m", "\x1b[36m")
        for index in range(600):
            text = f"ROW-{index:05d} " + "abcdefghij " * (index % 15)
            rows.append(f"{styles[index % len(styles)]}{text}\x1b[0m\r\n")
        os.write(1, ("".join(rows) + "COLORED-HISTORY-END\r\n").encode())
    elif data == b"A":
        os.write(1, b"\x1b[?1049h\x1b[?1002h\x1b[?1006h\x1b[HCLICK-HEADER")
    elif data == b"a":
        os.write(1, b"\x1b[?1002l\x1b[?1006l\x1b[?1049l")
    elif data == b"L":
        os.write(
            1,
            b"\x1b[H\x1b[2J\x1b]8;;file:///tmp/tmux-link%20test.txt\x1b\\"
            b"LINK-TARGET\x1b]8;;\x1b\\\r\nPLAIN-TEXT",
        )
    elif data == b"X":
        break
