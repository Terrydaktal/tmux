"""Explicit Mosh cleanup using packet timestamps or opt-in legacy login records."""

import ctypes
import errno
import os
import signal
import time
from collections import defaultdict
from functools import cache

import mosh_sessions
import mosh_status

WARNING = (
    "Closing a direct Mosh session can terminate its shell/app. "
    "Tmux servers and their programs are not targeted. "
    "Lost signal can look the same as intentionally closing the client."
)
LEGACY_WARNING = (
    "Legacy cleanup uses Mosh's coarse UNREACHABLE login flag (~30s), "
    "not an exact packet timestamp."
)


@cache
def pidfd_api():
    # Some Python builds omit pidfd helpers even though libc/kernel support them.
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        open_fd, send_signal = libc.pidfd_open, libc.pidfd_send_signal
    except AttributeError:
        raise OSError(errno.ENOSYS, "PID-safe signalling is unavailable") from None
    open_fd.argtypes = [ctypes.c_int, ctypes.c_uint]
    send_signal.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    open_fd.restype = send_signal.restype = ctypes.c_int
    return open_fd, send_signal


def checked_call(operation, *args):
    result = operation(*args)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def pidfd_open(pid):
    return checked_call(pidfd_api()[0], pid, 0)


def send_termination(fd):
    checked_call(pidfd_api()[1], fd, signal.SIGTERM, None, 0)


def same_server(before, after):
    return (
        (before.pid, before.started, before.uid, before.executable)
        == (after.pid, after.started, after.uid, after.executable)
        and after.uid == os.getuid()
        and after.executable in mosh_sessions.MOSH_SERVERS
        and after.state not in ("Z", "X")
    )


def packet_age(owner, lookup):
    status = mosh_status.read(owner, lookup)
    if status is None:
        return None
    return status["idle_seconds"]


def legacy_reachability(owner, anchor, lookup, read_logins):
    # A never-attached server has the same login flag; let new servers start first.
    try:
        age = time.clock_gettime(time.CLOCK_BOOTTIME) - owner.started / os.sysconf(
            "SC_CLK_TCK"
        )
        if owner.started <= 0 or age < 30:
            return "UNKNOWN", "legacy server is new or its start time is unavailable"
        if anchor is None or read_logins is None:
            return "UNKNOWN", "no verified legacy login terminal"
        current = lookup(anchor.pid)
        if (
            (
                current.pid,
                current.started,
                current.parent,
                current.uid,
                current.tty_device,
            )
            != (
                anchor.pid,
                anchor.started,
                anchor.parent,
                anchor.uid,
                anchor.tty_device,
            )
            or current.uid != os.getuid()
            or current.state in ("Z", "X")
        ):
            return "UNKNOWN", "legacy login terminal identity changed"
        records, note = read_logins()
        if note:
            return "UNKNOWN", note
        matches = [
            (tty, record) for (pid, tty), record in records.items() if pid == owner.pid
        ]
        if len(matches) != 1:
            return "UNKNOWN", "missing or ambiguous legacy Mosh login record"
        tty, (state, _) = matches[0]
        if not mosh_sessions.tty_matches(tty, current.tty_device):
            return "UNKNOWN", "legacy login record does not match the server's terminal"
        if state == "UNREACHABLE":
            return (
                state,
                "legacy login reports UNREACHABLE (~30s); exact packet age unavailable",
            )
        if state == "CONNECTED":
            return state, "legacy login reports a connected client"
        return "UNKNOWN", "ambiguous legacy Mosh login record"
    except (OSError, ValueError, IndexError):
        return "UNKNOWN", "legacy login metadata unavailable"


def cleanup(
    lookup,
    *,
    older_than=30,
    dry_run=False,
    include_legacy=False,
    read_logins=None,
    snapshot=None,
):
    if type(older_than) is not int or older_than <= 0:
        raise ValueError("the packet-age cutoff must be a positive number of seconds")
    if include_legacy and older_than != 30:
        raise ValueError(
            "legacy login records support only the default 30-second cutoff"
        )
    processes, note = (
        mosh_sessions.read_processes(lookup) if snapshot is None else snapshot
    )
    children = defaultdict(list)
    for owner in processes.values():
        children[owner.parent].append(owner)
    rows = []
    for owner in sorted(processes.values(), key=lambda item: item.pid):
        if (
            owner.pid <= 1
            or owner.uid != os.getuid()
            or owner.state in ("Z", "X")
            or owner.executable not in mosh_sessions.MOSH_SERVERS
        ):
            continue
        anchor, app = mosh_sessions.foreground(owner, children)
        row = {
            "mosh_pid": owner.pid,
            "started_ticks": owner.started,
            "app": app.executable if app else None,
            "idle_seconds": packet_age(owner, lookup),
            "source": "packet-timestamp",
            "reachability": None,
            "action": "skipped",
            "detail": "no packet timestamp; use --include-legacy for coarse UNREACHABLE cleanup",
        }
        rows.append(row)
        legacy = row["idle_seconds"] is None and include_legacy
        if row["idle_seconds"] is None:
            row["source"] = "legacy-login" if legacy else "unavailable"
            if not legacy:
                continue
            row["reachability"], row["detail"] = legacy_reachability(
                owner, anchor, lookup, read_logins
            )
            if row["reachability"] == "CONNECTED":
                row["action"] = "kept"
            if row["reachability"] != "UNREACHABLE":
                continue
        elif row["idle_seconds"] < older_than:
            row.update(action="kept", detail="heard from within the cutoff")
            continue
        if dry_run:
            row["action"] = "would-close"
            if not legacy:
                row["detail"] = "packet-age cutoff reached"
            continue

        fd = None
        try:
            # Pin this process before rechecking identity and packet freshness.
            # Never fall back to os.kill: a recycled PID could target another app.
            fd = pidfd_open(owner.pid)
            current = lookup(owner.pid)
            if not same_server(owner, current):
                row.update(detail="process identity changed")
                continue
            row["idle_seconds"] = packet_age(current, lookup)
            if row["idle_seconds"] is None:
                if not legacy:
                    row.update(
                        detail="packet timestamp became unavailable; cleanup cancelled"
                    )
                    continue
                row["reachability"], row["detail"] = legacy_reachability(
                    current, anchor, lookup, read_logins
                )
                if row["reachability"] == "CONNECTED":
                    row.update(
                        action="kept", detail="legacy client resumed before cleanup"
                    )
                if row["reachability"] != "UNREACHABLE":
                    continue
            elif row["idle_seconds"] < older_than:
                row.update(source="packet-timestamp", reachability=None)
                row.update(action="kept", detail="client resumed before cleanup")
                continue
            else:
                row.update(source="packet-timestamp", reachability=None)
            if not same_server(owner, lookup(owner.pid)):
                row.update(detail="process identity changed before signalling")
                continue
            send_termination(fd)
            row.update(
                action="closing",
                detail="SIGTERM sent; legacy login rechecked as UNREACHABLE"
                if row["source"] == "legacy-login"
                else "SIGTERM sent to the verified Mosh server",
            )
        except (ProcessLookupError, FileNotFoundError):
            row.update(detail="Mosh server already exited")
        except (ValueError, IndexError):
            row.update(detail="process metadata changed or became unreadable")
        except OSError as exc:
            row.update(action="error", detail=f"cannot safely close Mosh server: {exc}")
        finally:
            if fd is not None:
                os.close(fd)
    return {
        "older_than_seconds": older_than,
        "dry_run": dry_run,
        "include_legacy": include_legacy,
        "scope": "this user's Mosh servers on this host, across all tmux sockets and direct apps",
        "warning": WARNING + (" " + LEGACY_WARNING if include_legacy else ""),
        "discovery_error": note or None,
        "entries": rows,
        "summary": {
            action: sum(row["action"] == action for row in rows)
            for action in ("closing", "would-close", "kept", "skipped", "error")
        },
    }
