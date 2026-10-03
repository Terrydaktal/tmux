"""One shared schema for tmux attachments and direct Mosh/SSH sessions."""

import json
import os
import re
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import mosh_sessions
import tmux_clients as clients

BUILD_TIME = datetime(2026, 9, 30, 16, 32, tzinfo=UTC).timestamp()
LOCAL_VIA = "xfce4-terminal"
MOSH_VIA = "mosh"
DIRECT_VIA = "mosh"


@pytest.fixture(autouse=True)
def utc_dates(monkeypatch):
    monkeypatch.setattr(clients.time, "localtime", clients.time.gmtime)


@pytest.fixture
def snapshot():
    local = {
        "session": "work",
        "session_id": "$1",
        "app": "codex",
        "app_pid": 11,
        "command": "codex resume Work",
        "transport": "LOCAL",
        "state": "ATTACHED",
        "attachment": "ATTACHED",
        "reachability": None,
        "tty": "/dev/pts/1",
        "frontend": "xfce4-terminal",
        "frontend_pid": 21,
        "frontend_binary_mtime": BUILD_TIME,
        "idle_seconds": 61,
        "peer": "-",
        "width": 120,
        "height": 40,
        "sizing": "participating",
        "window_id": "@1",
        "sizing_policy": "latest",
        "sizing_client_pid": 1,
        "active_sizing": True,
    }
    remote = local | {
        "transport": "MOSH",
        "tty": "/dev/pts/2",
        "frontend_pid": 22,
        "frontend_binary_mtime": BUILD_TIME + 60,
        "state": "UNREACHABLE",
        "reachability": "UNREACHABLE",
        "sizing": "ignored-auto",
        "active_sizing": False,
    }
    direct = {
        "pid": 30,
        "frontend_binary_mtime": BUILD_TIME + 120,
        "mode": "DIRECT",
        "app": "fish",
        "app_pid": 31,
        "command": "fish -l",
        "process_state": "RUNNING",
        "reachability": "RECENT",
        "tty": "/dev/pts/3",
        "idle_seconds": None,
        "peer": "100.1.2.3",
        "width": 110,
        "height": 77,
    }
    return {
        "snapshot_at": 1790000000,
        "server_running": True,
        "server_binary_mtime": BUILD_TIME + 180,
        "monitor_running": True,
        "note": "",
        "clients": [local, remote],
        "sessions": [
            {
                "id": "$1",
                "name": "work",
                "attached_clients": 2,
                "idle_seconds": 61,
                "app": "codex",
                "app_pid": 11,
            },
            {
                "id": "$2",
                "name": "quiet",
                "attached_clients": 0,
                "idle_seconds": 123,
                "app": "python",
                "app_pid": 12,
                "command": "python Script.py",
            },
            {
                "id": "$3",
                "name": "changing",
                "attached_clients": 1,
                "idle_seconds": 1,
                "app": None,
                "app_pid": None,
            },
        ],
        "other_mosh_sessions": [
            direct,
            direct | {"pid": 40, "mode": "OTHER-TMUX", "app": "tmux", "app_pid": 41},
        ],
        "other_ssh_sessions": [
            direct
            | {
                "pid": 51,
                "ssh_pid": 50,
                "app_pid": 51,
                "tty": "/dev/pts/4",
                "reachability": None,
            }
        ],
    }


def test_one_uniform_schema_preserves_connection_and_process_meanings(snapshot):
    original = deepcopy(snapshot)
    rows = clients.unified_entries(snapshot)
    assert snapshot == original
    assert len(rows) == 7
    assert all(row.keys() == rows[0].keys() for row in rows)
    local, remote = [row for row in rows if row["session"] == "work"]
    assert local["app_pid"] == remote["app_pid"] == 11
    assert local["command"] == remote["command"] == "codex resume Work"
    assert local["via"] == LOCAL_VIA and remote["via"] == MOSH_VIA
    assert local["frontend_binary_mtime"] == BUILD_TIME
    assert remote["frontend_binary_mtime"] == BUILD_TIME + 60
    assert local["tmux_binary_mtime"] == remote["tmux_binary_mtime"] == BUILD_TIME + 180
    assert local["mosh_pid"] is None and remote["mosh_pid"] == 22
    assert remote["state"] == "ATTACHED" and remote["reachability"] == "UNREACHABLE"
    (direct,) = [
        row for row in rows if row["type"] == "DIRECT" and row["transport"] == "MOSH"
    ]
    assert direct["app_pid"] == 31 and direct["mosh_pid"] == 30
    assert direct["transport"] == "MOSH" and direct["state"] == "RUNNING"
    assert direct["via"] == DIRECT_VIA + " [recent]"
    assert all(direct[key] is None for key in ("session", "idle_seconds", "sizing"))
    assert (direct["width"], direct["height"]) == (110, 77)
    assert direct["command"] == "fish -l"
    (detached,) = [row for row in rows if row["session"] == "quiet"]
    assert detached["state"] == "DETACHED" and detached["app_pid"] == 12
    assert detached["transport"] is None and detached["tty"] is None
    assert detached["via"] == "detached"
    assert detached["tmux_binary_mtime"] == BUILD_TIME + 180
    assert detached["command"] == "python Script.py"
    (changing,) = [row for row in rows if row["session"] == "changing"]
    assert changing["state"] == "CHANGING"
    assert changing["via"] == "changing"
    (ssh,) = [row for row in rows if row["transport"] == "SSH"]
    assert ssh["type"] == "DIRECT" and ssh["app_pid"] == 51
    assert ssh["mosh_pid"] is None and ssh["reachability"] is None
    assert ssh["via"] == "ssh"


@pytest.mark.parametrize("kind", ["attached", "detached", "direct"])
@pytest.mark.parametrize("guest_stamp", [None, BUILD_TIME + 86400])
@pytest.mark.parametrize("guest_outdated", [True, False, None])
def test_vm_type_uses_guest_binary_date_never_the_host_date(
    snapshot, monkeypatch, capsys, kind, guest_stamp, guest_outdated
):
    snapshot["server_binary_outdated"] = True
    connection = {
        "session": "pi-live",
        "server_binary_mtime": guest_stamp,
        "server_binary_outdated": guest_outdated,
    }
    if kind == "attached":
        row = snapshot["clients"][0] | {
            "type": "OPSEC-TMUX",
            "opsec_connection": connection,
        }
        snapshot.update(clients=[row], sessions=[])
    elif kind == "detached":
        row = snapshot["sessions"][1] | {
            "type": "OPSEC-TMUX",
            "opsec_connection": connection,
        }
        snapshot.update(clients=[], sessions=[row])
    else:
        row = snapshot["other_mosh_sessions"][0] | {
            "mode": "OPSEC-TMUX",
            "transport": "LOCAL",
            "frontend": "xfce4-terminal",
            "opsec_connection": connection,
        }
        snapshot.update(clients=[], sessions=[], other_opsec_sessions=[row])
    snapshot.update(other_mosh_sessions=[], other_ssh_sessions=[])
    (entry,) = clients.unified_entries(snapshot)
    assert entry["type"] == "OPSEC-TMUX" and entry["session"] == "pi-live"
    assert entry["tmux_binary_mtime"] == guest_stamp
    assert entry["tmux_outdated"] is guest_outdated
    snapshot["entries"] = [entry]
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    clients.show_clients_flat(SimpleNamespace(socket=Path("/private/test.sock")))
    data = re.split(r" {2,}", capsys.readouterr().out.splitlines()[-1])
    assert data[1] == "opsec-tmux"
    assert "2026-" not in data


@pytest.mark.parametrize("verbose", [False, True])
def test_combined_output_has_one_header_and_aligned_shared_columns(
    snapshot, monkeypatch, capsys, verbose
):
    snapshot["entries"] = clients.unified_entries(snapshot)
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    clients.show_clients_flat(
        SimpleNamespace(socket=Path("/private/test.sock")), verbose=verbose
    )
    text = capsys.readouterr().out
    assert "Other Mosh Sessions" not in text
    lines = text.splitlines()
    headers = [line for line in lines if line.startswith("VIA ")]
    assert len(headers) == 1
    header = re.split(r" {2,}", headers[0])
    assert header == [
        "VIA",
        "TYPE",
        "SESSION",
        "APP",
        "IDLE",
        "PEER",
        "SIZE",
        "SIZING",
    ]
    table_start = lines.index(headers[0]) + 1
    data = [re.split(r" {2,}", line) for line in lines[table_start:]]
    assert len(data) == 7 and all(len(row) == len(header) for row in data)
    rows = [dict(zip(header, row)) for row in data]
    assert [(row["VIA"], row["TYPE"], row["SESSION"], row["APP"]) for row in rows] == [
        (
            entry["via"],
            entry["type"].lower(),
            entry["session"] or "-",
            clients.format_process(entry["app"], entry["app_pid"]).lower(),
        )
        for entry in snapshot["entries"]
    ]
    (direct,) = [
        row
        for row in rows
        if row["TYPE"] == "direct" and row["VIA"] == DIRECT_VIA + " [recent]"
    ]
    assert direct["APP"] == "fish (31)"
    assert direct["IDLE"] == "-"
    assert direct["SIZE"] == "110x77"
    assert next(row for row in rows if row["SESSION"] == "quiet")["VIA"] == "detached"
    assert (
        next(row for row in rows if row["SESSION"] == "changing")["VIA"] == "changing"
    )
    (ssh,) = [row for row in rows if row["VIA"] == "ssh"]
    assert ssh["TYPE"] == "direct" and ssh["APP"] == "fish (51)"
    assert ssh["SIZE"] == "110x77"
    assert all(value == value.lower() for row in rows for value in row.values())
    assert next(row for row in rows if row["VIA"] == LOCAL_VIA)["SIZING"] == "active"
    assert next(row for row in rows if row["VIA"] == MOSH_VIA)["SIZING"] == "auto-off"
    assert all(value == value.upper() for value in header)
    assert not {"STATE", "APP PID", "MOSH PID", "LINK", "COMMAND", "TTY"} & set(header)
    assert "pts/" not in text
    assert "auto-off" in text and "1m01s" in text


@pytest.mark.parametrize("verbose", [False, True])
def test_clients_output_ends_at_the_table_and_keeps_notes_in_json(
    snapshot, monkeypatch, capsys, verbose
):
    snapshot["note"] = "Active sizing unavailable on this older tmux server."
    snapshot["entries"] = clients.unified_entries(snapshot)
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    server = SimpleNamespace(socket=Path("/private/test.sock"))
    clients.show_clients_flat(server, verbose=verbose)
    text = capsys.readouterr().out
    assert text.splitlines()[-1].startswith(DIRECT_VIA)
    assert snapshot["note"] not in text
    assert not any(
        line.startswith(prefix)
        for line in text.splitlines()
        for prefix in (
            "Scope:",
            "Snapshot only",
            "TYPE:",
            "APP =",
            "SIZE =",
            "COMMAND =",
            "VIA:",
            "VIA/TYPE",
            "IDLE =",
            "Mosh [",
            "SIZING:",
        )
    )
    clients.show_clients_flat(server, as_json=True)
    assert json.loads(capsys.readouterr().out)["note"] == snapshot["note"]


@pytest.mark.parametrize(
    "frontend_outdated,tmux_outdated",
    [(True, True), (True, False), (False, True), (False, False), (None, None)],
)
def test_red_background_marks_old_or_unverified_mosh_and_preserves_alignment(
    snapshot, monkeypatch, capsys, frontend_outdated, tmux_outdated
):
    for row in snapshot["clients"] + snapshot["other_mosh_sessions"]:
        row["frontend_outdated"] = frontend_outdated
    snapshot["server_binary_outdated"] = tmux_outdated
    snapshot["entries"] = clients.unified_entries(snapshot)
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    monkeypatch.setattr(clients.sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    server = SimpleNamespace(socket=Path("/private/test.sock"))
    clients.show_clients_flat(server)
    colored = capsys.readouterr().out
    assert ("\033[41mxfce4-terminal\033[0m" in colored) is (frontend_outdated is True)
    assert ("\033[41mmosh\033[0m [recent]" in colored) is (
        frontend_outdated is not False
    )
    assert ("\033[41mtmux\033[0m" in colored) is (tmux_outdated is True)
    assert "\033[31m" not in colored and "BUILDVER" not in colored
    monkeypatch.setattr(clients.sys.stdout, "isatty", lambda: False)
    clients.show_clients_flat(server)
    plain = capsys.readouterr().out
    assert re.sub(r"\033\[[0-9;]*m", "", colored) == plain
    assert "\033" not in plain
    clients.show_clients_flat(server, as_json=True)
    encoded = capsys.readouterr().out
    assert "\033" not in encoded
    assert json.loads(encoded) == snapshot


@pytest.mark.parametrize("disabled_by", ["redirected", "dumb", "no_color"])
def test_terminal_only_highlights_respect_plain_output_settings(
    snapshot, monkeypatch, capsys, disabled_by
):
    snapshot["clients"][0]["frontend_outdated"] = True
    snapshot["server_binary_outdated"] = True
    snapshot["entries"] = clients.unified_entries(snapshot)
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    monkeypatch.setattr(
        clients.sys.stdout, "isatty", lambda: disabled_by != "redirected"
    )
    monkeypatch.setenv("TERM", "dumb" if disabled_by == "dumb" else "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    if disabled_by == "no_color":
        monkeypatch.setenv("NO_COLOR", "")
    clients.show_clients_flat(SimpleNamespace(socket=Path("/private/test.sock")))
    assert "\033" not in capsys.readouterr().out


def test_vm_tmux_background_uses_guest_comparison_not_the_hosts(
    snapshot, monkeypatch, capsys
):
    snapshot["server_binary_outdated"] = True
    snapshot["clients"][0].update(
        type="OPSEC-TMUX",
        opsec_connection={"session": "pi-live", "server_binary_outdated": False},
    )
    snapshot["entries"] = clients.unified_entries(snapshot)
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    monkeypatch.setattr(clients.sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    clients.show_clients_flat(SimpleNamespace(socket=Path("/private/test.sock")))
    text = capsys.readouterr().out
    assert "\033[41mtmux\033[0m" in text
    assert "\033[41mopsec-tmux" not in text
    snapshot["clients"][0]["opsec_connection"]["server_binary_outdated"] = True
    snapshot["entries"] = clients.unified_entries(snapshot)
    clients.show_clients_flat(SimpleNamespace(socket=Path("/private/test.sock")))
    assert "\033[41mopsec-tmux\033[0m" in capsys.readouterr().out


@pytest.mark.parametrize("verbose", [False, True])
def test_missing_build_dates_do_not_restore_build_columns(
    snapshot, monkeypatch, capsys, verbose
):
    snapshot["server_binary_mtime"] = None
    for row in snapshot["clients"] + snapshot["other_mosh_sessions"]:
        row["frontend_binary_mtime"] = None
    snapshot["entries"] = clients.unified_entries(snapshot)
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    clients.show_clients_flat(
        SimpleNamespace(socket=Path("/private/test.sock")), verbose=verbose
    )
    lines = capsys.readouterr().out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("VIA ")) + 1
    for line in lines[start:]:
        row = re.split(r" {2,}", line)
        assert len(row) == 8
        assert "(" not in row[0] and "(" not in row[1]
    assert "BUILDVER" not in lines[start - 1]


def test_direct_app_never_claims_an_outdated_tmux_version(
    snapshot, monkeypatch, capsys
):
    snapshot["entries"] = clients.unified_entries(snapshot)
    monkeypatch.setattr(clients.sys.stdout, "isatty", lambda: True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    for row in snapshot["entries"]:
        if row["type"] == "DIRECT":
            row["tmux_binary_mtime"] = BUILD_TIME
            row["tmux_outdated"] = True
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    clients.show_clients_flat(SimpleNamespace(socket=Path("/private/test.sock")))
    for line in capsys.readouterr().out.splitlines():
        row = re.split(r" {2,}", line)
        if len(row) == 8 and row[1] == "direct":
            assert "\033" not in row[1]


@pytest.mark.parametrize("active_index", [0, 1], ids=["desktop", "mosh"])
def test_active_sizing_indicator_identifies_desktop_or_mosh_viewer(
    snapshot, monkeypatch, capsys, active_index
):
    for index, row in enumerate(snapshot["clients"]):
        row.update(sizing="participating", active_sizing=index == active_index)
    snapshot["entries"] = clients.unified_entries(snapshot)
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    clients.show_clients_flat(SimpleNamespace(socket=Path("/private/test.sock")))
    lines = capsys.readouterr().out.splitlines()
    header = next(line for line in lines if line.startswith("VIA "))
    keys = re.split(r" {2,}", header)
    rows = [
        dict(zip(keys, re.split(r" {2,}", line)))
        for line in lines
        if line.startswith((LOCAL_VIA, MOSH_VIA))
    ]
    active = next(row for row in rows if row["SIZING"] == "active")
    assert active["VIA"] == [LOCAL_VIA, MOSH_VIA][active_index]
    assert [row["SIZING"] for row in rows].count("standby") == 1


@pytest.mark.parametrize("idle", [None, 0, 61])
@pytest.mark.parametrize("reachability", ["RECENT", "UNREACHABLE", "UNKNOWN", None])
def test_mosh_label_retains_coarse_flag_only_without_packet_age(idle, reachability):
    label = clients.format_via(
        "MOSH",
        idle_seconds=idle,
        reachability=reachability,
    )
    assert label == MOSH_VIA + (
        f" [{(reachability or 'unknown').lower()}]" if idle is None else ""
    )


@pytest.mark.parametrize(
    "name,pid,expected",
    [
        ("codex", 1234, "codex (1234)"),
        ("fish", None, "fish"),
        (None, None, "-"),
        (None, 1234, "- (1234)"),
        ("fish", 0, "fish"),
        ("fish", True, "fish"),
    ],
)
def test_process_label_never_invents_a_pid(name, pid, expected):
    assert clients.format_process(name, pid) == expected


def test_lowercase_table_keeps_actual_names_and_json_unchanged(
    snapshot, monkeypatch, capsys
):
    snapshot["clients"][0].update(session="MyWork", app="MyApp", peer="FE80::ABCD")
    snapshot["clients"][1]["idle_seconds"] = None
    snapshot["entries"] = clients.unified_entries(snapshot)
    before = deepcopy(snapshot)
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    clients.show_clients_flat(SimpleNamespace(socket=Path("/private/test.sock")))
    text = capsys.readouterr().out
    assert "mywork" in text and "myapp" in text and "fe80::abcd" in text
    assert "MyWork" not in text and "MyApp" not in text and "UNREACHABLE" not in text
    assert "unreachable" in text and "other-tmux" in text
    assert snapshot == before
    clients.show_clients_flat(
        SimpleNamespace(socket=Path("/private/test.sock")), as_json=True
    )
    assert json.loads(capsys.readouterr().out) == before


@pytest.mark.parametrize(
    "transport,frontend,state,expected",
    [
        ("LOCAL", "xfce4-terminal", None, LOCAL_VIA),
        ("LOCAL", "konsole", None, "konsole"),
        ("LOCAL", None, None, "local"),
        ("MOSH", "mosh-server", None, MOSH_VIA + " [unknown]"),
        ("SSH", "sshd-session", None, "ssh"),
        ("UNKNOWN", None, None, "unknown"),
        (None, None, "DETACHED", "detached"),
        (None, None, "CHANGING", "changing"),
    ],
)
def test_via_label_keeps_dates_and_pids_out_of_the_name(
    transport, frontend, state, expected
):
    assert clients.format_via(transport, frontend, state) == expected


def test_json_keeps_legacy_arrays_and_adds_the_combined_entries(
    snapshot, monkeypatch, capsys
):
    snapshot["entries"] = clients.unified_entries(snapshot)
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    clients.show_clients_flat(
        SimpleNamespace(socket=Path("/private/test.sock")), as_json=True
    )
    result = json.loads(capsys.readouterr().out)
    assert result == snapshot
    assert len(result["clients"]) == 2 and len(result["entries"]) == 7
    assert len(result["other_ssh_sessions"]) == 1


@pytest.mark.parametrize("verbose", [False, True])
def test_command_is_hidden_in_text_but_preserved_in_json(
    snapshot, monkeypatch, capsys, verbose
):
    command = (
        "/very/long/runtime/path/python3.14 /Projects/Script.py " + "Argument" * 30
    )
    snapshot["other_mosh_sessions"][0]["command"] = command
    snapshot["entries"] = clients.unified_entries(snapshot)
    before = deepcopy(snapshot)
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    server = SimpleNamespace(socket=Path("/private/test.sock"))
    clients.show_clients_flat(server, verbose=verbose)
    text = capsys.readouterr().out
    lines = text.splitlines()
    header = next(line for line in lines if line.startswith("VIA "))
    assert "COMMAND" not in header
    assert "/projects/script.py" not in text
    for entry in snapshot["entries"]:
        if entry.get("command"):
            assert entry["command"].lower() not in text
    direct = next(line for line in lines if line.startswith(DIRECT_VIA + " [recent]"))
    assert len(re.split(r" {2,}", direct)) == 8
    clients.show_clients_flat(server, as_json=True)
    assert json.loads(capsys.readouterr().out) == before


def test_command_control_characters_cannot_break_the_table(
    snapshot, monkeypatch, capsys
):
    snapshot["other_mosh_sessions"][0]["command"] = "python 'script\n\x1b[2J.py'"
    snapshot["entries"] = clients.unified_entries(snapshot)
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    clients.show_clients_flat(
        SimpleNamespace(socket=Path("/private/test.sock")), verbose=True
    )
    text = capsys.readouterr().out
    direct = next(
        line for line in text.splitlines() if line.startswith(DIRECT_VIA + " [recent]")
    )
    assert len(re.split(r" {2,}", direct)) == 8
    assert "script" not in text
    assert "\x1b" not in text


def test_no_server_and_empty_inventory_still_render_one_table(
    snapshot, monkeypatch, capsys
):
    snapshot.update(
        clients=[],
        sessions=[],
        other_mosh_sessions=[],
        other_ssh_sessions=[],
        entries=[],
        server_running=False,
        monitor_running=False,
    )
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    clients.show_clients_flat(SimpleNamespace(socket=Path("/absent.sock")))
    text = capsys.readouterr().out
    assert "No tmux server at /absent.sock" in text
    assert sum(line.startswith("VIA ") for line in text.splitlines()) == 1


def test_suspended_mosh_is_not_claimed_as_running(snapshot):
    snapshot["other_mosh_sessions"][0]["process_state"] = "SUSPENDED"
    (direct,) = [
        row
        for row in clients.unified_entries(snapshot)
        if row["type"] == "DIRECT" and row["transport"] == "MOSH"
    ]
    assert direct["state"] == "SUSPENDED"


def test_foreground_app_pid_is_not_the_waiting_pane_shell_pid():
    shell = clients.Process(100, 1, 1, "fish", os.getuid(), "S", 100, 123, 101)
    app = clients.Process(101, 100, 2, "codex", os.getuid(), "S", 101, 123, 101)
    unrelated = replace(app, pid=102, tty_device=456)
    assert mosh_sessions.foreground_app(shell, [shell, unrelated, app]) == app
    assert mosh_sessions.foreground_app(shell, [shell, unrelated]) is None
    assert (
        mosh_sessions.foreground_app(shell, [replace(app, uid=os.getuid() + 1)]) is None
    )
    assert mosh_sessions.foreground_app(None, [shell, app]) is None
    assert mosh_sessions.foreground_app(replace(shell, tty_device=0), [app]) is None


def test_real_tmux_app_is_reported_without_an_extra_process_scan(backend, monkeypatch):
    app = backend.attach()
    second = None
    try:
        app.until(lambda state: b"READY" in state["screen"])
        second = backend.attach(existing=True)
        second.until(lambda state: b"READY" in state["screen"])
        original = mosh_sessions.read_processes
        read_command = clients.process_command
        scans = []
        commands = []

        def record(lookup):
            scans.append(True)
            return original(lookup)

        def record_command(info, lookup):
            if info:
                commands.append(info.pid)
            return read_command(info, lookup)

        monkeypatch.setattr(mosh_sessions, "read_processes", record)
        monkeypatch.setattr(clients, "process_command", record_command)
        result = clients.inventory(clients.Server(backend.binary, backend.socket))
        rows = [row for row in result["entries"] if row["session"] == "test"]
        assert len(rows) == 2 and rows[0]["command"] == rows[1]["command"]
        row = rows[0]
        assert row["app"].startswith("python")
        assert row["app_pid"] == int(backend.field("pane_pid"))
        assert str(Path(__file__).parent / "workload.py") in row["command"]
        assert commands.count(row["app_pid"]) == 1
        assert scans == [True]
        server_pid = int(backend.field("pid"))
        stamp = Path(f"/proc/{server_pid}/exe").stat().st_mtime
        assert result["server_binary_mtime"] == stamp
        assert all(entry["tmux_binary_mtime"] == stamp for entry in rows)
    finally:
        app.close()
        if second:
            second.close()
