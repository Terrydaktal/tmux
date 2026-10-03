"""Identify existing foreground viewers using the fixed opsec VM SSH route."""

import json
import os
import shlex
import stat
import subprocess
from pathlib import Path

from mosh_sessions import foreground_app, process_tty

CONTROL_PATH = "/run/opsec-whonix-terminal/master"
GUEST_LAUNCHERS = {"/home/qwen/.local/bin/opsec-guest", "/usr/local/bin/opsec-guest"}
VALUE_OPTIONS = set("BbcDEeFIiJLlmOopQRSWw")
FLAG_OPTIONS = set("46AaCfGgKkMNnqSsTtVvXxYy")
STATUS_TIMEOUT = 2.0
STATUS_SCRIPT = r"""
import hashlib, json, os, stat, subprocess, sys
from pathlib import Path

def binary_fingerprint(path, fingerprints):
    def key(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    try:
        before = path.stat()
        identity = key(before)
        limit = 64 * 1024 * 1024
        if not stat.S_ISREG(before.st_mode) or not 4 <= before.st_size <= limit:
            return None
        if identity in fingerprints:
            return fingerprints[identity]
        with path.open('rb') as source:
            if key(os.fstat(source.fileno())) != identity:
                return None
            magic = source.read(4)
            if magic != b'\x7fELF':
                return None
            digest = hashlib.sha256(magic)
            remaining = limit - 4
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

def binary_outdated(pid, installed, fingerprints, root=Path('/proc')):
    try:
        directory = root / str(int(pid))
        def identity():
            fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
            return directory.stat().st_uid, fields[19], fields[0]
        before = identity()
        if before[0] != os.getuid() or before[2] in ('Z', 'X'):
            return None
        running = binary_fingerprint(directory / 'exe', fingerprints)
        expected = binary_fingerprint(installed, fingerprints)
        if identity() != before:
            return None
        return running != expected if running is not None and expected is not None else None
    except (OSError, ValueError, IndexError):
        return None

def binary_mtime(pid, root=Path('/proc')):
    try:
        pid = int(pid)
        if pid <= 0:
            return None
        directory = root / str(pid)
        def identity():
            fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
            return directory.stat().st_uid, fields[19], fields[0]
        before = identity()
        if before[0] != os.getuid() or before[2] in ('Z', 'X'):
            return None
        executable = directory / 'exe'
        metadata = executable.stat()
        if identity() != before:
            return None
        current = executable.stat()
        if ((metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns)
                != (current.st_dev, current.st_ino, current.st_mtime_ns)
                or not stat.S_ISREG(metadata.st_mode)
                or not 0 < metadata.st_mtime < 253402300800):
            return None
        return metadata.st_mtime
    except (OSError, ValueError, IndexError):
        return None

requests = json.load(sys.stdin)
root = Path('/run/user') / str(os.getuid()) / 'opsec-tmux-simple'
binary = Path.home() / '.local/bin/tmux'
sessions = []
binary_dates = {}
binary_versions = {}
fingerprints = {}
for socket in sorted(root.glob('*/server.sock'))[:8]:
    try:
        info = socket.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            continue
        output = subprocess.check_output([
            str(binary), '-N', '-S', str(socket), 'list-sessions', '-F',
            '#{session_name}\t#{session_path}\t#{session_id}\t#{pid}',
        ], text=True, timeout=0.2, stderr=subprocess.DEVNULL)
        for line in output.splitlines()[:64]:
            fields = line.split('\t')
            if len(fields) in (3, 4):
                mtime = None
                outdated = None
                if len(fields) == 4:
                    if fields[3] not in binary_dates:
                        binary_dates[fields[3]] = binary_mtime(fields[3])
                    mtime = binary_dates[fields[3]]
                    if fields[3] not in binary_versions:
                        binary_versions[fields[3]] = binary_outdated(fields[3], binary, fingerprints)
                    outdated = binary_versions[fields[3]]
                sessions.append({'session': fields[0], 'cwd': fields[1],
                                 'id': fields[2], 'socket': str(socket),
                                 'server_binary_mtime': mtime,
                                 'server_binary_outdated': outdated})
    except (OSError, subprocess.SubprocessError):
        continue
names = {}
for request in requests[:64]:
    candidates = sessions
    if request.get('workspace'):
        try:
            cwd = str(Path(request['workspace']).resolve(strict=True))
        except OSError:
            continue
        prefix = request.get('action', '') + '-'
        candidates = [row for row in sessions if row['cwd'] == cwd
                      and row['session'].startswith(prefix)
                      and Path(row['socket']).parent.name == row['session']]
    elif request.get('guest_socket'):
        candidates = [row for row in sessions
                      if row['socket'] == request['guest_socket']]
        target = request.get('session_target')
        if target:
            target = target.removeprefix('=').split(':', 1)[0]
            exact = [row for row in candidates
                     if row['session'] == target or row['id'] == target]
            candidates = exact or [row for row in candidates
                                   if row['session'].startswith(target)]
    else:
        continue
    if len(candidates) == 1:
        row = candidates[0]
        names[str(request['app_pid'])] = {
            'session': row['session'], 'server_socket': row['socket'],
        }
        if row['server_binary_mtime'] is not None:
            names[str(request['app_pid'])]['server_binary_mtime'] = row['server_binary_mtime']
        if row['server_binary_outdated'] is not None:
            names[str(request['app_pid'])]['server_binary_outdated'] = row['server_binary_outdated']
print(json.dumps(names))
"""


def ssh_target(command):
    """Recognize terminal attachment requests, not arbitrary SSH/VM commands."""
    try:
        argv = shlex.split(command or "")
        if not argv or Path(argv[0]).name != "ssh":
            return None
        options, flags, values = {}, set(), {}
        index = 1
        while index < len(argv) and argv[index].startswith("-"):
            argument = argv[index]
            index += 1
            if argument == "--":
                break
            for offset, option in enumerate(argument[1:], start=1):
                if option in VALUE_OPTIONS:
                    value = argument[offset + 1 :]
                    if not value:
                        value = argv[index]
                        index += 1
                    if option != "o" and option in values and values[option] != value:
                        return None
                    values[option] = value
                    if option == "o":
                        key, separator, setting = value.partition("=")
                        if not separator:
                            key, setting = value.split(None, 1)
                        if key.lower() in options and options[key.lower()] != setting:
                            return None
                        options[key.lower()] = setting
                    break
                if option not in FLAG_OPTIONS:
                    return None
                flags.add(option)
        if flags & set("fGMNnsTV") or values.keys() & {"O", "Q", "W"}:
            return None
        if "T" in flags or options.get("requesttty", "").lower() == "no":
            return None
        if "t" not in flags and options.get("requesttty", "").lower() not in (
            "yes",
            "force",
        ):
            return None
        target = argv[index]
        if target != "qwen@opsec-qwen" and not (
            target == "opsec-qwen" and values.get("l") == "qwen"
        ):
            return None
        control_path = options.get("controlpath", values.get("S"))
        if "S" in values and "controlpath" in options and values["S"] != control_path:
            return None
        if control_path != CONTROL_PATH:
            if control_path and control_path.lower() != "none":
                return None
            proxy = shlex.split(options.get("proxycommand", ""))
            clients = {
                str(Path.home() / "tasks/opsec/vm/client.py"),
                "/usr/local/libexec/opsec-vm/client.py",
            }
            if not (
                len(proxy) == 4
                and proxy[0] in ("/usr/bin/python3", "python3")
                and proxy[1] == "-I"
                and proxy[2] in clients
                and proxy[3] == "connect"
            ):
                return None
        lexer = shlex.shlex(
            " ".join(argv[index + 1 :]), posix=True, punctuation_chars=True
        )
        lexer.whitespace_split = True
        remote = list(lexer)
        if any(token and set(token) <= set("();<>|&") for token in remote):
            return None
        if (
            len(remote) >= 3
            and remote[0] in GUEST_LAUNCHERS
            and remote[1] in ("pi", "shell")
            and remote[2].startswith("/")
        ):
            return {"target": "opsec-qwen", "workspace": remote[2], "action": remote[1]}
        if not remote or remote[0] not in ("tmux", "/home/qwen/.local/bin/tmux"):
            return None
        index, socket = 1, None
        while index < len(remote) and remote[index].startswith("-"):
            option = remote[index]
            index += 1
            if option == "--":
                break
            if option in ("-S", "-L", "-f"):
                value = remote[index]
                index += 1
                if option == "-S":
                    socket = value
            elif option not in ("-N", "-u", "-v", "-2"):
                return None
        parts = Path(socket).parts if socket else ()
        if not (
            len(parts) == 7
            and parts[1:3] == ("run", "user")
            and parts[3].isascii()
            and parts[3].isdecimal()
            and parts[4] == "opsec-tmux-simple"
            and parts[5] != ".."
            and parts[6] == "server.sock"
            and remote[index] in ("attach", "attach-session", "new", "new-session")
        ):
            return None
        session_target = None
        for position, argument in enumerate(remote[index + 1 :], start=index + 1):
            if argument in ("-t", "-s"):
                session_target = remote[position + 1]
                break
            if argument.startswith(("-t", "-s")) and len(argument) > 2:
                session_target = argument[2:]
                break
        return {
            "target": "opsec-qwen",
            "workspace": None,
            "guest_socket": socket,
            "session_target": session_target,
        }
    except (ValueError, IndexError):
        return None


def discover(processes, read_command, *, tty_lookup=None):
    """Return one VM-viewer record per current foreground application group."""
    viewers = {}
    tty_lookup = process_tty if tty_lookup is None else tty_lookup
    for client in sorted(processes.values(), key=lambda info: info.pid):
        if (
            client.executable != "ssh"
            or client.uid != os.getuid()
            or client.state in ("Z", "X")
            or not client.tty_device
            or client.pgrp <= 0
            or client.pgrp != client.foreground_pgrp
        ):
            continue
        request = ssh_target(read_command(client.pid))
        if request is None:
            continue
        app = foreground_app(client, processes.values())
        if app is None:
            continue
        current, seen = client, set()
        for _ in range(64):
            if current.pid == app.pid:
                tty = tty_lookup(app) or tty_lookup(client)
                if tty:
                    viewers.setdefault(
                        app.pid,
                        {
                            **request,
                            "ssh_pid": client.pid,
                            "app_pid": app.pid,
                            "tty": tty,
                        },
                    )
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
    return viewers


def resolve_sessions(viewers):
    """Query existing VM servers once; never start the VM or open a TCP route."""
    if not viewers:
        return {}, ""
    control_path = Path(os.environ.get("TMUX_MOSH_OPSEC_CONTROL_PATH", CONTROL_PATH))
    try:
        info = control_path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise OSError("unverified VM SSH master")
        result = subprocess.run(
            [
                "/usr/bin/ssh",
                "-F",
                "/dev/null",
                "-o",
                "ControlPath=" + str(control_path),
                "-o",
                "ControlMaster=no",
                "-o",
                "ProxyCommand=false",
                "-o",
                "HostName=127.0.0.1",
                "-o",
                "ConnectTimeout=1",
                "-o",
                "ConnectionAttempts=1",
                "-o",
                "BatchMode=yes",
                "-o",
                "ForwardAgent=no",
                "-o",
                "ForwardX11=no",
                "-o",
                "ClearAllForwardings=yes",
                "-T",
                "qwen@opsec-qwen",
                shlex.join(["python3", "-c", STATUS_SCRIPT]),
            ],
            input=json.dumps(list(viewers.values())[:64]),
            text=True,
            capture_output=True,
            timeout=STATUS_TIMEOUT,
            check=True,
        )
        if len(result.stdout) > 65536:
            raise ValueError("oversized VM session inventory")
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            raise TypeError("invalid VM session inventory")
        names = {}
        for pid in viewers:
            row = data.get(str(pid))
            if (
                isinstance(row, dict)
                and isinstance(row.get("session"), str)
                and 0 < len(row["session"]) <= 256
                and isinstance(row.get("server_socket"), str)
                and row["server_socket"].startswith("/run/user/")
            ):
                names[pid] = {
                    "session": row["session"],
                    "server_socket": row["server_socket"],
                }
                mtime = row.get("server_binary_mtime")
                if type(mtime) in (int, float) and 0 < mtime < 253402300800:
                    names[pid]["server_binary_mtime"] = mtime
                outdated = row.get("server_binary_outdated")
                if type(outdated) is bool:
                    names[pid]["server_binary_outdated"] = outdated
        missing = len(names) != len(viewers)
        return (
            names,
            "VM session names unavailable for one or more viewers." if missing else "",
        )
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        return (
            {},
            f"VM session names unavailable ({type(exc).__name__}); existing connections are still listed.",
        )
