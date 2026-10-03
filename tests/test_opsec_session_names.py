"""Read-only guest session queries with fake SSH and tmux, never a live VM."""

import io
import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import opsec_sessions as opsec

SOCKET = "/run/user/1002/opsec-tmux-simple/pi-actual/server.sock"
VIEWERS = {
    102: {
        "app_pid": 102,
        "action": "pi",
        "workspace": "/workspace/a ' ; echo bad",
    }
}


@pytest.fixture
def fake_master(monkeypatch):
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path: SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=os.getuid()),
    )
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps({"102": {"session": "pi-actual", "server_socket": SOCKET}}),
            "",
        )

    monkeypatch.setattr(opsec.subprocess, "run", run)
    return calls


def test_guest_query_uses_only_existing_master_and_fixed_code(fake_master):
    names, note = opsec.resolve_sessions(VIEWERS)
    assert names == {102: {"session": "pi-actual", "server_socket": SOCKET}}
    assert note == ""
    ((argv, options),) = fake_master
    assert argv[:3] == ["/usr/bin/ssh", "-F", "/dev/null"]
    assert "ControlMaster=no" in argv and "ProxyCommand=false" in argv
    assert "HostName=127.0.0.1" in argv and "BatchMode=yes" in argv
    assert "ForwardAgent=no" in argv and "ClearAllForwardings=yes" in argv
    assert "-T" in argv and "-t" not in argv
    assert shlex.split(argv[-1]) == ["python3", "-c", opsec.STATUS_SCRIPT]
    assert VIEWERS[102]["workspace"] not in argv[-1]
    assert json.loads(options["input"]) == list(VIEWERS.values())
    assert options["timeout"] == 2 and options["check"] and options["capture_output"]
    assert not options.get("shell")


@pytest.mark.parametrize(
    "stamp",
    [
        1790000000.25,
        None,
        True,
        0,
        -1,
        "yesterday",
        float("nan"),
        float("inf"),
        10**100,
    ],
)
def test_guest_date_validation_keeps_session_when_date_is_unavailable(
    fake_master, monkeypatch, stamp
):
    row = {
        "session": "pi-actual",
        "server_socket": SOCKET,
        "server_binary_mtime": stamp,
    }
    monkeypatch.setattr(
        opsec.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps({"102": row})),
    )
    names, note = opsec.resolve_sessions(VIEWERS)
    assert note == "" and names[102]["session"] == "pi-actual"
    assert names[102].get("server_binary_mtime") == (
        stamp if stamp == 1790000000.25 else None
    )


@pytest.mark.parametrize("outdated", [True, False, None, 0, 1, "true", {}, []])
def test_guest_outdated_status_accepts_only_actual_booleans(
    fake_master, monkeypatch, outdated
):
    row = {
        "session": "pi-actual",
        "server_socket": SOCKET,
        "server_binary_outdated": outdated,
    }
    monkeypatch.setattr(
        opsec.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps({"102": row})),
    )
    names, note = opsec.resolve_sessions(VIEWERS)
    assert note == ""
    assert names[102].get("server_binary_outdated") is (
        outdated if type(outdated) is bool else None
    )


def test_no_viewers_means_no_remote_query(fake_master):
    assert opsec.resolve_sessions({}) == ({}, "")
    assert fake_master == []


@pytest.mark.parametrize(
    "info",
    [
        SimpleNamespace(st_mode=stat.S_IFREG, st_uid=os.getuid()),
        SimpleNamespace(st_mode=stat.S_IFLNK, st_uid=os.getuid()),
        SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=os.getuid() + 1),
        FileNotFoundError(),
    ],
)
def test_unverified_master_never_attempts_ssh(fake_master, monkeypatch, info):
    def lstat(path):
        if isinstance(info, Exception):
            raise info
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    names, note = opsec.resolve_sessions(VIEWERS)
    assert names == {} and "unavailable" in note and fake_master == []


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError(),
        subprocess.TimeoutExpired("ssh", 2),
        subprocess.CalledProcessError(255, "ssh"),
    ],
)
def test_failed_query_keeps_inventory_and_suppresses_remote_stderr(
    fake_master, monkeypatch, failure
):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(opsec.subprocess, "run", fail)
    names, note = opsec.resolve_sessions(VIEWERS)
    assert names == {} and "existing connections are still listed" in note


@pytest.mark.parametrize("output", ["not json", "[]", "null", " " * 65537])
def test_bad_or_oversize_response_does_not_break_inventory(
    fake_master, monkeypatch, output
):
    monkeypatch.setattr(
        opsec.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=output)
    )
    names, note = opsec.resolve_sessions(VIEWERS)
    assert names == {} and "unavailable" in note


@pytest.mark.parametrize(
    "row",
    [
        None,
        {},
        {"session": "", "server_socket": SOCKET},
        {"session": "x" * 257, "server_socket": SOCKET},
        {"session": 12, "server_socket": SOCKET},
        {"session": "pi-actual", "server_socket": "/tmp/not-managed.sock"},
    ],
)
def test_invalid_session_metadata_is_not_displayed(fake_master, monkeypatch, row):
    monkeypatch.setattr(
        opsec.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps({"102": row})),
    )
    assert opsec.resolve_sessions(VIEWERS) == (
        {},
        "VM session names unavailable for one or more viewers.",
    )


def test_guest_can_only_supply_names_for_known_viewers(fake_master, monkeypatch):
    row = {
        "session": "pi-actual",
        "server_socket": SOCKET,
        "app_pid": 999,
        "tty": "evil",
    }
    monkeypatch.setattr(
        opsec.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps({"102": row, "999": row})
        ),
    )
    assert opsec.resolve_sessions(VIEWERS) == (
        {102: {"session": "pi-actual", "server_socket": SOCKET}},
        "",
    )


@pytest.fixture
def guest_query(monkeypatch, capsys):
    root = Path("/run/user") / str(os.getuid()) / "opsec-tmux-simple"

    def query(requests, rows, *, unavailable=(), foreign=()):
        sockets = [root / name / "server.sock" for name in rows]
        calls = []
        monkeypatch.setattr(Path, "glob", lambda path, pattern: iter(sockets))
        monkeypatch.setattr(
            Path,
            "lstat",
            lambda path: SimpleNamespace(
                st_mode=stat.S_IFSOCK,
                st_uid=os.getuid() + int(path.parent.name in foreign),
            ),
        )

        def output(argv, **kwargs):
            calls.append((argv, kwargs))
            name = Path(argv[3]).parent.name
            if name in unavailable:
                raise subprocess.TimeoutExpired(argv, 0.2)
            return "\n".join("\t".join(row) for row in rows[name])

        monkeypatch.setattr(opsec.subprocess, "check_output", output)
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(requests)))
        exec(compile(opsec.STATUS_SCRIPT, "guest-session-query", "exec"), {})  # noqa: S102 - fixed code, mocked guest I/O
        names = json.loads(capsys.readouterr().out)
        for argv, options in calls:
            assert argv[1:3] == ["-N", "-S"]
            assert argv[4:6] == ["list-sessions", "-F"]
            assert "\t" in argv[-1]
            assert options["timeout"] == 0.2 and not options.get("shell")
        return names, calls

    return root, query


@pytest.mark.parametrize("alias", [True, False])
def test_guest_matches_actual_name_and_canonical_workspace(
    guest_query, tmp_path, alias
):
    root, query = guest_query
    workspace = tmp_path / "project"
    workspace.mkdir()
    requested = workspace
    if alias:
        requested = tmp_path / "alias"
        requested.symlink_to(workspace)
    requests = [{"app_pid": 102, "action": "pi", "workspace": str(requested)}]
    names, _ = query(requests, {"pi-live": [("pi-live", str(workspace), "$1")]})
    assert names == {
        "102": {
            "session": "pi-live",
            "server_socket": str(root / "pi-live/server.sock"),
        }
    }


@pytest.mark.parametrize("target", [None, "pi-live", "=pi-live:0", "$1", "pi-l"])
def test_guest_matches_explicit_attachment_session(guest_query, target):
    root, query = guest_query
    request = {
        "app_pid": 102,
        "guest_socket": str(root / "pi-live/server.sock"),
        "session_target": target,
    }
    names, _ = query([request], {"pi-live": [("pi-live", "/workspace", "$1")]})
    assert names["102"]["session"] == "pi-live"


def test_guest_reports_running_executable_date_in_existing_session_query(guest_query):
    root, query = guest_query
    request = {"app_pid": 102, "guest_socket": str(root / "pi-live/server.sock")}
    names, calls = query(
        [request],
        {"pi-live": [("pi-live", "/workspace", "$1", str(os.getpid()))]},
    )
    assert len(calls) == 1 and "#{pid}" in calls[0][0][-1]
    assert names["102"]["server_binary_mtime"] == Path("/proc/self/exe").stat().st_mtime


def test_guest_does_not_guess_on_ambiguous_or_missing_workspace(guest_query, tmp_path):
    _, query = guest_query
    rows = {
        name: [(name, str(tmp_path), "$1")]
        for name in ("pi-one", "pi-two", "shell-one")
    }
    requests = [
        {"app_pid": 102, "action": "pi", "workspace": str(tmp_path)},
        {"app_pid": 103, "action": "pi", "workspace": str(tmp_path / "missing")},
        {"app_pid": 104, "action": "shell", "workspace": str(tmp_path)},
    ]
    names, _ = query(requests, rows)
    assert set(names) == {"104"} and names["104"]["session"] == "shell-one"


def test_guest_skips_dead_and_foreign_servers_without_losing_live_one(
    guest_query, tmp_path
):
    _, query = guest_query
    rows = {
        name: [(name, str(tmp_path), "$1")]
        for name in ("pi-dead", "pi-foreign", "pi-live")
    }
    names, calls = query(
        [{"app_pid": 102, "action": "pi", "workspace": str(tmp_path)}],
        rows,
        unavailable=("pi-dead",),
        foreign=("pi-foreign",),
    )
    assert names["102"]["session"] == "pi-live" and len(calls) == 2


def test_guest_bounds_server_queries(guest_query, tmp_path):
    _, query = guest_query
    rows = {
        f"pi-{index:02}": [(f"pi-{index:02}", str(tmp_path), "$1")]
        for index in range(12)
    }
    names, calls = query(
        [{"app_pid": 102, "action": "pi", "workspace": str(tmp_path)}], rows
    )
    assert names == {} and len(calls) == 8
