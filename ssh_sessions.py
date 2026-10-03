"""Read-only discovery of this user's incoming OpenSSH shell/command sessions."""

import ipaddress
import os
from functools import cache
from pathlib import Path

from mosh_sessions import MOSH_SERVERS, foreground_app, process_tty

SSH_SERVERS = {"sshd", "sshd-session", "sshd-auth"}
ENV_LIMIT = 1024 * 1024


def peer_address(anchor, lookup, root=Path("/proc")):
    if anchor.uid != os.getuid():
        return None
    try:
        with (root / str(anchor.pid) / "environ").open("rb") as source:
            raw = source.read(ENV_LIMIT)
        current = lookup(anchor.pid)
        if (current.pid, current.started, current.uid) != (
            anchor.pid,
            anchor.started,
            anchor.uid,
        ):
            return None
        # Ignore incomplete records at the read limit; never expose other variables.
        values = [
            item.removeprefix(b"SSH_CONNECTION=").decode("ascii")
            for item in raw.split(b"\0")[:-1]
            if item.startswith(b"SSH_CONNECTION=")
        ]
        if len(values) != 1:
            return None
        peer, peer_port, host, host_port = values[0].split()
        ipaddress.ip_address(host)
        for port in (peer_port, host_port):
            if (
                not port.isascii()
                or not port.isdecimal()
                or not 1 <= int(port) <= 65535
            ):
                return None
        return str(ipaddress.ip_address(peer))
    except (OSError, ValueError, IndexError):
        return None


def discover(processes, clients, lookup, *, tty_lookup=process_tty, peer_lookup=None):
    processes = {
        pid: info
        for pid, info in processes.items()
        if info.uid == os.getuid() and info.state not in ("Z", "X")
    }

    @cache
    def parent(pid):
        if pid <= 1:
            return None
        try:
            return processes[pid] if pid in processes else lookup(pid)
        except (OSError, ValueError, IndexError):
            return None

    anchors = {}
    for info in processes.values():
        if info.executable in SSH_SERVERS | MOSH_SERVERS:
            continue
        owner = parent(info.parent)
        if (
            owner is not None
            and owner.executable in SSH_SERVERS
            and owner.state not in ("Z", "X")
            and owner.started <= info.started
        ):
            anchors[info.pid] = (info, owner)

    # An SSH connection can host several channels. Exclude only the login whose
    # descendants include a listed tmux client, not every login under that sshd.
    represented = set()
    for client in clients:
        if client.transport != "SSH":
            continue
        pid = client.pid
        visited = set()
        for _ in range(64):
            if pid in anchors:
                represented.add(pid)
                break
            if pid in visited:
                break
            visited.add(pid)
            info = parent(pid)
            if info is None or info.executable in SSH_SERVERS | MOSH_SERVERS:
                break
            pid = info.parent

    rows = []
    for pid, (anchor, owner) in sorted(anchors.items()):
        if pid in represented:
            continue
        app = (
            foreground_app(anchor, processes.values()) if anchor.tty_device else anchor
        )
        if app is not None and app.executable in MOSH_SERVERS:
            continue
        tty = tty_lookup(anchor)
        if tty is None and app is not None and app.pid != anchor.pid:
            tty = tty_lookup(app)
        if tty and any(
            client.transport == "SSH"
            and client.tty == tty
            and client.frontend_pid == owner.pid
            for client in clients
        ):
            continue
        peer = (
            peer_lookup(anchor)
            if peer_lookup is not None
            else peer_address(anchor, lookup)
        )
        rows.append(
            {
                "pid": anchor.pid,
                "started_ticks": anchor.started,
                "ssh_pid": owner.pid,
                "tty": tty,
                "mode": "UNKNOWN"
                if app is None
                else "OTHER-TMUX"
                if app.executable in ("tmux", "tmux-simple")
                else "DIRECT",
                "app": app.executable if app is not None else None,
                "app_pid": app.pid if app is not None else None,
                "process_state": "SUSPENDED"
                if (app or anchor).state in ("T", "t")
                else "RUNNING",
                "reachability": None,
                "peer": peer,
                "idle_seconds": None,
                "network_last_seen_at": None,
            }
        )
    return rows
