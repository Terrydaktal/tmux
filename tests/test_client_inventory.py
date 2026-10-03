"""Client attachments, detached sessions and activity must remain distinct."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import tmux_clients as clients


def query(backend, as_json=True):
    result = subprocess.run(
        backend.mosh_cli("clients", *(["--json"] if as_json else [])),
        env=backend.env,
        capture_output=True,
        text=True,
        check=False,
        timeout=6,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if as_json else result.stdout


def detached(backend):
    subprocess.run(
        backend.cli("new-session", "-d", "-s", "quiet", "--", "sleep", "60"),
        env=backend.env,
        capture_output=True,
        check=True,
        timeout=5,
    )


def test_no_server_inventory_has_the_same_schema_and_does_not_start_one(backend):
    result = query(backend)
    assert result["snapshot_at"] > 0
    assert result["socket"] == str(backend.socket)
    assert result["sessions"] == result["clients"] == []
    assert not result["monitor_running"]
    assert not backend.socket.exists()


def test_never_attached_session_is_reported_without_creating_a_client(backend):
    detached(backend)
    before = backend.run("display-message", "-p", "#{pane_pid}").stdout
    result = query(backend)
    assert result["clients"] == []
    assert len(result["sessions"]) == 1
    session = result["sessions"][0]
    assert session["name"] == "quiet"
    assert session["attached_clients"] == 0
    assert session["created_at"] > 0
    assert session["last_activity_at"] > 0
    text = query(backend, as_json=False)
    assert any(
        line.lstrip().startswith("tmux ") and " quiet " in line and line.endswith(" 0")
        for line in text.splitlines()
    )
    assert "Snapshot only" not in text and "authenticated client packet" not in text
    assert backend.run("display-message", "-p", "#{pane_pid}").stdout == before


def test_terminal_close_removes_client_but_keeps_session_and_program(backend):
    app = backend.attach()
    try:
        app.until(lambda state: b"READY" in state["screen"])
        before = query(backend)
        assert len(before["clients"]) == 1
        assert before["clients"][0]["attachment"] == "ATTACHED"
        pane_pid = backend.field("pane_pid")
    finally:
        app.close()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        after = query(backend)
        if not after["clients"]:
            break
        time.sleep(0.05)
    assert after["clients"] == []
    assert after["sessions"][0]["attached_clients"] == 0
    assert backend.field("pane_pid") == pane_pid
    assert any(
        line.lstrip().startswith("tmux ") and " test " in line and line.endswith(" 0")
        for line in query(backend, as_json=False).splitlines()
    )


def test_activity_uses_existing_tmux_timestamp_not_inventory_reads(backend):
    app = backend.attach()
    try:
        app.until(lambda state: b"READY" in state["screen"])
        before = query(backend)["clients"][0]
        assert before["last_activity_at"] >= before["created_at"] > 0
        time.sleep(1.05)
        idle = query(backend)["clients"][0]
        assert idle["last_activity_at"] == before["last_activity_at"]
        assert idle["activity_idle_seconds"] >= 1
        assert idle["idle_seconds"] is None
        assert idle["network_last_seen_at"] is None
        app.send(b"inventory-input")
        app.until(lambda state: backend.received().endswith(b"inventory-input"))
        after = query(backend)["clients"][0]
        assert after["last_activity_at"] > before["last_activity_at"]
    finally:
        app.close()


@pytest.mark.parametrize(
    "name,expected",
    [
        ("xfce4-terminal", "LOCAL"),
        ("konsole", "LOCAL"),
        ("sshd", "SSH"),
        ("sshd-session", "SSH"),
        ("sshd-auth", "SSH"),
        ("mosh-server", "MOSH"),
        ("mosh-native-server", "MOSH"),
    ],
)
def test_transport_reports_the_actual_ancestor(name, expected):
    processes = {
        100: clients.Process(100, 101, 1, "tmux", os.getuid()),
        101: clients.Process(101, 102, 2, "fish", os.getuid()),
        102: clients.Process(102, 1, 3, name, os.getuid()),
    }
    kind, owner = clients.connection_origin(100, processes.__getitem__)
    assert kind == expected
    assert owner.pid == 102


def test_mosh_is_not_mislabelled_ssh_when_started_via_sshd():
    processes = {
        100: clients.Process(100, 101, 1, "tmux", os.getuid()),
        101: clients.Process(101, 102, 2, "mosh-server", os.getuid()),
        102: clients.Process(102, 1, 3, "sshd-session", 0),
    }
    assert clients.transport(100, processes.__getitem__) == ("MOSH", 101)


def test_orphan_is_unknown_not_automatically_local():
    orphan = clients.Process(100, 1, 1, "tmux", os.getuid())
    assert clients.transport(100, lambda pid: orphan) == ("UNKNOWN", None)


def test_unreachable_mosh_still_has_a_live_tmux_attachment():
    processes = {
        100: clients.Process(100, 101, 1, "tmux", os.getuid(), "S"),
        101: clients.Process(101, 1, 2, "mosh-server", os.getuid(), "S"),
    }
    row = clients.Client(100, "/dev/pts/8", "example", 80, 24, set())
    rows, _ = clients.describe(
        [row],
        processes.__getitem__,
        lambda: ({(101, row.tty): ("UNREACHABLE", "-")}, ""),
    )
    assert rows[0].state == "UNREACHABLE"
    assert rows[0].attachment == "ATTACHED"
    assert rows[0].frontend_pid == 101


@pytest.mark.parametrize(
    "state,attachment", [("Z", "EXITED"), ("X", "EXITED"), ("T", "SUSPENDED")]
)
def test_exited_or_stopped_process_is_not_claimed_as_normal_attachment(
    state, attachment
):
    info = clients.Process(100, 1, 1, "tmux", os.getuid(), state)
    row = clients.Client(100, "/dev/pts/8", "example", 80, 24, set())
    assert clients.describe([row], lambda pid: info)[0][0].attachment == attachment


def test_process_that_disappears_during_inventory_is_explicit():
    def missing(pid):
        raise FileNotFoundError(pid)

    row = clients.Client(100, "/dev/pts/8", "example", 80, 24, set())
    assert clients.describe([row], missing)[0][0].attachment == "EXITED"


def test_control_attachment_is_not_falsely_reported_as_detached(backend):
    detached(backend)
    child = subprocess.Popen(
        backend.cli("-C", "attach-session", "-t", "=quiet"),
        env=backend.env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = query(backend)
            if result["clients"]:
                break
            time.sleep(0.05)
        assert len(result["clients"]) == 1
        assert result["clients"][0]["sizing"] == "not-applicable"
        assert result["sessions"][0]["attached_clients"] == 1
        assert "detached" not in query(backend, as_json=False)
        server = clients.Server(backend.binary, backend.socket, backend.env)
        assert server.snapshot()[0] == []  # Sizing still excludes non-TTY clients.
    finally:
        child.stdin.close()
        child.wait(timeout=3)
        child.stdout.close()
        child.stderr.close()


def test_idle_format_and_unknown_timestamps():
    assert clients.timestamp("") is None
    assert clients.timestamp("0") is None
    assert clients.format_age(None) == "-"
    assert clients.format_age(61) == "1m01s"
    assert clients.format_age(3700) == "1h01m"
    assert clients.format_age(86400) == "1d00h"
    assert clients.activity_age(200, 100) == 0


XFCE_CLOSE_DRIVER = r"""
import json, os, shlex, subprocess, sys, time
from pathlib import Path

binary, socket, terminal, helper = sys.argv[1:]
prefix = [binary, "-N", "-S", socket]

def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=4)

def inventory():
    return json.loads(run(helper, "-S", socket, "--tmux", binary, "clients", "--json").stdout)

def until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = inventory()
        if predicate(result):
            return result
        time.sleep(0.05)
    raise AssertionError(result)

window = None
try:
    run("xfconf-query", "-c", "xfce4-terminal", "-p", "/misc-confirm-close",
        "--create", "--type", "bool", "--set", "false")
    run(binary, "-S", socket, "new-session", "-d", "-s", "quiet", "--", "sleep", "60")
    pane_pid = run(*prefix, "display-message", "-p", "#{pane_pid}").stdout
    with Path(socket).with_suffix(".xfce-log").open("w") as log:
        window = subprocess.Popen(
            [terminal, "--disable-server", "--title=INVENTORY-CLOSE-FIXTURE",
             "--command", shlex.join(prefix + ["attach-session", "-t", "=quiet"])],
            stdout=log, stderr=log)
    before = until(lambda result: len(result["clients"]) == 1)
    assert before["clients"][0]["transport"] == "LOCAL", before
    assert before["clients"][0]["attachment"] == "ATTACHED", before
    assert before["clients"][0]["frontend"] == "xfce4-terminal", before
    assert before["clients"][0]["frontend_pid"] == window.pid, before
    stamp = Path(f"/proc/{window.pid}/exe").stat().st_mtime
    assert before["entries"][0]["frontend_binary_mtime"] == stamp, before
    assert before["entries"][0]["via"] == "xfce4-terminal", before
    xid = run("xdotool", "search", "--onlyvisible", "--sync", "--name",
              "INVENTORY-CLOSE-FIXTURE").stdout.splitlines()[0]
    run("xdotool", "windowfocus", "--sync", xid)
    run("xdotool", "key", "--clearmodifiers", "ctrl+shift+q")
    window.wait(timeout=5)
    after = until(lambda result: not result["clients"])
    assert after["sessions"][0]["attached_clients"] == 0, after
    assert run(*prefix, "display-message", "-p", "#{pane_pid}").stdout == pane_pid
    text = run(helper, "-S", socket, "--tmux", binary, "clients").stdout
    assert any(line.lstrip().startswith("tmux ") and " quiet " in line and line.endswith(" 0") for line in text.splitlines()), text
    print("real-xfce-close-keeps-session-ok")
finally:
    subprocess.run(prefix + ["kill-server"], capture_output=True, timeout=3, check=False)
    if window is not None:
        try:
            window.wait(timeout=3)
        except subprocess.TimeoutExpired:
            window.terminate()
            window.wait(timeout=3)
"""


def test_real_xfce_window_close_reports_detached_without_stopping_session(backend):
    root = Path(__file__).resolve().parents[1]
    terminal = Path(
        os.environ.get(
            "TEST_XFCE_TERMINAL",
            str(root.parents[1] / "repos/xfce4-terminal/build/terminal/xfce4-terminal"),
        )
    )
    assert terminal.is_file(), "Build the XFCE terminal first"
    result = subprocess.run(
        [
            "xvfb-run",
            "-a",
            "dbus-run-session",
            "--",
            sys.executable,
            "-c",
            XFCE_CLOSE_DRIVER,
            backend.binary,
            str(backend.socket),
            str(terminal),
            str(backend.mosh_helper),
        ],
        env=backend.env
        | {
            "GDK_BACKEND": "x11",
            "GTK_THEME": "Adwaita",
            "NO_AT_BRIDGE": "1",
            "GIO_USE_VFS": "local",
            "GTK_USE_PORTAL": "0",
        },
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "real-xfce-close-keeps-session-ok" in result.stdout
