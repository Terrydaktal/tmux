"""Synthetic scrollback records; never run shell startup or contact a network."""

import os
import signal
import sys
import tty
from pathlib import Path

tty.setraw(0)
style = sys.argv[2]


def resized(*_):
    size = os.get_terminal_size(0)
    log.write(f"SIZE={size.columns}x{size.lines}\n".encode())


def record(index):
    if style == "short":
        return f"record-{index:04d} short history"
    parts = "|".join(f"{index:04d}:{part:03d}" for part in range(90))
    if style == "unicode":
        return f"record-{index:04d} \u4e2d\u6587 e\u0301 {parts}"
    return f"record-{index:04d} {parts}"


with Path(sys.argv[1]).open("ab", buffering=0) as log:
    signal.signal(signal.SIGWINCH, resized)
    resized()
    os.write(1, b"READY\r\n")
    while data := os.read(0, 4096):
        log.write(b"INPUT=" + data.hex().encode() + b"\n")
        if data == b"H":
            os.write(1, b"".join((record(i) + "\r\n").encode() for i in range(350)))
            os.write(1, b"HISTORY-END\r\n")
        elif data == b"X":
            break
