"""Linux client inventory and reversible sizing for disconnected stock Mosh peers."""

import argparse
import fcntl
import json
import os
import re
import select
import signal
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

OWNED = "@tmux-simple-offline-clients"
WATCHER = "@tmux-simple-sizing-monitor"
CLIENT_FORMAT = "\t".join(
    "#{" + name + "}"
    for name in (
        "client_pid",
        "client_tty",
        "session_name",
        "client_width",
        "client_height",
        "client_flags",
    )
)


class MonitorError(RuntimeError):
    pass


class ClientChanged(MonitorError):
    pass


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    started: int
    executable: str
    uid: int
    state: str = ""


def process(pid, root=Path("/proc")):
    directory = root / str(pid)
    raw = (directory / "stat").read_text()
    fields = raw.rsplit(")", 1)[1].split()
    # Fall back to comm for inaccessible ancestors (not their environment).
    name = raw.split("(", 1)[1].rsplit(")", 1)[0].split(":", 1)[0]
    try:
        name = Path(os.readlink(directory / "exe")).name.removesuffix(" (deleted)")
    except OSError:
        pass
    return Process(
        pid, int(fields[1]), int(fields[19]), name, directory.stat().st_uid, fields[0]
    )


def transport(pid, lookup=process):
    visited = set()
    try:
        for _ in range(64):
            if pid <= 1:
                return "LOCAL", None
            if pid in visited:
                break
            visited.add(pid)
            info = lookup(pid)
            if info.executable in ("mosh-server", "mosh-native-server"):
                if info.uid == os.getuid():
                    return "MOSH", info.pid
                break
            if info.executable in ("sshd", "sshd-session"):
                return "SSH", None
            pid = info.parent
    except (OSError, ValueError, IndexError):
        pass
    return "UNKNOWN", None


def parse_logins(output):
    records = {}
    for line in output.splitlines():
        match = re.search(r"\((?:(\S+) via )?mosh \[([0-9]+)\]\)\s*$", line)
        fields = line.split()
        if not match or len(fields) < 2:
            continue
        key = (int(match[2]), "/dev/" + fields[1])
        record = ("CONNECTED", match[1]) if match[1] else ("UNREACHABLE", "-")
        # Ambiguous or transient duplicate records must not assert disconnection.
        records[key] = record if key not in records else ("UNKNOWN", "-")
    return records


def login_records():
    env = {**os.environ, "LC_ALL": "C"}
    try:
        result = subprocess.run(
            ["who", "-u"],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=True,
        )
        return parse_logins(result.stdout), ""
    except (OSError, subprocess.SubprocessError) as exc:
        return {}, f"Mosh login records unavailable ({type(exc).__name__})"


@dataclass
class Client:
    pid: int
    tty: str
    session: str
    width: int
    height: int
    flags: set
    identity: str = ""
    transport: str = "UNKNOWN"
    state: str = "UNKNOWN"
    peer: str = "-"


def describe(clients, lookup=process, read_logins=login_records):
    records = None
    note = ""
    for client in clients:
        try:
            info = lookup(client.pid)
            if info.uid != os.getuid():
                continue
            client.identity = f"{client.pid}:{info.started}:{client.tty}"
        except (OSError, ValueError, IndexError):
            continue
        client.transport, mosh_pid = transport(client.pid, lookup)
        if client.transport == "MOSH":
            if records is None:
                records, note = read_logins()
            client.state, client.peer = records.get(
                (mosh_pid, client.tty), ("UNKNOWN", "-")
            )
            if client.state == "UNKNOWN" and not note:
                note = "No unambiguous Mosh login record for one or more clients; unknown is not treated as offline."
        elif client.transport != "UNKNOWN":
            client.state = "ATTACHED"
    return clients, note


class Server:
    def __init__(self, binary, socket, env=None):
        self.binary = str(binary)
        self.socket = Path(socket)
        self.env = env
        self.identity = None
        self.watcher = ""

    def run(self, *args):
        result = subprocess.run(
            [self.binary, "-N", "-S", str(self.socket), *args],
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if result.returncode:
            raise MonitorError(result.stderr.strip() or "tmux server unavailable")
        return result.stdout

    def snapshot(self):
        output = self.run(
            "display-message",
            "-p",
            f"#{{pid}}\t#{{@tmux-simple}}\t#{{{OWNED}}}\t#{{{WATCHER}}}",
            ";",
            "list-clients",
            "-F",
            CLIENT_FORMAT,
        ).splitlines()
        try:
            pid, marker, owned, self.watcher = output[0].split("\t")
            info = process(int(pid))
            identity = (info.pid, info.started)
            if marker != "1" or info.uid != os.getuid():
                raise MonitorError("refusing to manage a foreign tmux server")
            if self.identity is not None and identity != self.identity:
                raise MonitorError("tmux server was replaced")
            self.identity = identity
            owned = json.loads(owned or "[]")
            if not isinstance(owned, list) or not all(
                isinstance(key, str)
                and re.fullmatch(r"[0-9]+:[0-9]+:/dev/[A-Za-z0-9/_-]+", key)
                for key in owned
            ):
                raise ValueError("invalid ownership record")
            clients = []
            for line in output[1:]:
                pid, tty, session, width, height, flags = line.split("\t")
                if not re.fullmatch(r"/dev/[A-Za-z0-9/_-]+", tty):
                    continue  # Control-mode clients have no normal terminal size.
                clients.append(
                    Client(
                        int(pid),
                        tty,
                        session,
                        int(width),
                        int(height),
                        set(flags.split(",")),
                    )
                )
            return clients, set(owned)
        except (IndexError, ValueError, OSError) as exc:
            raise MonitorError("invalid or unavailable tmux client inventory") from exc

    def set_option(self, name, value):
        # Guard the queued write against a replacement server on the same socket.
        command = "set-option -g " + name + " " + quote(value)
        self.run("if-shell", "-F", self.guard(), command)

    def guard(self):
        return (
            f"#{{&&:#{{==:#{{pid}},{self.identity[0]}}},#{{==:#{{@tmux-simple}},1}}}}"
        )

    def save_owned(self, owned):
        # Setting this private metadata option also makes the pinned tmux backend
        # recalculate sizes; refresh-client -f alone only changes the flags.
        self.set_option(OWNED, json.dumps(sorted(owned), separators=(",", ":")))

    def flag(self, client, ignored):
        try:
            current = process(client.pid)
        except (OSError, ValueError, IndexError) as exc:
            raise ClientChanged("client disappeared during sizing update") from exc
        if client.identity != f"{client.pid}:{current.started}:{client.tty}":
            raise ClientChanged("client process changed during sizing update")
        command = f"refresh-client -t {client.tty} -f {'ignore-size' if ignored else '!ignore-size'}"
        # Check the exact PID/TTY pair inside the server queue as well, so an
        # attachment that vanished cannot redirect this update to a reused TTY.
        match = (
            "#{L:#{?#{&&:#{==:#{client_pid},"
            + str(client.pid)
            + "},#{==:#{client_tty},"
            + client.tty
            + "}},1,}}"
        )
        self.run("if-shell", "-F", "#{&&:" + self.guard() + "," + match + "}", command)


def quote(value):
    # tmux command strings, not shell programs; quote its metacharacters too.
    return "'" + value.replace("'", "'\\''") + "'"


class SizeMonitor:
    def __init__(self, server, lookup=process, read_logins=login_records):
        self.server = server
        self.lookup = lookup
        self.read_logins = read_logins
        self.initial = True

    def step(self, restore=False):
        clients, owned = self.server.snapshot()
        clients, note = describe(clients, self.lookup, self.read_logins)
        present = {(str(client.pid), client.tty): client for client in clients}
        retained = set()
        for key in owned:
            pid, _, tty = key.split(":", 2)
            client = present.get((pid, tty))
            if client is not None and (not client.identity or client.identity == key):
                retained.add(key)
        changes = []
        for client in clients:
            if not client.identity:
                continue
            ignored = "ignore-size" in client.flags
            offline = (
                not restore
                and client.transport == "MOSH"
                and client.state == "UNREACHABLE"
            )
            if offline and not ignored:
                retained.add(client.identity)
                changes.append((client, True))
            elif not offline and client.identity in retained:
                if ignored:
                    changes.append((client, False))
                retained.remove(client.identity)
        if changes or retained != owned or self.initial:
            # Persist new claims before setting flags so a replacement monitor
            # can restore them after a crash; release claims only after clearing.
            self.server.save_owned(owned | retained)
            for client, ignored in changes:
                try:
                    self.server.flag(client, ignored)
                except ClientChanged:
                    # Preserve the claim until the next inventory proves whether
                    # it belongs to a departed, re-used, or temporarily hidden PID.
                    if client.identity in owned:
                        retained.add(client.identity)
            self.server.save_owned(retained)
        self.initial = False
        return clients, note


def private_file(path, flags):
    info = path.parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise MonitorError("unsafe monitor directory")
    fd = os.open(
        path, flags | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600
    )
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        os.close(fd)
        raise MonitorError("unsafe monitor file")
    return fd


def start_monitor(server):
    server.snapshot()
    fd = private_file(server.socket.with_suffix(".sizing.lock"), os.O_RDWR)
    log = None
    pipe = None
    child = None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        log = private_file(
            server.socket.with_suffix(".sizing.log"), os.O_WRONLY | os.O_APPEND
        )
        if os.fstat(log).st_size > 65536:
            os.ftruncate(log, 0)
        pipe = os.pipe()
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--tmux",
                server.binary,
                "--socket",
                str(server.socket),
                "--lock-fd",
                str(fd),
                "--server-id",
                ":".join(map(str, server.identity)),
                "--ready-fd",
                str(pipe[1]),
            ],
            env=server.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            pass_fds=(fd, pipe[1]),
            start_new_session=True,
        )
        os.close(pipe[1])
        pipe = (pipe[0], -1)
        if not select.select([pipe[0]], [], [], 5)[0] or os.read(pipe[0], 1) != b"1":
            raise MonitorError(
                f"sizing monitor failed to start; see {server.socket.with_suffix('.sizing.log')}"
            )
        return True
    except BaseException:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        raise
    finally:
        if pipe is not None:
            for pipe_fd in pipe:
                if pipe_fd >= 0:
                    os.close(pipe_fd)
        if log is not None:
            os.close(log)
        os.close(fd)


def maybe_start_monitor(server):
    if transport(os.getpid())[0] == "MOSH":
        start_monitor(server)
    else:
        clients, _ = server.snapshot()
        if any(transport(client.pid)[0] == "MOSH" for client in clients):
            start_monitor(server)


def watcher_alive(identity):
    try:
        pid, started = map(int, identity.split(":"))
        info = process(pid)
        return info.started == started and info.state not in ("Z", "X")
    except (OSError, ValueError, IndexError):
        return False


def show_clients(server, as_json=False):
    clients, owned = server.snapshot()
    clients, note = describe(clients)
    rows = [
        dict(
            session=client.session,
            pid=client.pid,
            tty=client.tty,
            transport=client.transport,
            state=client.state,
            peer=client.peer,
            width=client.width,
            height=client.height,
            sizing=(
                "ignored-auto"
                if client.identity in owned
                else "ignored-manual"
                if client.identity
                else "ignored-unknown"
            )
            if "ignore-size" in client.flags
            else "participating",
        )
        for client in clients
    ]
    running = watcher_alive(server.watcher)
    if as_json:
        print(json.dumps({"monitor_running": running, "clients": rows, "note": note}))
        return
    print("Sizing monitor: " + ("running" if running else "not running"))
    table = [["SESSION", "CLIENT", "TRANSPORT", "STATE", "PEER", "SIZE", "SIZING"]]
    for row in rows:
        table.append(
            [
                row["session"],
                row["tty"],
                row["transport"],
                row["state"],
                row["peer"],
                f"{row['width']}x{row['height']}",
                row["sizing"],
            ]
        )
    table = [
        [
            "".join(char if char.isprintable() else "?" for char in value)
            for value in row
        ]
        for row in table
    ]
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    for row in table:
        print(
            "  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip()
        )
    if note:
        print(note)
    print(
        "Mosh status is coarse (about 30s detection), not keyboard idle time or app visibility."
    )


def watch(server, stop, interval=2.0, ready_fd=None):
    monitor = SizeMonitor(server)
    server.snapshot()
    me = process(os.getpid())
    server.set_option(WATCHER, f"{me.pid}:{me.started}")
    if ready_fd is not None:
        os.write(ready_fd, b"1")
        os.close(ready_fd)
    try:
        while not stop.is_set():
            clients, _ = monitor.step()
            has_mosh = any(client.transport == "MOSH" for client in clients)
            stop.wait(interval if has_mosh else max(interval, 5.0))
    finally:
        try:
            monitor.step(restore=True)
            server.set_option(WATCHER, "")
        except (MonitorError, OSError, subprocess.SubprocessError):
            pass  # Server exit/replacement must never start or alter a new server.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tmux", required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--lock-fd", type=int, required=True)
    parser.add_argument("--server-id", required=True)
    parser.add_argument("--ready-fd", type=int, required=True)
    args = parser.parse_args()
    # This FD is deliberately inherited across the detached helper's exec.
    lock_info = os.fstat(args.lock_fd)
    path_info = args.socket.with_suffix(".sizing.lock").lstat()
    if (lock_info.st_dev, lock_info.st_ino) != (path_info.st_dev, path_info.st_ino):
        raise MonitorError("monitor lock changed during startup")
    fcntl.flock(args.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, lambda *_: stop.set())
    server = Server(args.tmux, args.socket)
    server.identity = tuple(map(int, args.server_id.split(":")))
    watch(server, stop, ready_fd=args.ready_fd)


if __name__ == "__main__":
    try:
        main()
    except (MonitorError, OSError, subprocess.SubprocessError) as exc:
        print(f"tmux-simple sizing monitor: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
