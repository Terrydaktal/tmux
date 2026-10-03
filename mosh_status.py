"""Query the local Mosh server's authenticated-packet receive timestamp."""

import json
import os
import socket
import stat
import struct
import time
from pathlib import Path


def directories():
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    bases = [Path(runtime)] if runtime and Path(runtime).is_absolute() else []
    bases.append(Path(f"/run/user/{os.getuid()}"))
    paths = [base / "mosh-status" for base in bases]
    paths.append(Path(f"/tmp/mosh-status-{os.getuid()}"))
    return list(dict.fromkeys(paths))


def read(server, lookup, *, timeout=0.1):
    if server.uid != os.getuid() or server.state in ("Z", "X"):
        return None
    for directory in directories():
        try:
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                continue
            path = directory / f"{server.pid}.sock"
            info = path.lstat()
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                continue
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout)
                connection.connect(str(path))
                pid, uid, _ = struct.unpack(
                    "3i",
                    connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12),
                )
                if (pid, uid) != (server.pid, server.uid):
                    continue
                data = bytearray()
                deadline = time.monotonic() + timeout
                while b"\n" not in data and len(data) < 512:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    connection.settimeout(remaining)
                    chunk = connection.recv(512 - len(data))
                    if not chunk:
                        break
                    data.extend(chunk)
            observed_mono_ms = time.monotonic_ns() // 1_000_000
            observed_wall = time.time()
            current = lookup(server.pid)
            if (current.pid, current.started, current.uid) != (
                server.pid,
                server.started,
                server.uid,
            ):
                return None
            if not data.endswith(b"\n"):
                continue
            status = json.loads(data)
            if (
                not isinstance(status, dict)
                or type(status.get("version")) is not int
                or status.get("version") != 1
                or status.get("pid") != server.pid
            ):
                continue
            received = status.get("last_rx_monotonic_ms")
            if type(received) is not int or not 0 <= received <= observed_mono_ms:
                continue
            age_s = (observed_mono_ms - received) / 1000.0
            return {
                "idle_seconds": int(age_s),
                "network_last_seen_at": observed_wall - age_s,
                "network_last_rx_monotonic_ms": received,
            }
        except (OSError, ValueError, IndexError, struct.error):
            continue
    return None
