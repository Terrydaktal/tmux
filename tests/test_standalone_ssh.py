"""Incoming SSH channels, tmux deduplication and bounded peer metadata reads."""

import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

import mosh_sessions
import ssh_sessions as ssh
import tmux_clients as clients


def info(pid, parent, executable, **changes):
    return replace(
        clients.Process(pid, parent, pid, executable, os.getuid(), "S", pid),
        **changes,
    )


def tree(app="python"):
    return {
        100: info(100, 1, "sshd-session", uid=0),
        101: info(101, 100, "fish", tty_device=123, foreground_pgrp=102),
        102: info(102, 101, app, tty_device=123, foreground_pgrp=102),
        103: info(103, 101, "background", tty_device=123, foreground_pgrp=102),
    }


def client(pid=102, tty="/dev/pts/3"):
    return SimpleNamespace(pid=pid, tty=tty, transport="SSH", frontend_pid=100)


def rows(processes=None, represented=(), lookup=None, **kwargs):
    processes = tree() if processes is None else processes

    def get(pid):
        if pid not in processes:
            raise FileNotFoundError(pid)
        return processes[pid]

    return ssh.discover(
        processes,
        represented,
        lookup or get,
        tty_lookup=lambda anchor: "/dev/pts/3" if anchor.tty_device else None,
        peer_lookup=kwargs.pop("peer_lookup", lambda anchor: "100.70.36.28"),
        **kwargs,
    )


@pytest.mark.parametrize("server", sorted(ssh.SSH_SERVERS))
def test_direct_foreground_app_not_waiting_shell_or_background_job(server):
    processes = tree()
    processes[100] = replace(processes[100], executable=server)
    (row,) = rows(processes)
    assert row["app"] == "python" and row["app_pid"] == 102
    assert row["pid"] == 101 and row["ssh_pid"] == 100
    assert row["mode"] == "DIRECT" and row["tty"] == "/dev/pts/3"
    assert row["peer"] == "100.70.36.28"
    assert (
        row["idle_seconds"]
        is row["reachability"]
        is row["network_last_seen_at"]
        is None
    )


def test_no_tty_command_is_visible_without_inventing_a_foreground_terminal():
    processes = tree()
    processes[101] = replace(processes[101], executable="rsync", tty_device=0)
    del processes[102], processes[103]
    (row,) = rows(processes)
    assert row["mode"] == "DIRECT" and row["app"] == "rsync"
    assert row["tty"] is None


def test_listed_tmux_attachment_is_not_duplicated():
    assert rows(tree("tmux"), [client()]) == []


def test_deduplication_preserves_other_channels_under_the_same_sshd():
    processes = tree("tmux")
    processes[104] = info(104, 100, "bash", tty_device=456, foreground_pgrp=104)
    (row,) = rows(processes, [client(tty="/dev/pts/other")])
    assert row["pid"] == 104 and row["app"] == "bash"


def test_control_tmux_client_without_a_tty_is_not_duplicated():
    processes = {100: tree()[100], 101: info(101, 100, "tmux")}
    assert rows(processes, [client(pid=101, tty="")]) == []


def test_other_tmux_server_is_visible_but_not_mislabeled_direct():
    (row,) = rows(tree("tmux"))
    assert row["mode"] == "OTHER-TMUX"


def test_local_outgoing_ssh_and_inherited_environment_cannot_create_ssh_logins():
    for executable in ("systemd", "xfce4-terminal", "ssh", "mosh-server"):
        processes = tree()
        processes[100] = replace(processes[100], executable=executable)
        assert rows(processes) == []


@pytest.mark.parametrize(
    "change", [{"state": "Z"}, {"state": "X"}, {"uid": os.getuid() + 1}]
)
def test_foreign_or_dead_login_processes_are_not_sessions(change):
    processes = tree()
    processes[101] = replace(processes[101], **change)
    assert rows(processes) == []


def test_daemon_and_auth_processes_alone_are_not_logins():
    processes = {
        100: info(100, 1, "sshd"),
        101: info(101, 100, "sshd-session"),
        102: info(102, 101, "sshd-auth"),
    }
    assert rows(processes) == []


def test_dead_or_reused_parent_is_not_session_evidence():
    for change in ({"state": "Z"}, {"started": 1000}):
        processes = tree()
        processes[100] = replace(processes[100], **change)
        assert rows(processes) == []


@pytest.mark.parametrize("exception", [FileNotFoundError, PermissionError, ValueError])
def test_inaccessible_ancestors_do_not_crash_or_guess(exception):
    processes = tree()
    del processes[100]

    def lookup(pid):
        raise exception(pid)

    assert rows(processes, lookup=lookup) == []


def test_root_parent_metadata_is_read_once_without_needing_root():
    processes = tree()
    owner = processes.pop(100)
    processes[104] = info(104, 100, "rsync")
    calls = []

    def lookup(pid):
        calls.append(pid)
        assert pid == owner.pid
        return owner

    assert len(rows(processes, lookup=lookup)) == 2
    assert calls == [100]


def test_missing_foreground_process_does_not_claim_the_shell_is_active():
    processes = tree()
    del processes[102]
    (row,) = rows(processes)
    assert row["mode"] == "UNKNOWN" and row["app"] is None


def test_suspended_foreground_is_preserved_in_json():
    processes = tree()
    processes[102] = replace(processes[102], state="T")
    assert rows(processes)[0]["process_state"] == "SUSPENDED"


def test_cycle_in_client_ancestry_does_not_hang():
    processes = tree()
    processes[150] = info(150, 151, "tmux")
    processes[151] = info(151, 150, "fish")
    assert len(rows(processes, [client(pid=150, tty="")])) == 1


@pytest.mark.parametrize(
    "value,expected",
    [
        (b"192.0.2.1 50123 192.0.2.2 22", "192.0.2.1"),
        (b"2001:db8::1 50123 2001:db8::2 22", "2001:db8::1"),
        (b"not-an-ip 50123 192.0.2.2 22", None),
        (b"192.0.2.1 -1 192.0.2.2 22", None),
        (b"192.0.2.1 50123 192.0.2.2 65536", None),
        (b"192.0.2.1 50123 192.0.2.2 22 extra", None),
        (b"\xff 50123 192.0.2.2 22", None),
    ],
)
def test_peer_reads_only_valid_connection_metadata(tmp_path, value, expected):
    anchor = tree()[101]
    env = tmp_path / "101/environ"
    env.parent.mkdir()
    env.write_bytes(b"SECRET=do-not-display\0SSH_CONNECTION=" + value + b"\0")
    assert ssh.peer_address(anchor, lambda pid: anchor, tmp_path) == expected


@pytest.mark.parametrize(
    "value",
    [
        b"",
        b"SSH_CONNECTION=192.0.2.1 22 192.0.2.2 22",
        b"SSH_CONNECTION=x\0SSH_CONNECTION=x\0",
    ],
)
def test_missing_truncated_or_duplicate_connection_metadata_is_unknown(tmp_path, value):
    anchor = tree()[101]
    env = tmp_path / "101/environ"
    env.parent.mkdir()
    env.write_bytes(value)
    assert ssh.peer_address(anchor, lambda pid: anchor, tmp_path) is None


def test_peer_read_is_bounded_and_pid_reuse_is_rejected(tmp_path, monkeypatch):
    anchor = tree()[101]
    env = tmp_path / "101/environ"
    env.parent.mkdir()
    env.write_bytes(b"SSH_CONNECTION=192.0.2.1 1234 192.0.2.2 22\0")
    assert (
        ssh.peer_address(anchor, lambda pid: replace(anchor, started=1000), tmp_path)
        is None
    )
    assert (
        ssh.peer_address(
            replace(anchor, uid=os.getuid() + 1), lambda pid: anchor, tmp_path
        )
        is None
    )
    monkeypatch.setattr(ssh, "ENV_LIMIT", 25)
    assert ssh.peer_address(anchor, lambda pid: anchor, tmp_path) is None
    env.unlink()
    assert ssh.peer_address(anchor, lambda pid: anchor, tmp_path) is None


def test_ssh_inventory_works_without_tmux_or_mosh_and_reuses_snapshot(
    backend, monkeypatch
):
    processes = tree()
    reads = []

    def read(lookup):
        reads.append(True)
        return processes, ""

    def lookup(pid):
        if pid not in processes:
            raise FileNotFoundError(pid)
        return processes[pid]

    monkeypatch.setattr(mosh_sessions, "read_processes", read)
    monkeypatch.setattr(clients, "process", lookup)
    monkeypatch.setattr(ssh, "peer_address", lambda anchor, lookup: "192.0.2.1")
    discover = ssh.discover
    monkeypatch.setattr(
        ssh,
        "discover",
        lambda *args: discover(*args, tty_lookup=lambda anchor: "/dev/pts/10"),
    )
    commands = []
    sizes = []

    def read_command(info, lookup):
        commands.append(info)
        return "python Script.py"

    def read_size(tty, device):
        sizes.append((tty, device))
        return 110, 77

    monkeypatch.setattr(clients, "process_command", read_command)
    monkeypatch.setattr(mosh_sessions, "terminal_size", read_size)
    result = clients.inventory(clients.Server(backend.binary, backend.socket))
    assert reads == [True]
    assert (
        not result["clients"]
        and not result["sessions"]
        and not result["other_mosh_sessions"]
    )
    assert result["other_ssh_sessions"][0]["app"] == "python"
    assert result["entries"][0]["transport"] == "SSH"
    assert result["entries"][0]["mosh_pid"] is None
    assert result["entries"][0]["command"] == "python Script.py"
    assert (result["entries"][0]["width"], result["entries"][0]["height"]) == (110, 77)
    assert commands == [processes[102]]
    assert sizes == [("/dev/pts/10", processes[102].tty_device)]
    assert not backend.socket.exists()
