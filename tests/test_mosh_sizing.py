import json
import os
import signal
import struct
import subprocess
import time
from dataclasses import replace

import pytest

import tmux_clients as clients


def cli(backend, *args):
    return subprocess.run(
        backend.cli("clients", *args), env=backend.env, capture_output=True, timeout=6
    )


def test_clients_command_does_not_start_a_server(backend):
    result = cli(backend, "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["clients"] == []
    assert not backend.socket.exists()
    assert cli(backend, "--manage-sizing").returncode == 1
    assert not backend.socket.exists()


def test_login_records_classify_only_explicit_mosh_status():
    output = """lewis pts/1 2026-09-27 09:01 . 90 (100.1.2.3 via mosh [401])
lewis pts/2 2026-09-27 09:02 old 91 (mosh [402])
lewis pts/3 2026-09-27 09:03 . 92 (fe80::123%eth0 via mosh [403])
lewis pts/4 2026-09-27 09:04 . 93 (100.1.2.4)
badly truncated (mosh [)
"""
    records = clients.parse_logins(output)
    assert records == {
        (401, "/dev/pts/1"): ("CONNECTED", "100.1.2.3"),
        (402, "/dev/pts/2"): ("UNREACHABLE", "-"),
        (403, "/dev/pts/3"): ("CONNECTED", "fe80::123%eth0"),
    }
    duplicate = output + "lewis pts/2 2026-09-27 09:05 . 91 (10.0.0.1 via mosh [402])\n"
    assert clients.parse_logins(duplicate)[402, "/dev/pts/2"] == ("UNKNOWN", "-")


@pytest.fixture
def terminals(backend):
    desktop = backend.attach(cols=100, rows=30)
    phone = None
    try:
        desktop.until(lambda state: b"READY" in state["screen"])
        phone = backend.attach(existing=True, cols=40, rows=16)
        phone.until(lambda state: b"READY" in state["screen"])
        server = clients.Server(backend.binary, backend.socket, backend.env)
        rows, _ = server.snapshot()
        small = next(row for row in rows if row.width == 40)
        yield desktop, phone, server, small
    finally:
        if phone:
            phone.close()
        desktop.close()


def fake_mosh(small):
    def lookup(pid):
        if pid == 99999999:
            return clients.Process(pid, 1, 1, "mosh-server", os.getuid())
        info = clients.process(pid)
        return replace(info, parent=99999999 if pid == small.pid else 1)

    return lookup


@pytest.mark.parametrize("policy", ["latest", "smallest"])
def test_offline_phone_releases_size_and_reconnect_restores_it(
    terminals, backend, policy
):
    desktop, phone, server, small = terminals
    backend.run("set-option", "-gw", "window-size", policy)
    phone.send(b"phone-active")
    phone.until(lambda state: backend.received().endswith(b"phone-active"))
    pane_pid = backend.field("pane_pid")
    status = {(99999999, small.tty): ("CONNECTED", "100.1.2.3")}
    monitor = clients.SizeMonitor(server, fake_mosh(small), lambda: (status, ""))
    monitor.step()
    assert backend.field("pane_width") == b"40"
    status[99999999, small.tty] = ("UNREACHABLE", "-")
    monitor.step()
    desktop.until(lambda state: backend.field("pane_width") == b"100")
    assert backend.field("pane_height") == b"30"
    assert backend.field("window-size") == policy.encode()
    assert len(server.snapshot()[0]) == 2
    assert backend.field("pane_pid") == pane_pid
    assert not desktop.exited and not phone.exited
    status[99999999, small.tty] = ("CONNECTED", "100.1.2.3")
    monitor.step()
    phone.until(lambda state: backend.field("pane_width") == b"40")
    assert backend.field("pane_height") == b"16"
    assert backend.field("pane_pid") == pane_pid
    assert server.snapshot()[1] == set()


def test_manual_ignore_and_unknown_records_are_not_treated_as_offline(
    terminals, backend
):
    _, _, server, small = terminals
    monitor = clients.SizeMonitor(
        server, fake_mosh(small), lambda: ({}, "missing utmp")
    )
    rows, note = monitor.step()
    assert note == "missing utmp"
    assert next(row for row in rows if row.pid == small.pid).state == "UNKNOWN"
    assert backend.field("pane_width") == b"40"
    backend.run("refresh-client", "-t", small.tty, "-f", "ignore-size,read-only")
    monitor.read_logins = lambda: ({(99999999, small.tty): ("UNREACHABLE", "-")}, "")
    monitor.step()
    assert not server.snapshot()[1]
    monitor.read_logins = lambda: ({(99999999, small.tty): ("CONNECTED", "host")}, "")
    monitor.step()
    flags = next(row.flags for row in server.snapshot()[0] if row.pid == small.pid)
    assert {"ignore-size", "read-only"} <= flags


def test_monitor_restart_and_unknown_state_restore_only_owned_flags(terminals, backend):
    _, _, server, small = terminals
    monitor = clients.SizeMonitor(
        server,
        fake_mosh(small),
        lambda: ({(99999999, small.tty): ("UNREACHABLE", "-")}, ""),
    )
    monitor.step()
    assert backend.field("pane_width") == b"100"
    replacement = clients.SizeMonitor(
        server, fake_mosh(small), lambda: ({}, "missing utmp")
    )
    replacement.step()
    assert backend.field("pane_width") == b"40"
    assert server.snapshot()[1] == set()


def test_process_lookup_failure_does_not_forget_owned_flags(terminals):
    _, _, server, small = terminals
    monitor = clients.SizeMonitor(
        server,
        fake_mosh(small),
        lambda: ({(99999999, small.tty): ("UNREACHABLE", "-")}, ""),
    )
    monitor.step()
    owned = server.snapshot()[1]

    def missing(pid):
        if pid == small.pid:
            raise PermissionError("transient /proc access failure")
        return clients.process(pid)

    monitor.lookup = missing
    monitor.step()
    assert server.snapshot()[1] == owned
    monitor.lookup = fake_mosh(small)
    monitor.step(restore=True)
    assert not server.snapshot()[1]


@pytest.mark.parametrize("action", [("clients", "--json"), ("clients",), ("list",)])
def test_inventory_enables_monitor_by_default_and_is_singleton(
    terminals, backend, action
):
    desktop, _, server, _ = terminals
    assert not backend.socket.with_suffix(".sizing.lock").exists()
    pane_pid = backend.field("pane_pid")
    try:
        enabled = subprocess.run(
            backend.cli(*action), env=backend.env, capture_output=True, timeout=6
        )
        assert enabled.returncode == 0, enabled.stderr
        server.snapshot()
        assert clients.watcher_alive(server.watcher)
        desktop.until(
            lambda state: (
                (server.snapshot() is not None)
                and clients.watcher_alive(server.watcher)
            )
        )
        identity = server.watcher
        assert not clients.start_monitor(server)
        for options in [("--json",), ("--manage-sizing", "--json")]:
            result = cli(backend, *options)
            assert result.returncode == 0, result.stderr
            status = json.loads(result.stdout)
            assert status["monitor_running"]
            assert len(status["clients"]) == 2
        server.snapshot()
        assert server.watcher == identity
        assert backend.field("pane_pid") == pane_pid
        assert backend.field("pane_width") == b"40"
    finally:
        server.snapshot()
        if clients.watcher_alive(server.watcher):
            os.kill(int(server.watcher.split(":")[0]), signal.SIGTERM)
            deadline = time.monotonic() + 5
            while clients.watcher_alive(server.watcher) and time.monotonic() < deadline:
                time.sleep(0.05)
                server.snapshot()


def test_inventory_survives_monitor_start_failure(terminals, backend):
    _, _, server, _ = terminals
    target = backend.socket.parent / "do-not-touch"
    target.write_text("keep")
    backend.socket.with_suffix(".sizing.lock").symlink_to(target)
    result = cli(backend, "--json")
    assert result.returncode == 0, result.stderr
    assert b"Warning: Mosh sizing monitor unavailable:" in result.stderr
    status = json.loads(result.stdout)
    assert len(status["clients"]) == 2
    assert not status["monitor_running"]
    assert not server.snapshot()[1]
    assert target.read_text() == "keep"


def test_monitor_does_not_follow_a_replacement_server(backend):
    first = backend.attach()
    first.until(lambda state: b"READY" in state["screen"])
    server = clients.Server(backend.binary, backend.socket, backend.env)
    server.snapshot()
    backend.run("kill-server")
    first.close()
    second = backend.attach()
    try:
        second.until(lambda state: b"READY" in state["screen"])
        with pytest.raises(clients.MonitorError, match="replaced"):
            server.snapshot()
    finally:
        second.close()


def test_monitor_refuses_unsafe_lock_paths(backend):
    target = backend.socket.parent / "do-not-touch"
    target.write_text("keep")
    link = backend.socket.with_suffix(".sizing.lock")
    link.symlink_to(target)
    with pytest.raises(OSError):
        clients.private_file(link, os.O_RDWR)
    assert target.read_text() == "keep"


def test_coreutils_who_output_from_isolated_login_records(tmp_path):
    # Linux/glibc utmp layout; feed only this fixture to real who, never /run/utmp.
    record = struct.Struct("@hi32s4s32s256shhiii4i20s")
    assert record.size == 384
    data = bytearray()
    for tty, host in [
        (b"pts/8", b"100.1.2.3 via mosh [456]"),
        (b"pts/9", b"mosh [789]"),
    ]:
        data += record.pack(
            7,
            os.getpid(),
            tty,
            b"ts",
            b"lewis",
            host,
            0,
            0,
            0,
            int(time.time()),
            0,
            0,
            0,
            0,
            0,
            b"",
        )
    fixture = tmp_path / "utmp"
    fixture.write_bytes(data)
    result = subprocess.run(
        ["/usr/bin/who", "-u", str(fixture)],
        env={"LC_ALL": "C"},
        capture_output=True,
        text=True,
        check=True,
        timeout=3,
    )
    assert clients.parse_logins(result.stdout) == {
        (456, "/dev/pts/8"): ("CONNECTED", "100.1.2.3"),
        (789, "/dev/pts/9"): ("UNREACHABLE", "-"),
    }


def test_flag_update_checks_exact_client_identity_inside_server(terminals):
    _, _, server, small = terminals
    rows, _ = clients.describe(server.snapshot()[0])
    actual = next(row for row in rows if row.pid == small.pid)
    other = next(row for row in rows if row.pid != small.pid)
    stale = replace(actual, tty=other.tty)
    stale.identity = f"{actual.pid}:{clients.process(actual.pid).started}:{other.tty}"
    server.flag(stale, True)
    assert all("ignore-size" not in row.flags for row in server.snapshot()[0])


def test_process_identity_reuse_refuses_flag_update(terminals):
    _, _, server, small = terminals
    rows, _ = clients.describe(server.snapshot()[0])
    actual = next(row for row in rows if row.pid == small.pid)
    actual.identity = f"{actual.pid}:0:{actual.tty}"
    with pytest.raises(clients.MonitorError, match="process changed"):
        server.flag(actual, True)
    assert all("ignore-size" not in row.flags for row in server.snapshot()[0])


def test_read_failure_is_unknown_not_unreachable(monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("no who executable")

    monkeypatch.setattr(clients.subprocess, "run", unavailable)
    records, note = clients.login_records()
    assert records == {}
    assert "unavailable" in note


def test_auto_start_only_when_mosh_is_present(terminals, monkeypatch):
    _, _, server, small = terminals
    started = []
    monkeypatch.setattr(clients, "start_monitor", lambda target: started.append(target))
    monkeypatch.setattr(clients, "transport", lambda pid: ("LOCAL", None))
    clients.maybe_start_monitor(server)
    assert started == []
    monkeypatch.setattr(
        clients,
        "transport",
        lambda pid: ("MOSH", 999) if pid == small.pid else ("LOCAL", None),
    )
    clients.maybe_start_monitor(server)
    assert started == [server]


def test_claim_survives_failure_between_flag_and_resize(
    terminals, backend, monkeypatch
):
    _, _, server, small = terminals
    monitor = clients.SizeMonitor(
        server,
        fake_mosh(small),
        lambda: ({(99999999, small.tty): ("UNREACHABLE", "-")}, ""),
    )
    save = server.save_owned
    calls = 0

    def interrupted(owned):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise clients.MonitorError("simulated crash after setting flag")
        save(owned)

    monkeypatch.setattr(server, "save_owned", interrupted)
    with pytest.raises(clients.MonitorError, match="simulated crash"):
        monitor.step()
    monkeypatch.setattr(server, "save_owned", save)
    assert server.snapshot()[1]
    replacement = clients.SizeMonitor(
        server,
        fake_mosh(small),
        lambda: ({(99999999, small.tty): ("CONNECTED", "peer")}, ""),
    )
    replacement.step()
    assert not server.snapshot()[1]
    assert backend.field("pane_width") == b"40"


def test_detached_client_ownership_is_pruned(terminals, backend):
    _, phone, server, small = terminals
    monitor = clients.SizeMonitor(
        server,
        fake_mosh(small),
        lambda: ({(99999999, small.tty): ("UNREACHABLE", "-")}, ""),
    )
    monitor.step()
    assert server.snapshot()[1]
    backend.run("detach-client", "-t", small.tty)
    phone.until(lambda state: phone.exited)
    monitor.step()
    assert not server.snapshot()[1]


def test_client_exit_race_does_not_stop_the_monitor(terminals, monkeypatch):
    _, _, server, small = terminals
    offline = {(99999999, small.tty): ("UNREACHABLE", "-")}
    monitor = clients.SizeMonitor(server, fake_mosh(small), lambda: (offline, ""))
    real_flag = server.flag

    def unavailable(*args):
        raise clients.ClientChanged("client disappeared between snapshot and update")

    monkeypatch.setattr(server, "flag", unavailable)
    monitor.step()
    monkeypatch.setattr(server, "flag", real_flag)
    monitor.step()
    assert "ignore-size" in next(
        row.flags for row in server.snapshot()[0] if row.pid == small.pid
    )
    monitor.step(restore=True)
    assert not server.snapshot()[1]


def test_idle_polling_does_not_write_options_or_force_redraw(terminals, monkeypatch):
    _, _, server, small = terminals
    online = {(99999999, small.tty): ("CONNECTED", "peer")}
    monitor = clients.SizeMonitor(server, fake_mosh(small), lambda: (online, ""))
    monitor.step()

    def unexpected(*args):
        raise AssertionError("unchanged status must not mutate tmux")

    monkeypatch.setattr(server, "save_owned", unexpected)
    monkeypatch.setattr(server, "flag", unexpected)
    for _ in range(3):
        monitor.step()


def test_monitor_process_exits_after_its_private_server_stops(terminals, backend):
    _, _, server, _ = terminals
    clients.start_monitor(server)
    server.snapshot()
    identity = server.watcher
    assert clients.watcher_alive(identity)
    backend.run("kill-server")
    deadline = time.monotonic() + 7
    while clients.watcher_alive(identity) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not clients.watcher_alive(identity)
