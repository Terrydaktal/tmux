"""Read-only discovery of this user's Mosh servers outside the tmux inventory."""

import fcntl
import os
import re
import stat
import struct
import termios
from collections import defaultdict, deque
from pathlib import Path

MOSH_SERVERS = {"mosh-server", "mosh-native-server"}


def read_processes(lookup, root=Path("/proc")):
    processes = {}
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                try:
                    if entry.stat().st_uid != os.getuid():
                        continue
                    info = lookup(int(entry.name))
                    if info.uid == os.getuid() and info.state not in ("Z", "X"):
                        processes[info.pid] = info
                except (OSError, ValueError, IndexError):
                    continue  # Process exit and restricted /proc entries are normal.
    except OSError as exc:
        return {}, f"Remote session discovery unavailable ({type(exc).__name__})"
    return processes, ""


def tty_matches(path, device):
    if not re.fullmatch(r"/dev/pts/[0-9]+", path) or not device:
        return False
    try:
        info = Path(path).stat()
        return stat.S_ISCHR(info.st_mode) and info.st_rdev == (device & 0xFFFFFFFF)
    except OSError:
        return False


def process_tty(info, root=Path("/proc")):
    for fd in (0, 1, 2):
        try:
            path = os.readlink(root / str(info.pid) / "fd" / str(fd))
        except OSError:
            continue
        if tty_matches(path, info.tty_device):
            return path
    return None


def terminal_size(tty, device=None):
    """Read an owned login PTY's columns/rows without input or a resize."""
    if not tty or not re.fullmatch(r"/dev/pts/[0-9]+", tty):
        return None, None
    fd = None
    try:
        fd = os.open(
            tty,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        info = os.fstat(fd)
        if (
            not stat.S_ISCHR(info.st_mode)
            or info.st_uid != os.getuid()
            or (device is not None and info.st_rdev != (device & 0xFFFFFFFF))
        ):
            return None, None
        rows, columns, _, _ = struct.unpack(
            "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        )
        return (columns, rows) if columns and rows else (None, None)
    except OSError:
        return None, None
    finally:
        if fd is not None:
            os.close(fd)


def foreground(server, children):
    pending = deque(children[server.pid])
    seen = {server.pid}
    descendants = []
    while pending:
        info = pending.popleft()
        if info.pid in seen or info.executable in MOSH_SERVERS:
            continue
        seen.add(info.pid)
        descendants.append(info)
        pending.extend(children[info.pid])
    # The nearest controlling terminal is Mosh's child PTY, not a nested app's PTY.
    anchor = next((info for info in descendants if info.tty_device), None)
    return anchor, foreground_app(anchor, descendants)


def foreground_app(anchor, processes):
    if anchor is None:
        return None
    group = [
        info
        for info in processes
        if info.tty_device
        and info.tty_device == anchor.tty_device
        and info.pgrp == anchor.foreground_pgrp
        and info.pgrp > 0
        and info.uid == os.getuid()
        and info.state not in ("Z", "X")
    ]
    group.sort(key=lambda info: (info.pid != info.pgrp, info.started, info.pid))
    return group[0] if group else None


def extra_sessions(processes, represented, logins, tty_lookup=process_tty):
    children = defaultdict(list)
    servers = []
    for info in processes.values():
        if info.uid != os.getuid() or info.state in ("Z", "X"):
            continue
        children[info.parent].append(info)
        if info.executable in MOSH_SERVERS and info.pid not in represented:
            servers.append(info)
    rows = []
    for server in sorted(servers, key=lambda info: info.pid):
        anchor, app = foreground(server, children)
        tty = tty_lookup(anchor) if anchor is not None else None
        if tty is None and anchor is not None:
            # Redirected stdin/stdout must not hide an otherwise verified login PTY.
            candidates = {
                path
                for pid, path in logins
                if pid == server.pid and tty_matches(path, anchor.tty_device)
            }
            if len(candidates) == 1:
                tty = candidates.pop()
        state, peer = logins.get((server.pid, tty), ("UNKNOWN", "-"))
        rows.append(
            {
                "pid": server.pid,
                "started_ticks": server.started,
                "tty": tty,
                "mode": "UNKNOWN"
                if app is None
                else "OTHER-TMUX"
                if app.executable in ("tmux", "tmux-simple")
                else "DIRECT",
                "app": app.executable if app is not None else None,
                "app_pid": app.pid if app is not None else None,
                "process_state": "SUSPENDED"
                if server.state in ("T", "t")
                else "RUNNING",
                "reachability": "RECENT" if state == "CONNECTED" else state,
                "peer": peer,
                "idle_seconds": None,
                "network_last_seen_at": None,
                "network_last_rx_monotonic_ms": None,
            }
        )
    return rows


def discover(clients, lookup, read_logins, *, snapshot=None):
    processes, note = read_processes(lookup) if snapshot is None else snapshot
    represented = {
        client.frontend_pid for client in clients if client.transport == "MOSH"
    }
    if not any(
        info.executable in MOSH_SERVERS and info.pid not in represented
        for info in processes.values()
    ):
        return [], note
    logins, login_note = read_logins()
    rows = extra_sessions(processes, represented, logins)
    return rows, "; ".join(filter(None, (note, login_note)))
