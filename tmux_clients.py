"""Linux Mosh inventory, explicit cleanup and reversible tmux sizing."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import mosh_cleanup
import mosh_sessions
import mosh_status
import opsec_sessions
import ssh_sessions

PROJECT_ROOT = Path(__file__).resolve().parent
FORK_BINARIES = {
    "tmux": PROJECT_ROOT / "build/runtime/tmux",
    "mosh-server": PROJECT_ROOT / "build/runtime/mosh-server",
    "mosh-native-server": PROJECT_ROOT.parent
    / "mosh-native/build/runtime/mosh-native-server",
}

OWNED = "@tmux-simple-offline-clients"
WATCHER = "@tmux-simple-sizing-monitor"
CLIENT_FORMAT = "CLIENT\t" + "\t".join(
    "#{" + name + "}"
    for name in (
        "client_pid",
        "client_tty",
        "session_name",
        "client_width",
        "client_height",
        "client_flags",
        "session_id",
        "client_created",
        "client_activity",
        "pane_pid",
        "window_id",
        "window-size",
        "window_size_client_pid",
    )
)
SESSION_FORMAT = "SESSION\t" + "\t".join(
    "#{" + name + "}"
    for name in (
        "session_id",
        "session_name",
        "session_attached",
        "session_created",
        "session_activity",
        "pane_pid",
    )
)
TERMINALS = {
    "xfce4-terminal",
    "konsole",
    "xterm",
    "gnome-terminal-server",
    "gnome-terminal",
    "kgx",
    "ptyxis",
    "foot",
    "footclient",
    "kitty",
    "alacritty",
    "wezterm-gui",
    "lxterminal",
    "qterminal",
    "st",
    "urxvt",
    "rxvt",
    "login",
    "agetty",
    "getty",
}
MAX_COMMAND_BYTES = 65536
PI_RUNTIMES = {"node", "bun", "pi"}
SHELLS = {"bash", "sh", "dash", "fish", "zsh"}
MAX_BINARY_BYTES = 64 * 1024 * 1024


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
    pgrp: int = 0
    tty_device: int = 0
    foreground_pgrp: int = 0


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
        pid,
        int(fields[1]),
        int(fields[19]),
        name,
        directory.stat().st_uid,
        fields[0],
        int(fields[2]),
        int(fields[4]),
        int(fields[5]),
    )


def process_command(info, lookup=process, root=Path("/proc")):
    """Read current argv only while the foreground process identity still matches."""
    if info is None or info.uid != os.getuid() or info.state in ("Z", "X"):
        return None

    def matches(current):
        return (
            current.pid == info.pid
            and current.started == info.started
            and current.uid == info.uid
            and current.executable == info.executable
            and current.state not in ("Z", "X")
        )

    try:
        if not matches(lookup(info.pid)):
            return None
        with (root / str(info.pid) / "cmdline").open("rb") as source:
            raw = source.read(MAX_COMMAND_BYTES + 1)
        if not raw or len(raw) > MAX_COMMAND_BYTES or not matches(lookup(info.pid)):
            return None
    except (OSError, ValueError, IndexError):
        return None
    argv = raw.split(b"\0")
    if argv[-1] == b"":
        argv.pop()
    return shlex.join(arg.decode("utf-8", errors="replace") for arg in argv)


def process_binary_mtime(info, lookup=process, root=Path("/proc")):
    """Stat the running executable, not its potentially replaced installed path."""
    if info is None or info.uid != os.getuid() or info.state in ("Z", "X"):
        return None

    def matches(current):
        return (
            current.pid == info.pid
            and current.started == info.started
            and current.uid == info.uid
            and current.executable == info.executable
            and current.state not in ("Z", "X")
        )

    try:
        if not matches(lookup(info.pid)):
            return None
        executable = root / str(info.pid) / "exe"
        before = executable.stat()
        if not matches(lookup(info.pid)):
            return None
        after = executable.stat()
        if (
            (before.st_dev, before.st_ino, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_mtime_ns)
            or not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_mtime < 253402300800
        ):
            return None
        return before.st_mtime
    except (OSError, ValueError, IndexError):
        return None


def binary_fingerprint(path, fingerprints):
    def key(info):
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    try:
        before = path.stat()
        identity = key(before)
        if (
            not stat.S_ISREG(before.st_mode)
            or not 4 <= before.st_size <= MAX_BINARY_BYTES
        ):
            return None
        if identity in fingerprints:
            return fingerprints[identity]
        with path.open("rb") as source:
            if key(os.fstat(source.fileno())) != identity:
                return None
            magic = source.read(4)
            if magic != b"\x7fELF":
                return None
            digest = hashlib.sha256(magic)
            remaining = MAX_BINARY_BYTES - 4
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            if source.read(1) or key(os.fstat(source.fileno())) != identity:
                return None
        if key(path.stat()) != identity:
            return None
        fingerprints[identity] = digest.hexdigest()
        return fingerprints[identity]
    except OSError:
        return None


def installed_binary(name):
    if name not in {"tmux", "xfce4-terminal", *mosh_sessions.MOSH_SERVERS}:
        return None
    try:
        # PATH and user command aliases must not make a distro build "current".
        if name in FORK_BINARIES:
            return FORK_BINARIES[name].resolve(strict=True)
        preferred = Path.home() / ".local/bin" / name
        target = str(preferred) if preferred.is_file() else shutil.which(name)
        if not target:
            return None
        path = Path(target).resolve(strict=True)
        if name == "xfce4-terminal" and path.name == "run-patched-xfce4-terminal.sh":
            path = path.parent / "build/terminal/xfce4-terminal"
        return path
    except (OSError, RuntimeError):
        return None


def latest_binary(name, fingerprints):
    installed = installed_binary(name)
    if installed is None or name not in mosh_sessions.MOSH_SERVERS:
        return installed
    if installed.parts[-3:] != ("src", "frontend", name):
        return installed
    release = installed.parents[2]
    prefix = "mosh-release." if name == "mosh-server" else "release."
    if release.parent.name != "build" or not release.name.startswith(prefix):
        return installed

    # A successful build can be newer than the deliberately preserved install link.
    # Stay in this variant's release directory; native and standard are not substitutes.
    try:
        installed_time = installed.stat().st_mtime_ns
        candidates = []
        releases = 0
        for directory in release.parent.iterdir():
            if not directory.name.startswith(prefix) or directory.is_symlink():
                continue
            releases += 1
            if releases > 128:
                return installed
            try:
                if not (directory / "BUILD-SHA256SUMS").is_file():
                    continue
                candidate = directory / "src/frontend" / name
                info = candidate.stat()
                if (
                    stat.S_ISREG(info.st_mode)
                    and info.st_mode & 0o111
                    and info.st_uid == os.getuid()
                    and info.st_mtime_ns > installed_time
                ):
                    candidates.append((info.st_mtime_ns, candidate))
            except OSError:
                continue
        for _, candidate in sorted(candidates, reverse=True):
            if binary_fingerprint(candidate, fingerprints) is not None:
                return candidate
    except OSError:
        pass
    return installed


def process_binary_outdated(
    info, installed, fingerprints, lookup=process, root=Path("/proc")
):
    if (
        info is None
        or installed is None
        or info.uid != os.getuid()
        or info.state in ("Z", "X")
    ):
        return None

    def matches(current):
        return (
            current.pid == info.pid
            and current.started == info.started
            and current.uid == info.uid
            and current.executable == info.executable
            and current.state not in ("Z", "X")
        )

    try:
        if not matches(lookup(info.pid)):
            return None
        running = binary_fingerprint(root / str(info.pid) / "exe", fingerprints)
        expected = binary_fingerprint(installed, fingerprints)
        if not matches(lookup(info.pid)):
            return None
        return (
            running != expected
            if running is not None and expected is not None
            else None
        )
    except (OSError, ValueError, IndexError):
        return None


def is_pi_program(info, command):
    if info is None or info.executable not in PI_RUNTIMES or not command:
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return bool(argv) and (
        Path(argv[0]).name == "pi"
        or (
            len(argv) > 1
            and Path(argv[1]).parts[-4:]
            == ("@mariozechner", "pi-coding-agent", "dist", "cli.js")
        )
    )


def foreground_pi(app, processes, read_command):
    """Prefer a verified Pi child over its waiting shell, not arbitrary helpers."""
    if (
        app is None
        or app.executable not in SHELLS
        or not app.tty_device
        or app.uid != os.getuid()
        or app.state in ("Z", "X")
        or app.pgrp <= 0
        or app.pgrp != app.foreground_pgrp
    ):
        return app
    candidates = []
    for info in processes.values():
        if (
            info.pid == app.pid
            or info.executable not in PI_RUNTIMES
            or info.uid != os.getuid()
            or info.state in ("Z", "X")
            or info.tty_device != app.tty_device
            or info.pgrp != app.pgrp
            or info.pgrp != app.foreground_pgrp
            or info.started < app.started
        ):
            continue
        current, seen = info, set()
        for _ in range(64):
            if current.pid == app.pid:
                if is_pi_program(info, read_command(info.pid)):
                    candidates.append(info)
                break
            if current.pid in seen:
                break
            seen.add(current.pid)
            parent = processes.get(current.parent)
            if (
                parent is None
                or parent.uid != os.getuid()
                or parent.state in ("Z", "X")
                or parent.started > current.started
            ):
                break
            current = parent
    return candidates[0] if len(candidates) == 1 else app


def connection_origin(pid, lookup=process):
    visited = set()
    try:
        for _ in range(64):
            if pid <= 1:
                break
            if pid in visited:
                break
            visited.add(pid)
            info = lookup(pid)
            if info.executable in mosh_sessions.MOSH_SERVERS:
                if info.uid == os.getuid():
                    return "MOSH", info
                break
            if info.executable in ssh_sessions.SSH_SERVERS:
                return "SSH", info
            if info.executable in TERMINALS:
                return "LOCAL", info
            pid = info.parent
    except (OSError, ValueError, IndexError):
        pass
    return "UNKNOWN", None


def transport(pid, lookup=process):
    kind, origin = connection_origin(pid, lookup)
    return kind, origin.pid if kind == "MOSH" else None


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


def timestamp(value):
    return int(value) if value and int(value) > 0 else None


def activity_age(value, now):
    return max(0, int(now) - value) if value is not None else None


def format_age(seconds):
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02}s"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02}m"
    return f"{seconds // 86400}d{seconds % 86400 // 3600:02}h"


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
    session_id: str = ""
    created: int | None = None
    activity: int | None = None
    attachment: str = "UNKNOWN"
    frontend: str = ""
    frontend_pid: int | None = None
    pane_pid: int | None = None
    window_id: str = ""
    sizing_policy: str = ""
    sizing_client_pid: int | None = None

    @property
    def active_sizing(self):
        # Empty means an older server; zero means the server reports no owner.
        if self.sizing_client_pid is None:
            return None
        return self.sizing_client_pid > 0 and self.sizing_client_pid == self.pid


@dataclass
class Session:
    identity: str
    name: str
    attached: int
    created: int | None
    activity: int | None
    pane_pid: int | None = None


def describe(clients, lookup=process, read_logins=login_records):
    records = None
    note = ""
    for client in clients:
        try:
            info = lookup(client.pid)
            if info.uid != os.getuid():
                continue
            if info.state in ("Z", "X"):
                client.attachment = "EXITED"
                continue
            client.identity = f"{client.pid}:{info.started}:{client.tty}"
        except FileNotFoundError:
            client.attachment = "EXITED"
            continue
        except (OSError, ValueError, IndexError):
            continue
        client.attachment = (
            "SUSPENDED"
            if info.state in ("T", "t") or "suspended" in client.flags
            else "ATTACHED"
        )
        client.transport, origin = connection_origin(client.pid, lookup)
        if origin is not None:
            client.frontend, client.frontend_pid = origin.executable, origin.pid
        if client.transport == "MOSH":
            if records is None:
                records, note = read_logins()
            client.state, client.peer = records.get(
                (origin.pid, client.tty), ("UNKNOWN", "-")
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
        self.sessions = []

    def run(self, *args):
        result = subprocess.run(
            [self.binary, "-N", "-S", str(self.socket), *args],
            check=False,
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

    def snapshot(self, include_control=False):
        output = self.run(
            "display-message",
            "-p",
            f"#{{pid}}\t#{{@tmux-simple}}\t#{{{OWNED}}}\t#{{{WATCHER}}}",
            ";",
            "list-sessions",
            "-F",
            SESSION_FORMAT,
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
            sessions = []
            for line in output[1:]:
                kind, *fields = line.split("\t")
                if kind == "SESSION":
                    identity, name, attached, created, activity, pane_pid = fields
                    sessions.append(
                        Session(
                            identity,
                            name,
                            int(attached),
                            timestamp(created),
                            timestamp(activity),
                            int(pane_pid) if pane_pid else None,
                        )
                    )
                    continue
                if kind != "CLIENT":
                    raise ValueError("unexpected inventory record")
                (
                    pid,
                    tty,
                    session,
                    width,
                    height,
                    flags,
                    session_id,
                    created,
                    activity,
                    pane_pid,
                    window_id,
                    sizing_policy,
                    sizing_client_pid,
                ) = fields
                if not include_control and not re.fullmatch(
                    r"/dev/[A-Za-z0-9/_-]+", tty
                ):
                    continue  # Control-mode clients have no normal terminal size.
                clients.append(
                    Client(
                        int(pid),
                        tty,
                        session,
                        int(width or 0),
                        int(height or 0),
                        set(flags.split(",")),
                        session_id=session_id,
                        created=timestamp(created),
                        activity=timestamp(activity),
                        pane_pid=int(pane_pid) if pane_pid else None,
                        window_id=window_id,
                        sizing_policy=sizing_policy,
                        sizing_client_pid=int(sizing_client_pid)
                        if sizing_client_pid
                        else None,
                    )
                )
            self.sessions = sessions
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


def default_socket():
    inherited = os.environ.get("TMUX", "").rsplit(",", 2)
    if (
        len(inherited) == 3
        and Path(inherited[0]).is_absolute()
        and inherited[1].isdigit()
        and inherited[2].isdigit()
    ):
        return Path(inherited[0])
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    if not base.is_absolute():
        base = Path("/tmp")
    return base / f"tmux-simple-{os.getuid()}" / "server.sock"


def public_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-S", "--socket", type=Path, default=default_socket())
    parser.add_argument(
        "--tmux",
        type=Path,
        default=Path(__file__).resolve().parent / "build/runtime/tmux",
    )
    commands = parser.add_subparsers(dest="action", required=True)
    inventory = commands.add_parser(
        "clients", help="show sessions, attachments, activity and Mosh sizing"
    )
    inventory.add_argument("--json", action="store_true")
    inventory.add_argument(
        "--once", action="store_true", help="print an expanded session-tree snapshot"
    )
    inventory.add_argument(
        "--flat", action="store_true", help="print the previous flat-table snapshot"
    )
    inventory.add_argument(
        "--verbose",
        action="store_true",
        help="accepted for compatibility; uses the same columns as the default view",
    )
    cleanup = commands.add_parser(
        "cleanup",
        help="close this user's Mosh sessions not heard from for 30 seconds",
        description=(
            "Close Mosh servers with verified last-packet age at or above the cutoff. "
            "Servers without receive timestamps are skipped unless --include-legacy "
            "is requested. " + mosh_cleanup.WARNING
        ),
    )
    cleanup.add_argument(
        "--older-than",
        type=int,
        default=30,
        metavar="SECONDS",
        help="last-packet age cutoff in seconds (default: 30; positive integer)",
    )
    cleanup.add_argument(
        "--include-legacy",
        action="store_true",
        help="also close legacy servers marked UNREACHABLE; only valid with the 30s cutoff",
    )
    cleanup.add_argument(
        "--dry-run", action="store_true", help="preview without sending signals"
    )
    cleanup.add_argument(
        "--json", action="store_true", help="print machine-readable results"
    )
    commands.add_parser("ensure", help="start monitoring if a Mosh client is attached")
    commands.add_parser("start", help="start the singleton monitor explicitly")
    args = parser.parse_args()
    if args.action == "cleanup":
        if args.older_than <= 0:
            cleanup.error("--older-than must be a positive integer")
        if args.include_legacy and args.older_than != 30:
            cleanup.error("--include-legacy supports only the default 30-second cutoff")
        if not args.json:
            print(mosh_cleanup.WARNING, flush=True)
            if args.include_legacy:
                print(mosh_cleanup.LEGACY_WARNING, flush=True)
        result = mosh_cleanup.cleanup(
            process,
            older_than=args.older_than,
            dry_run=args.dry_run,
            include_legacy=args.include_legacy,
            read_logins=login_records,
        )
        show_cleanup(result, args.json)
        return int(bool(result["discovery_error"] or result["summary"]["error"]))
    server = Server(args.tmux.absolute(), args.socket.absolute())
    if args.action == "clients":
        try:
            show_clients(
                server, args.json, verbose=args.verbose, once=args.once, flat=args.flat
            )
        except KeyboardInterrupt:
            return 130
        return 0
    if not server.socket.exists():
        if args.action == "start":
            raise MonitorError("no tmux server is running at this socket")
        return 0
    if args.action == "ensure":
        maybe_start_monitor(server)
    else:
        start_monitor(server)
    return 0


def inventory(server, now=None):
    now = time.time() if now is None else now
    server_running = server.socket.exists()
    clients, owned = (
        server.snapshot(include_control=True) if server_running else ([], set())
    )
    read_logins = cache(login_records)
    clients, note = describe(clients, lookup=process, read_logins=read_logins)
    if any(client.tty and client.sizing_client_pid is None for client in clients):
        note = "; ".join(
            filter(
                None,
                (
                    note,
                    (
                        "Active sizing unavailable on this older tmux server; "
                        "a new server using the rebuilt binary is required. "
                        "Existing sessions have not been restarted."
                    ),
                ),
            )
        )
    process_snapshot = mosh_sessions.read_processes(process)
    other_mosh, mosh_note = mosh_sessions.discover(
        clients, process, read_logins, snapshot=process_snapshot
    )
    note = "; ".join(dict.fromkeys(filter(None, (note, mosh_note))))
    processes, _ = process_snapshot
    other_ssh = ssh_sessions.discover(processes, clients, process)

    @cache
    def binary_mtime(pid):
        return process_binary_mtime(processes.get(pid), process)

    fingerprints = {}
    reference_binary = cache(lambda name: latest_binary(name, fingerprints))

    @cache
    def binary_outdated(pid):
        info = processes.get(pid)
        return process_binary_outdated(
            info,
            reference_binary(info.executable) if info else None,
            fingerprints,
            process,
        )

    server_binary_mtime = None
    server_binary_outdated = None
    if server_running and server.identity:
        owner = processes.get(server.identity[0])
        if owner and (owner.pid, owner.started) == server.identity:
            server_binary_mtime = binary_mtime(owner.pid)
            server_binary_outdated = binary_outdated(owner.pid)

    @cache
    def last_contact(pid):
        try:
            owner = processes.get(pid) or process(pid)
        except (OSError, ValueError, IndexError):
            return {}
        return mosh_status.read(owner, process) or {}

    for row in other_mosh:
        row.update(last_contact(row["pid"]))
        row["frontend_binary_mtime"] = binary_mtime(row["pid"])
        row["frontend_outdated"] = binary_outdated(row["pid"])

    @cache
    def app_command(pid):
        return process_command(processes.get(pid), process)

    opsec = opsec_sessions.discover(processes, app_command)
    names, opsec_note = opsec_sessions.resolve_sessions(opsec)
    note = "; ".join(dict.fromkeys(filter(None, (note, opsec_note))))
    for app_pid, connection in opsec.items():
        connection.update(names.get(app_pid, {}))

    @cache
    def app_details(pid):
        app = processes.get(pid)
        connection = opsec.get(pid)
        if not connection:
            app = foreground_pi(app, processes, app_command)
        command = app_command(app.pid if app else None)
        label = "pi" if is_pi_program(app, command) else None
        if connection and (
            connection.get("action") == "pi"
            or (
                not connection.get("action")
                and (connection.get("session") or "").startswith("pi-")
            )
        ):
            label = "pi-opsec"
        return {
            "app": app.executable if app else None,
            "app_label": label,
            "app_pid": app.pid if app else None,
            "command": command,
        }

    read_size = cache(mosh_sessions.terminal_size)
    for row in other_mosh + other_ssh:
        original_pid = row["app_pid"]
        row.update(app_details(original_pid))
        app = processes.get(row["app_pid"])
        if original_pid in opsec:
            row["mode"] = "OPSEC-TMUX"
            row["opsec_connection"] = opsec[original_pid]
        row["width"], row["height"] = read_size(
            row["tty"], app.tty_device if app else None
        )

    @cache
    def pane_app(pane_pid):
        app = mosh_sessions.foreground_app(processes.get(pane_pid), processes.values())
        return {
            "pane_pid": pane_pid,
            **app_details(app.pid if app else None),
            **(
                {"type": "OPSEC-TMUX", "opsec_connection": opsec[app.pid]}
                if app and app.pid in opsec
                else {}
            ),
        }

    rows = [
        {
            **pane_app(client.pane_pid),
            "session": client.session,
            "session_id": client.session_id,
            "pid": client.pid,
            "tty": client.tty,
            "transport": client.transport,
            "state": client.state,
            "attachment": client.attachment,
            "reachability": ("RECENT" if client.state == "CONNECTED" else client.state)
            if client.transport == "MOSH"
            else None,
            "frontend": client.frontend or None,
            "frontend_pid": client.frontend_pid,
            "frontend_binary_mtime": binary_mtime(client.frontend_pid),
            "frontend_outdated": binary_outdated(client.frontend_pid),
            "created_at": client.created,
            "last_activity_at": client.activity,
            "activity_idle_seconds": activity_age(client.activity, now),
            "idle_seconds": None,
            "network_last_seen_at": None,
            "network_last_rx_monotonic_ms": None,
            **(last_contact(client.frontend_pid) if client.transport == "MOSH" else {}),
            "peer": client.peer,
            "width": client.width,
            "height": client.height,
            "window_id": client.window_id or None,
            "sizing_policy": client.sizing_policy or None,
            "sizing_client_pid": client.sizing_client_pid,
            "active_sizing": client.active_sizing,
            "sizing": "not-applicable"
            if not client.tty
            else (
                "ignored-auto"
                if client.identity in owned
                else "ignored-manual"
                if client.identity
                else "ignored-unknown"
            )
            if "ignore-size" in client.flags
            else "participating",
        }
        for client in clients
    ]
    rows.sort(
        key=lambda row: (row["session"].casefold(), row["created_at"] or 0, row["pid"])
    )
    sessions = [
        {
            **pane_app(session.pane_pid),
            "id": session.identity,
            "name": session.name,
            "attached_clients": session.attached,
            "created_at": session.created,
            "last_activity_at": session.activity,
            "activity_idle_seconds": activity_age(session.activity, now),
            "idle_seconds": None,
        }
        for session in sorted(
            server.sessions if server_running else [],
            key=lambda item: item.name.casefold(),
        )
    ]
    represented = {row["app_pid"] for row in rows + sessions + other_mosh + other_ssh}
    other_opsec = []
    for app_pid, connection in opsec.items():
        if app_pid in represented:
            continue
        app = processes[app_pid]
        transport, origin = connection_origin(app_pid, lookup=process)
        tty = connection["tty"]
        reachability, peer = None, None
        if transport == "MOSH":
            records, login_note = read_logins()
            note = "; ".join(dict.fromkeys(filter(None, (note, login_note))))
            state, peer = records.get((origin.pid, tty), ("UNKNOWN", None))
            reachability = "RECENT" if state == "CONNECTED" else state
        width, height = read_size(tty, app.tty_device)
        other_opsec.append(
            {
                "pid": connection["ssh_pid"],
                **app_details(app_pid),
                "mode": "OPSEC-TMUX",
                "transport": transport,
                "frontend": origin.executable if origin else None,
                "frontend_pid": origin.pid if origin else None,
                "frontend_binary_mtime": binary_mtime(origin.pid) if origin else None,
                "frontend_outdated": binary_outdated(origin.pid) if origin else None,
                "process_state": "SUSPENDED" if app.state in ("T", "t") else "RUNNING",
                "tty": tty,
                "reachability": reachability,
                "peer": peer,
                "width": width,
                "height": height,
                "idle_seconds": None,
                **(last_contact(origin.pid) if transport == "MOSH" else {}),
                "opsec_connection": connection,
            }
        )
    result = {
        "snapshot_at": now,
        "socket": str(server.socket),
        "server_running": server_running,
        "server_binary_mtime": server_binary_mtime,
        "server_binary_outdated": server_binary_outdated,
        "monitor_running": server_running and watcher_alive(server.watcher),
        "clients": rows,
        "sessions": sessions,
        "other_mosh_sessions": other_mosh,
        "other_ssh_sessions": other_ssh,
        "other_opsec_sessions": other_opsec,
        "note": note,
    }
    result["entries"] = unified_entries(result)
    return result


def format_process(name, pid):
    label = name or "-"
    return f"{label} ({pid})" if type(pid) is int and pid > 0 else label


def format_via(
    transport,
    frontend=None,
    state=None,
    *,
    idle_seconds=None,
    reachability=None,
):
    if transport == "LOCAL":
        return (frontend or "local").lower()
    if transport == "MOSH":
        label = "mosh"
        if idle_seconds is None:
            label += f" [{(reachability or 'unknown').lower()}]"
        return label
    return (transport or state or "unknown").lower()


def unified_entries(result):
    entries = [
        {
            "type": row.get("type", "TMUX"),
            "session": row["opsec_connection"].get("session")
            if row.get("opsec_connection")
            else row["session"],
            "app": row["app"],
            "app_label": row.get("app_label"),
            "app_pid": row["app_pid"],
            "command": row.get("command"),
            "transport": row["transport"],
            "frontend_binary_mtime": row.get("frontend_binary_mtime"),
            "frontend_outdated": row.get("frontend_outdated"),
            "tmux_binary_mtime": row["opsec_connection"].get("server_binary_mtime")
            if row.get("opsec_connection")
            else result.get("server_binary_mtime"),
            "tmux_outdated": row["opsec_connection"].get("server_binary_outdated")
            if row.get("opsec_connection")
            else result.get("server_binary_outdated"),
            "via": format_via(
                row["transport"],
                row.get("frontend"),
                idle_seconds=row["idle_seconds"],
                reachability=row["reachability"],
            ),
            "state": row["attachment"],
            "reachability": row["reachability"],
            "tty": row["tty"],
            "mosh_pid": row["frontend_pid"] if row["transport"] == "MOSH" else None,
            "idle_seconds": row["idle_seconds"] if row["transport"] == "MOSH" else None,
            "peer": row["peer"],
            "width": row["width"],
            "height": row["height"],
            "sizing": row["sizing"],
            "window_id": row.get("window_id"),
            "sizing_policy": row.get("sizing_policy"),
            "sizing_client_pid": row.get("sizing_client_pid"),
            "active_sizing": row.get("active_sizing"),
        }
        for row in result["clients"]
    ]
    present = {row["session_id"] for row in result["clients"]}
    for session in result["sessions"]:
        if session["id"] in present:
            continue
        entries.append(
            {
                "type": session.get("type", "TMUX"),
                "session": session["opsec_connection"].get("session")
                if session.get("opsec_connection")
                else session["name"],
                "app": session["app"],
                "app_label": session.get("app_label"),
                "app_pid": session["app_pid"],
                "command": session.get("command"),
                "transport": None,
                "frontend_binary_mtime": None,
                "frontend_outdated": None,
                "tmux_binary_mtime": session["opsec_connection"].get(
                    "server_binary_mtime"
                )
                if session.get("opsec_connection")
                else result.get("server_binary_mtime"),
                "tmux_outdated": session["opsec_connection"].get(
                    "server_binary_outdated"
                )
                if session.get("opsec_connection")
                else result.get("server_binary_outdated"),
                "via": "detached" if session["attached_clients"] == 0 else "changing",
                "state": "DETACHED" if session["attached_clients"] == 0 else "CHANGING",
                "reachability": None,
                "tty": None,
                "mosh_pid": None,
                "idle_seconds": None,
                "peer": None,
                "width": None,
                "height": None,
                "sizing": None,
                "window_id": None,
                "sizing_policy": None,
                "sizing_client_pid": None,
                "active_sizing": None,
            }
        )
    remote_rows = [
        (via, row)
        for via, key in (("MOSH", "other_mosh_sessions"), ("SSH", "other_ssh_sessions"))
        for row in result.get(key, [])
    ]
    remote_rows.extend(
        (row["transport"], row) for row in result.get("other_opsec_sessions", [])
    )
    for via, row in remote_rows:
        entries.append(
            {
                "type": row["mode"],
                "session": row.get("opsec_connection", {}).get("session"),
                "app": row["app"],
                "app_label": row.get("app_label"),
                "app_pid": row["app_pid"],
                "command": row.get("command"),
                "transport": via,
                "frontend_binary_mtime": row.get("frontend_binary_mtime"),
                "frontend_outdated": row.get("frontend_outdated"),
                "tmux_binary_mtime": row.get("opsec_connection", {}).get(
                    "server_binary_mtime"
                ),
                "tmux_outdated": row.get("opsec_connection", {}).get(
                    "server_binary_outdated"
                ),
                "via": format_via(
                    via,
                    row.get("frontend"),
                    idle_seconds=row["idle_seconds"],
                    reachability=row["reachability"],
                ),
                "state": row["process_state"],
                "reachability": row["reachability"],
                "tty": row["tty"],
                "mosh_pid": row.get("frontend_pid", row["pid"])
                if via == "MOSH"
                else None,
                "idle_seconds": row["idle_seconds"],
                "peer": row["peer"],
                "width": row.get("width"),
                "height": row.get("height"),
                "sizing": None,
                "window_id": None,
                "sizing_policy": None,
                "sizing_client_pid": None,
                "active_sizing": None,
            }
        )
    type_order = {
        "TMUX": 0,
        "OPSEC-TMUX": 1,
        "DIRECT": 2,
        "OTHER-TMUX": 3,
        "UNKNOWN": 4,
    }
    entries.sort(
        key=lambda row: (
            type_order.get(row["type"], 4),
            (row["session"] or row.get("app_label") or row["app"] or "").casefold(),
        )
    )
    return entries


def compact_command(command):
    if not command:
        return "-"
    try:
        argv = shlex.split(command)
    except ValueError:
        return command
    if not argv:
        return "-"
    if argv[0] == "pi" and not any(argv[1:]):
        argv = argv[:1]
    argv[0] = Path(argv[0]).name
    home = str(Path.home())
    argv = [
        "~" + arg[len(home) :] if arg == home or arg.startswith(home + "/") else arg
        for arg in argv
    ]
    return shlex.join(argv)


def format_sizing(row):
    if row.get("active_sizing") is True:
        return "active"
    participation = row.get("sizing")
    if participation == "participating":
        if row.get("active_sizing") is None:
            return "unknown"
        policy = row.get("sizing_policy")
        if policy == "manual":
            return "manual"
        if policy in ("smallest", "largest"):
            return "shared"
        return "standby"
    return {
        "ignored-auto": "auto-off",
        "ignored-manual": "manual-off",
        "ignored-unknown": "unknown",
    }.get(participation, "-")


def show_clients(server, as_json=False, *, verbose=False, once=False, flat=False):
    show_clients_flat(server, as_json, verbose=verbose)


def show_clients_flat(server, as_json=False, *, verbose=False):
    result = inventory(server)
    if as_json:
        print(json.dumps(result))
        return
    observed = time.strftime(
        "%Y-%m-%d %H:%M:%S %Z", time.localtime(result["snapshot_at"])
    )
    print(
        f"Snapshot: {observed} | {len(result['sessions'])} tmux sessions, "
        f"{len(result['clients'])} clients, {len(result['other_mosh_sessions'])} other Mosh sessions, "
        f"{len(result.get('other_ssh_sessions', []))} other SSH sessions"
        f", {sum(row['type'] == 'OPSEC-TMUX' for row in result['entries'])} opsec-tmux viewers"
    )
    print(
        "Sizing monitor: " + ("running" if result["monitor_running"] else "not running")
    )
    if not result["server_running"]:
        print(f"No tmux server at {server.socket}.")
    table = [
        [
            "VIA",
            "TYPE",
            "SESSION",
            "APP",
            "IDLE",
            "PEER",
            "SIZE",
            "SIZING",
        ]
    ]
    for row in result["entries"]:
        table.append(
            [
                value.lower()
                for value in (
                    row["via"],
                    row["type"],
                    row["session"] or "-",
                    format_process(row.get("app_label") or row["app"], row["app_pid"]),
                    format_age(row["idle_seconds"]),
                    row["peer"] or "-",
                    f"{row['width']}x{row['height']}"
                    if row["width"] and row["height"]
                    else "-",
                    format_sizing(row),
                )
            ]
        )
    highlights = {}
    if (
        sys.stdout.isatty()
        and os.environ.get("TERM") != "dumb"
        and "NO_COLOR" not in os.environ
    ):
        for index, row in enumerate(result["entries"], start=1):
            label = row["via"].split(" ", 1)[0]
            if (label == "mosh" and row.get("frontend_outdated") is not False) or (
                label == "xfce4-terminal" and row.get("frontend_outdated") is True
            ):
                highlights[index, 0] = label
            if row.get("tmux_outdated") is True and row["type"] in (
                "TMUX",
                "OPSEC-TMUX",
                "OTHER-TMUX",
            ):
                highlights[index, 1] = row["type"].lower()
    print_table(table, highlights=highlights)


def show_cleanup(result, as_json=False):
    if as_json:
        print(json.dumps(result, indent=2))
        return
    print_table(
        [["MOSH PID", "APP", "IDLE", "SOURCE", "RESULT", "DETAIL"]]
        + [
            [
                str(row["mosh_pid"]),
                row["app"] or "-",
                f"{row['idle_seconds']}s" if row["idle_seconds"] is not None else "-",
                row["source"],
                row["action"],
                row["detail"],
            ]
            for row in result["entries"]
        ]
    )
    summary = result["summary"]
    print(
        f"{summary['closing']} closing; {summary['would-close']} would close; "
        f"{summary['kept']} kept; {summary['skipped']} skipped; {summary['error']} errors. "
        f"Cutoff: {result['older_than_seconds']}s since last client packet."
    )
    if result["discovery_error"]:
        print(result["discovery_error"], file=sys.stderr)


def print_table(table, *, highlights=None):
    table = [
        [
            "".join(char if char.isprintable() else "?" for char in value)
            for value in row
        ]
        for row in table
    ]
    widths = [max(len(row[i]) for row in table) for i in range(len(table[0]))]
    for index, row in enumerate(table):
        cells = []
        for column, (value, width) in enumerate(zip(row, widths)):
            cell = value.ljust(width)
            label = (highlights or {}).get((index, column))
            if label and value.startswith(label):
                cell = f"\033[41m{label}\033[0m" + cell[len(label) :]
            cells.append(cell)
        print("  ".join(cells).rstrip())


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
        print(f"tmux-mosh sizing monitor: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
