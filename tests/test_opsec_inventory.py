"""Host-only VM viewer detection; never open a VM or an SSH connection in tests."""

import os
import shlex
from dataclasses import replace
from pathlib import Path

import pytest

import mosh_sessions
import opsec_sessions as opsec
import ssh_sessions
import tmux_clients as clients

TTY = "/dev/pts/8"


def command(remote=None, *, route=None, flags=None, target="qwen@opsec-qwen"):
    return shlex.join(
        [
            "/usr/bin/ssh",
            "-F",
            "/dev/null",
            *(route or ["-o", "ControlPath=" + opsec.CONTROL_PATH]),
            *(flags or ["-t"]),
            target,
            remote
            or "/home/qwen/.local/bin/opsec-guest pi /workspace/project --continue",
        ]
    )


def resolved_session(monkeypatch):
    monkeypatch.setattr(
        opsec,
        "resolve_sessions",
        lambda viewers: (
            {
                102: {
                    "session": "pi-actual",
                    "server_socket": "/run/user/1002/opsec-tmux-simple/pi-actual/server.sock",
                }
            },
            "",
        ),
    )


@pytest.mark.parametrize("action", ["pi", "shell"])
@pytest.mark.parametrize("launcher", sorted(opsec.GUEST_LAUNCHERS))
def test_vm_launcher_request_keeps_literal_workspace(action, launcher):
    remote = shlex.join([launcher, action, "/workspace/a b", "--continue"])
    assert opsec.ssh_target(command(remote)) == {
        "target": "opsec-qwen",
        "workspace": "/workspace/a b",
        "action": action,
    }


@pytest.mark.parametrize("verb", ["attach", "attach-session", "new", "new-session"])
def test_explicit_guest_tmux_attach_requests(verb):
    remote = (
        "tmux -S /run/user/1002/opsec-tmux-simple/pi-test/server.sock -N "
        + verb
        + " -s pi-test"
    )
    assert opsec.ssh_target(command(remote)) == {
        "target": "opsec-qwen",
        "workspace": None,
        "guest_socket": "/run/user/1002/opsec-tmux-simple/pi-test/server.sock",
        "session_target": "pi-test",
    }


@pytest.mark.parametrize(
    "route",
    [
        ["-S", opsec.CONTROL_PATH],
        ["-oControlPath=" + opsec.CONTROL_PATH],
        ["-o", "CONTROLpath " + opsec.CONTROL_PATH],
        [
            "-o",
            "ProxyCommand="
            + shlex.join(
                [
                    "/usr/bin/python3",
                    "-I",
                    str(Path.home() / "tasks/opsec/vm/client.py"),
                    "connect",
                ]
            ),
        ],
    ],
)
def test_supported_host_vm_routes(route):
    assert opsec.ssh_target(command(route=route))


@pytest.mark.parametrize("flags", [["-tt"], ["-q", "-t"], ["-o", "RequestTTY=force"]])
def test_supported_pty_requests(flags):
    assert opsec.ssh_target(command(flags=flags))


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "python script-opsec-guest.py",
        "ssh -o",
        command(target="user@other-server"),
        command(route=["-o", "ControlPath=/tmp/not-opsec"]),
        command(route=["-F", "/dev/null"]),
        command(route=["-o", "ProxyCommand=python3 unrelated.py connect"]),
        command(
            route=[
                "-o",
                "ControlPath=/tmp/not-opsec",
                "-o",
                "ControlPath=" + opsec.CONTROL_PATH,
            ]
        ),
        command(flags=["-NT"]),
        command(flags=["-t", "-N"]),
        command(flags=["-t", "-O", "check"]),
        command(flags=["-t", "-W", "target:22"]),
        command(flags=["-t", "-o", "RequestTTY=no"]),
        command(flags=["-v"]),
        command("/home/qwen/.local/bin/opsec-guest run /workspace python app.py"),
        command("/home/qwen/.local/bin/opsec-guest pi relative-path"),
        command("printf opsec-guest pi /workspace"),
        command(
            "/home/qwen/.local/bin/opsec-guest pi /workspace; echo another-command"
        ),
        command(
            "tmux -S /run/user/1002/opsec-tmux-simple/test/server.sock list-clients -F attach"
        ),
        command("tmux -S /tmp/other.sock attach"),
        command("tmux -S /run/user/1002/opsec-tmux-simple/test/server.sock"),
    ],
)
def test_unrelated_background_or_ambiguous_ssh_requests_are_not_vm_viewers(value):
    assert opsec.ssh_target(value) is None


def info(pid, parent, executable, **changes):
    return replace(
        clients.Process(pid, parent, pid, executable, os.getuid(), "S"), **changes
    )


def tree(origin="xfce4-terminal"):
    return {
        100: info(100, 1, origin),
        101: info(101, 100, "fish", pgrp=101, tty_device=123, foreground_pgrp=102),
        102: info(
            102, 101, "python3.14", pgrp=102, tty_device=123, foreground_pgrp=102
        ),
        103: info(103, 102, "ssh", pgrp=102, tty_device=123, foreground_pgrp=102),
    }


def test_python_clipboard_wrapper_and_ssh_child_form_one_viewer():
    processes = tree()
    processes[104] = replace(processes[103], pid=104, started=104)
    rows = opsec.discover(processes, lambda pid: command(), tty_lookup=lambda app: TTY)
    assert rows == {
        102: {
            "target": "opsec-qwen",
            "workspace": "/workspace/project",
            "action": "pi",
            "ssh_pid": 103,
            "app_pid": 102,
            "tty": TTY,
        }
    }


def test_multiple_terminal_viewers_keep_their_distinct_processes():
    processes = tree()
    for pid, app in list(processes.items()):
        processes[pid + 200] = replace(
            app,
            pid=pid + 200,
            parent=app.parent + 200 if app.parent > 1 else 1,
            started=app.started + 200,
            pgrp=app.pgrp + 200 if app.pgrp else 0,
            foreground_pgrp=app.foreground_pgrp + 200 if app.foreground_pgrp else 0,
            tty_device=456 if app.tty_device else 0,
        )
    rows = opsec.discover(processes, lambda pid: command(), tty_lookup=lambda app: TTY)
    assert set(rows) == {102, 302}
    assert {row["ssh_pid"] for row in rows.values()} == {103, 303}


@pytest.mark.parametrize(
    "change",
    [
        {"state": "Z"},
        {"state": "X"},
        {"uid": os.getuid() + 1},
        {"pgrp": 103},
        {"tty_device": 0},
        {"parent": 999},
    ],
)
def test_dead_foreign_background_or_disconnected_process_is_not_a_viewer(change):
    processes = tree()
    processes[103] = replace(processes[103], **change)
    assert (
        opsec.discover(processes, lambda pid: command(), tty_lookup=lambda app: TTY)
        == {}
    )


def test_wrong_ancestry_and_missing_terminal_are_not_fabricated():
    processes = tree()
    assert (
        opsec.discover(processes, lambda pid: command(), tty_lookup=lambda app: None)
        == {}
    )
    processes[104] = info(104, 103, "wrapper")
    processes[103] = replace(processes[103], parent=104)
    assert (
        opsec.discover(processes, lambda pid: command(), tty_lookup=lambda app: TTY)
        == {}
    )


@pytest.fixture
def fake_inventory(monkeypatch, tmp_path):
    processes = tree()
    commands, scans = [], []

    def read(lookup):
        scans.append(True)
        return processes, ""

    def lookup(pid):
        if pid not in processes:
            raise FileNotFoundError(pid)
        return processes[pid]

    def argv(app, lookup):
        if app is None:
            return None
        commands.append(app.pid)
        return (
            command()
            if app.pid == 103
            else "python3 vm/client.py pi --workspace /workspace/project"
        )

    monkeypatch.setattr(mosh_sessions, "read_processes", read)
    monkeypatch.setattr(clients, "process", lookup)
    monkeypatch.setattr(clients, "process_command", argv)
    monkeypatch.setattr(opsec, "process_tty", lambda app: TTY)
    monkeypatch.setattr(mosh_sessions, "terminal_size", lambda *args: (120, 40))
    monkeypatch.setattr(opsec, "resolve_sessions", lambda viewers: ({}, ""))
    server = clients.Server(Path("/nonexistent-tmux"), tmp_path / "absent.sock")
    return server, processes, commands, scans


@pytest.mark.parametrize("resolved", [True, False])
@pytest.mark.parametrize("wrapper", ["bash", "python3.14"])
def test_host_table_adds_local_vm_viewer_even_without_a_host_tmux_server(
    fake_inventory, monkeypatch, capsys, resolved, wrapper
):
    server, processes, commands, scans = fake_inventory
    processes[102] = replace(processes[102], executable=wrapper)
    if resolved:
        resolved_session(monkeypatch)
    result = clients.inventory(server)
    assert not result["server_running"] and not server.socket.exists()
    assert result["clients"] == result["sessions"] == []
    assert len(result["other_opsec_sessions"]) == len(result["entries"]) == 1
    (row,) = result["entries"]
    assert row["type"] == "OPSEC-TMUX" and row["via"] == "xfce4-terminal"
    assert row["app_pid"] == 102 and row["app"] == wrapper
    assert row["app_label"] == "pi-opsec"
    assert row["session"] == ("pi-actual" if resolved else None)
    assert row["peer"] is None and row["sizing"] is None
    assert (row["width"], row["height"]) == (120, 40)
    assert scans == [True] and commands.count(102) == commands.count(103) == 1
    monkeypatch.setattr(clients, "inventory", lambda server: result)
    clients.show_clients(server)
    text = capsys.readouterr().out
    assert "opsec-tmux" in text and "xfce4-terminal" in text
    assert "pi-opsec (102)" in text and f"{wrapper} (102)" not in text
    assert "TYPE" in text and "OPSEC-TMUX" not in text
    assert ("pi-actual" in text) == resolved


@pytest.mark.parametrize("resolved", [True, False])
@pytest.mark.parametrize("origin", ["mosh-server", "sshd-session"])
def test_existing_remote_rows_are_retyped_without_duplicates(
    fake_inventory, monkeypatch, origin, resolved
):
    server, processes, _, scans = fake_inventory
    if resolved:
        resolved_session(monkeypatch)
    processes[100] = replace(processes[100], executable=origin)
    monkeypatch.setattr(
        clients, "login_records", lambda: ({(100, TTY): ("CONNECTED", "100.1.2.3")}, "")
    )
    monkeypatch.setattr(mosh_sessions, "tty_matches", lambda path, device: path == TTY)
    monkeypatch.setattr(clients.mosh_status, "read", lambda *args: {"idle_seconds": 0})
    discover = ssh_sessions.discover
    monkeypatch.setattr(
        ssh_sessions,
        "discover",
        lambda *args: discover(
            *args, tty_lookup=lambda app: TTY, peer_lookup=lambda app: "100.1.2.3"
        ),
    )
    result = clients.inventory(server)
    assert scans == [True]
    assert result["other_opsec_sessions"] == []
    (row,) = result["entries"]
    assert row["type"] == "OPSEC-TMUX" and row["peer"] == "100.1.2.3"
    assert row["app_label"] == "pi-opsec"
    assert row["session"] == ("pi-actual" if resolved else None)
    assert row["via"] == ("mosh" if origin == "mosh-server" else "ssh")
    assert row["idle_seconds"] == (0 if origin == "mosh-server" else None)


@pytest.mark.parametrize("resolved", [True, False])
@pytest.mark.parametrize("attached", [True, False])
def test_existing_host_tmux_viewer_or_detached_pane_is_retyped_without_duplicate(
    fake_inventory, monkeypatch, attached, resolved
):
    server, processes, _, _ = fake_inventory
    if resolved:
        resolved_session(monkeypatch)
    server.socket.touch()
    processes[200] = info(200, 100, "tmux")
    client = clients.Client(
        200, "/dev/pts/1", "host", 140, 50, set(), session_id="$1", pane_pid=101
    )
    server.sessions = [clients.Session("$1", "host", int(attached), 1, 1, 101)]
    monkeypatch.setattr(
        server, "snapshot", lambda **kwargs: ([client] if attached else [], set())
    )
    result = clients.inventory(server)
    assert result["other_opsec_sessions"] == []
    (row,) = result["entries"]
    assert row["type"] == "OPSEC-TMUX"
    assert row["app_label"] == "pi-opsec"
    assert row["session"] == ("pi-actual" if resolved else None)
    assert result["sessions"][0]["name"] == "host"
    if attached:
        assert result["clients"][0]["session"] == "host"
    assert row["via"] == ("xfce4-terminal" if attached else "detached")
    assert row["width"] == (140 if attached else None)


@pytest.mark.parametrize("view", ["attached", "detached", "mosh", "ssh"])
def test_waiting_shell_reports_real_pi_child_and_pid(
    fake_inventory, monkeypatch, capsys, view
):
    server, processes, commands, _ = fake_inventory
    processes[102] = replace(processes[102], executable="bash")
    processes[103] = replace(processes[103], executable="node")
    processes[104] = info(
        104, 102, "ssh", pgrp=102, tty_device=123, foreground_pgrp=102
    )
    if view in ("attached", "detached"):
        attached = view == "attached"
        server.socket.touch()
        processes[200] = info(200, 100, "tmux")
        client = clients.Client(
            200, "/dev/pts/1", "host", 140, 50, set(), session_id="$1", pane_pid=101
        )
        server.sessions = [clients.Session("$1", "host", int(attached), 1, 1, 101)]
        monkeypatch.setattr(
            server, "snapshot", lambda **kwargs: ([client] if attached else [], set())
        )
    else:
        processes[100] = replace(
            processes[100],
            executable="mosh-server" if view == "mosh" else "sshd-session",
        )
        monkeypatch.setattr(
            clients,
            "login_records",
            lambda: ({(100, TTY): ("CONNECTED", "100.1.2.3")}, ""),
        )
        monkeypatch.setattr(
            mosh_sessions, "tty_matches", lambda path, device: path == TTY
        )
        extra = mosh_sessions.extra_sessions
        monkeypatch.setattr(
            mosh_sessions,
            "extra_sessions",
            lambda *args: extra(*args, tty_lookup=lambda app: TTY),
        )
        ssh = ssh_sessions.discover
        monkeypatch.setattr(
            ssh_sessions,
            "discover",
            lambda *args: ssh(
                *args, tty_lookup=lambda app: TTY, peer_lookup=lambda app: "100.1.2.3"
            ),
        )

    def argv(app, lookup):
        if app is None:
            return None
        commands.append(app.pid)
        return {102: "bash /proc/self/fd/3", 103: "pi", 104: "ssh -N -T elsewhere"}.get(
            app.pid
        )

    monkeypatch.setattr(clients, "process_command", argv)
    result = clients.inventory(server)
    (row,) = result["entries"]
    assert row["app"] == "node" and row["app_label"] == "pi"
    assert row["app_pid"] == 103 and row["command"] == "pi"
    assert commands.count(103) == 1
    monkeypatch.setattr(clients, "inventory", lambda server: result)
    clients.show_clients(server)
    text = capsys.readouterr().out
    assert "pi (103)" in text and "bash (102)" not in text


@pytest.mark.parametrize(
    ("target", "label"),
    [
        ({"action": "pi"}, "pi-opsec"),
        ({"action": "shell", "session": "pi-old-name"}, None),
        ({"session": "pi-actual"}, "pi-opsec"),
        ({"session": "shell-actual"}, None),
        ({}, None),
    ],
)
def test_vm_shells_and_unknown_requests_are_not_mislabelled_pi(
    fake_inventory, monkeypatch, target, label
):
    server, _, _, _ = fake_inventory
    monkeypatch.setattr(
        opsec,
        "discover",
        lambda *args: {102: {**target, "app_pid": 102, "ssh_pid": 103, "tty": TTY}},
    )
    (row,) = clients.inventory(server)["entries"]
    assert row["app_label"] == label
    assert row["app_pid"] == 102 and row["app"] == "python3.14"
