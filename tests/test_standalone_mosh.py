"""Mosh inventory must include direct apps without inventing tmux attachments."""

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from loopback_harness import Session

import mosh_sessions as mosh
import tmux_clients as clients

TTY = "/dev/pts/8"


def info(pid, parent, executable, **fields):
    return replace(
        clients.Process(pid, parent, pid, executable, os.getuid(), "S"), **fields
    )


def tree(app="codex"):
    return {
        100: info(100, 1, "mosh-server"),
        101: info(101, 100, "fish", pgrp=101, tty_device=123, foreground_pgrp=102),
        102: info(102, 101, app, pgrp=102, tty_device=123, foreground_pgrp=102),
        103: info(
            103, 101, "background-job", pgrp=103, tty_device=123, foreground_pgrp=102
        ),
    }


def rows(processes=None, represented=(), records=None):
    return mosh.extra_sessions(
        tree() if processes is None else processes,
        set(represented),
        {(100, TTY): ("CONNECTED", "100.1.2.3")} if records is None else records,
        tty_lookup=lambda process: TTY,
    )


def test_foreground_app_not_parent_shell_or_background_job():
    (row,) = rows()
    assert (row["pid"], row["app"], row["app_pid"]) == (100, "codex", 102)
    assert (row["mode"], row["reachability"], row["peer"]) == (
        "DIRECT",
        "RECENT",
        "100.1.2.3",
    )
    assert row["idle_seconds"] is None and row["network_last_seen_at"] is None


def test_idle_shell_is_the_foreground_app():
    processes = tree()
    processes[101] = replace(processes[101], foreground_pgrp=101)
    assert rows(processes)[0]["app"] == "fish"


def test_selected_tmux_mosh_servers_are_not_duplicated():
    assert rows(represented={100}) == []


def test_other_tmux_server_is_not_mislabeled_as_a_direct_app():
    (row,) = rows(tree("tmux"))
    assert row["mode"] == "OTHER-TMUX"


@pytest.mark.parametrize(
    "records,state,peer",
    [
        ({}, "UNKNOWN", "-"),
        ({(100, "/dev/pts/9"): ("CONNECTED", "wrong")}, "UNKNOWN", "-"),
        ({(100, TTY): ("UNREACHABLE", "-")}, "UNREACHABLE", "-"),
        ({(100, TTY): ("UNKNOWN", "-")}, "UNKNOWN", "-"),
    ],
)
def test_missing_stale_and_ambiguous_logins_do_not_invent_reachability(
    records, state, peer
):
    (row,) = rows(records=records)
    assert row["reachability"] == state and row["peer"] == peer


def test_login_record_without_a_live_process_cannot_create_a_session():
    assert rows(processes={}) == []


@pytest.mark.parametrize(
    "change", [{"uid": os.getuid() + 1}, {"state": "Z"}, {"state": "X"}]
)
def test_foreign_and_exited_servers_are_excluded(change):
    processes = tree()
    processes[100] = replace(processes[100], **change)
    assert rows(processes) == []


def test_missing_child_is_unknown_but_server_is_still_visible():
    (row,) = rows({100: tree()[100]})
    assert row["mode"] == "UNKNOWN" and row["app"] is None
    assert row["tty"] is None and row["reachability"] == "UNKNOWN"


def test_native_mosh_and_nested_terminals_use_the_outer_foreground_group():
    processes = tree("tmux")
    processes[100] = replace(processes[100], executable="mosh-native-server")
    processes[104] = info(
        104, 102, "nested-app", pgrp=104, tty_device=456, foreground_pgrp=104
    )
    (row,) = rows(processes)
    assert row["mode"] == "OTHER-TMUX" and row["app_pid"] == 102


def test_cycle_in_process_snapshot_does_not_hang():
    processes = tree()
    processes[100] = replace(processes[100], parent=102)
    assert rows(processes)[0]["app_pid"] == 102


def test_proc_scan_tolerates_exit_and_permission_races(tmp_path):
    for name in ("100", "101", "102", "103", "self"):
        (tmp_path / name).mkdir()

    def lookup(pid):
        if pid == 101:
            raise FileNotFoundError(pid)
        if pid == 102:
            raise PermissionError(pid)
        return info(
            pid, 1, "mosh-server", uid=os.getuid() if pid == 100 else os.getuid() + 1
        )

    processes, note = mosh.read_processes(lookup, root=tmp_path)
    assert list(processes) == [100] and not note
    assert "unavailable" in mosh.read_processes(lookup, root=tmp_path / "missing")[1]


def test_fd_discovery_and_redirected_fd_fallback_verify_the_controlling_pty(tmp_path):
    master, slave = os.openpty()
    try:
        tty = os.ttyname(slave)
        device = os.fstat(slave).st_rdev
        processes = tree()
        for pid in (101, 102, 103):
            processes[pid] = replace(processes[pid], tty_device=device)
        fd = tmp_path / "101/fd/0"
        fd.parent.mkdir(parents=True)
        fd.symlink_to(tty)
        assert mosh.process_tty(processes[101], root=tmp_path) == tty
        assert (
            mosh.process_tty(
                replace(processes[101], tty_device=device + 1), root=tmp_path
            )
            is None
        )
        result = mosh.extra_sessions(
            processes,
            set(),
            {(100, tty): ("CONNECTED", "peer")},
            tty_lookup=lambda process: None,
        )
        assert result[0]["tty"] == tty and result[0]["reachability"] == "RECENT"
    finally:
        os.close(master)
        os.close(slave)


def test_discovery_without_tmux_server_never_starts_one(backend, monkeypatch):
    monkeypatch.setattr(mosh, "read_processes", lambda lookup: (tree(), ""))
    result = clients.inventory(clients.Server(backend.binary, backend.socket))
    assert not result["server_running"] and not result["monitor_running"]
    assert not result["clients"] and not result["sessions"]
    assert result["other_mosh_sessions"][0]["app"] == "codex"
    assert not backend.socket.exists()


def test_no_mosh_does_not_read_login_records(backend, monkeypatch):
    monkeypatch.setattr(mosh, "read_processes", lambda lookup: ({}, ""))

    def unexpected():
        pytest.fail("no reason to invoke who without Mosh")

    monkeypatch.setattr(clients, "login_records", unexpected)
    assert (
        clients.inventory(clients.Server(backend.binary, backend.socket))[
            "other_mosh_sessions"
        ]
        == []
    )


@pytest.mark.parametrize("through_tmux", [False, True], ids=["direct-app", "tmux-app"])
def test_real_loopback_mosh_inventory_and_deduplication(
    backend, tmp_path, through_tmux
):
    program = backend.program()
    if through_tmux:
        program = backend.cli("new-session", "-s", "test", "--", *program)
    session = Session(tmp_path, program=program)
    try:
        session.attachment.until(lambda state: b"READY" in state["screen"], timeout=8)
        # The PID comes from our fixture's pidfd, never from a process-name kill.
        pid = int(
            next(
                line.split()[1]
                for line in Path(f"/proc/self/fdinfo/{session.pidfd}")
                .read_text()
                .splitlines()
                if line.startswith("Pid:")
            )
        )
        result = subprocess.run(
            backend.mosh_cli("clients", "--json"),
            env=backend.env,
            capture_output=True,
            text=True,
            check=True,
            timeout=6,
        )
        result = json.loads(result.stdout)
        extra = [row for row in result["other_mosh_sessions"] if row["pid"] == pid]
        if through_tmux:
            assert extra == []
            assert any(row["frontend_pid"] == pid for row in result["clients"])
            outside = clients.inventory(
                clients.Server(backend.binary, tmp_path / "absent.sock")
            )
            (row,) = [
                row for row in outside["other_mosh_sessions"] if row["pid"] == pid
            ]
            assert row["mode"] == "OTHER-TMUX"
            assert "new-session -s test" in row["command"]
        else:
            (row,) = extra
            assert row["mode"] == "DIRECT" and row["app"].startswith("python")
            assert row["tty"].startswith("/dev/pts/")
            assert str(Path(__file__).parent / "workload.py") in row["command"]
            assert not backend.socket.exists()
        assert row["width"] > 0 and row["height"] > 0
        (entry,) = [
            entry
            for entry in (outside if through_tmux else result)["entries"]
            if entry["mosh_pid"] == pid
        ]
        assert (entry["width"], entry["height"]) == (row["width"], row["height"])
        assert entry["command"] == row["command"]
        session.attachment.resize(100, 35)
        session.attachment.until(
            lambda state: mosh.terminal_size(row["tty"]) == (100, 35), timeout=5
        )
        refreshed = clients.inventory(
            clients.Server(backend.binary, tmp_path / "absent.sock")
        )
        (entry,) = [entry for entry in refreshed["entries"] if entry["mosh_pid"] == pid]
        assert (entry["width"], entry["height"]) == (100, 35)
    finally:
        session.close()
