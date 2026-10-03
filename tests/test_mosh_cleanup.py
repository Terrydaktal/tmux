"""Stale cleanup must never signal a fresh, foreign, recycled or unverified PID."""

import errno
import json
import os
import select
import signal
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from loopback_harness import Session
from test_mosh_status import SERVER, owned_pid

import mosh_cleanup
import tmux_clients as clients


@pytest.fixture
def runtime(monkeypatch):
    owner = clients.Process(100, 1, 123, "mosh-server", os.getuid(), "S")
    data = SimpleNamespace(
        processes={100: owner}, ages={100: [30, 30]}, opened=[], closed=[], signals=[]
    )

    def age(process, _lookup):
        values = data.ages.get(process.pid, [None])
        value = values.pop(0) if len(values) > 1 else values[0]
        return None if value is None else {"idle_seconds": value}

    def open_fd(pid):
        data.opened.append(pid)
        return pid + 1000

    def send_signal(fd):
        data.signals.append((fd, signal.SIGTERM))

    monkeypatch.setattr(mosh_cleanup.mosh_status, "read", age)
    monkeypatch.setattr(
        mosh_cleanup,
        "os",
        SimpleNamespace(getuid=os.getuid, sysconf=os.sysconf, close=data.closed.append),
    )
    monkeypatch.setattr(mosh_cleanup, "pidfd_open", open_fd)
    monkeypatch.setattr(mosh_cleanup, "send_termination", send_signal)
    data.lookup = data.processes.__getitem__
    data.run = lambda **kwargs: mosh_cleanup.cleanup(
        data.lookup, snapshot=(data.processes, ""), **kwargs
    )
    return data


@pytest.fixture
def legacy(runtime, monkeypatch):
    runtime.processes[101] = clients.Process(
        101, 100, 124, "fish", os.getuid(), "S", 101, 123, 101
    )
    runtime.ages[100] = [None]
    runtime.records = [({(100, "/dev/pts/8"): ("UNREACHABLE", "-")}, "")]
    runtime.login_reads = 0

    def logins():
        runtime.login_reads += 1
        return (
            runtime.records.pop(0) if len(runtime.records) > 1 else runtime.records[0]
        )

    monkeypatch.setattr(mosh_cleanup.time, "clock_gettime", lambda _clock: 100)
    monkeypatch.setattr(
        mosh_cleanup.mosh_sessions,
        "tty_matches",
        lambda tty, device: tty == "/dev/pts/8" and device == 123,
    )
    run = runtime.run
    runtime.run = lambda **kwargs: run(read_logins=logins, **kwargs)
    return runtime


@pytest.mark.parametrize(
    "age,action", [(0, "kept"), (29, "kept"), (30, "closing"), (31, "closing")]
)
def test_default_30_second_boundary(runtime, age, action):
    runtime.ages[100] = [age]
    result = runtime.run()
    assert result["older_than_seconds"] == 30
    assert result["entries"][0]["action"] == action
    assert runtime.signals == ([(1100, signal.SIGTERM)] if action == "closing" else [])
    assert runtime.closed == ([1100] if action == "closing" else [])


def test_dry_run_never_opens_a_pidfd_or_signals(runtime):
    result = runtime.run(dry_run=True)
    assert result["entries"][0]["action"] == "would-close"
    assert not runtime.opened and not runtime.signals


@pytest.mark.parametrize("ages", [[None], [30, None]])
def test_missing_timestamp_always_skips(runtime, ages):
    runtime.ages[100] = ages
    assert runtime.run()["entries"][0]["action"] == "skipped"
    assert not runtime.signals
    assert len(runtime.closed) == len(runtime.opened)


def test_legacy_cleanup_requires_explicit_opt_in(legacy):
    (row,) = legacy.run()["entries"]
    assert row["action"] == "skipped" and "--include-legacy" in row["detail"]
    assert row["source"] == "unavailable"
    assert not legacy.login_reads and not legacy.signals


@pytest.mark.parametrize("dry_run", [False, True])
def test_legacy_unreachable_uses_rechecked_login_not_invented_age(legacy, dry_run):
    result = legacy.run(include_legacy=True, dry_run=dry_run)
    (row,) = result["entries"]
    assert row["action"] == ("would-close" if dry_run else "closing")
    assert row["source"] == "legacy-login" and row["idle_seconds"] is None
    assert row["reachability"] == "UNREACHABLE"
    assert "coarse" in result["warning"] and result["include_legacy"]
    assert legacy.login_reads == (1 if dry_run else 2)
    assert legacy.signals == ([] if dry_run else [(1100, signal.SIGTERM)])
    assert legacy.closed == ([] if dry_run else [1100])


@pytest.mark.parametrize(
    "records,action",
    [
        ({(100, "/dev/pts/8"): ("CONNECTED", "peer")}, "kept"),
        ({(100, "/dev/pts/8"): ("UNKNOWN", "-")}, "skipped"),
        ({(100, "/dev/pts/9"): ("UNREACHABLE", "-")}, "skipped"),
        ({(999, "/dev/pts/8"): ("UNREACHABLE", "-")}, "skipped"),
        ({}, "skipped"),
        (
            {
                (100, "/dev/pts/8"): ("UNREACHABLE", "-"),
                (100, "/dev/pts/9"): ("UNREACHABLE", "-"),
            },
            "skipped",
        ),
    ],
)
def test_unverified_or_connected_legacy_records_never_signal(legacy, records, action):
    legacy.records = [(records, "")]
    assert legacy.run(include_legacy=True)["entries"][0]["action"] == action
    assert not legacy.opened and not legacy.signals


def test_legacy_record_read_failure_is_not_treated_as_disconnected(legacy):
    legacy.records = [(legacy.records[0][0], "who failed")]
    (row,) = legacy.run(include_legacy=True)["entries"]
    assert row["action"] == "skipped" and row["detail"] == "who failed"
    assert not legacy.opened and not legacy.signals


@pytest.mark.parametrize(
    "age,action", [(0, "skipped"), (29.9, "skipped"), (30, "closing")]
)
def test_never_connected_legacy_servers_get_30_seconds_to_start(
    legacy, monkeypatch, age, action
):
    started = legacy.processes[100].started / os.sysconf("SC_CLK_TCK")
    monkeypatch.setattr(
        mosh_cleanup.time, "clock_gettime", lambda _clock: started + age
    )
    assert legacy.run(include_legacy=True)["entries"][0]["action"] == action
    if action == "skipped":
        assert not legacy.login_reads and not legacy.opened


@pytest.mark.parametrize(
    "change",
    [
        {"started": 125},
        {"parent": 1},
        {"tty_device": 456},
        {"uid": os.getuid() + 1},
        {"state": "Z"},
    ],
)
def test_changed_legacy_terminal_never_signals(legacy, change):
    original = legacy.lookup
    legacy.lookup = lambda pid: (
        replace(original(pid), **change) if pid == 101 else original(pid)
    )
    assert legacy.run(include_legacy=True)["entries"][0]["action"] == "skipped"
    assert not legacy.opened and not legacy.signals


@pytest.mark.parametrize(
    "state,action", [("CONNECTED", "kept"), ("UNKNOWN", "skipped")]
)
def test_final_legacy_reachability_recheck_can_cancel_cleanup(legacy, state, action):
    legacy.records += [({(100, "/dev/pts/8"): (state, "peer")}, "")]
    assert legacy.run(include_legacy=True)["entries"][0]["action"] == action
    assert legacy.login_reads == 2 and legacy.closed == [1100] and not legacy.signals


def test_changed_terminal_on_final_legacy_query_cancels_cleanup(legacy):
    original = legacy.lookup
    reads = 0

    def lookup(pid):
        nonlocal reads
        process = original(pid)
        if pid == 101:
            reads += 1
            if reads == 2:
                return replace(process, started=process.started + 1)
        return process

    legacy.lookup = lookup
    assert legacy.run(include_legacy=True)["entries"][0]["action"] == "skipped"
    assert legacy.login_reads == 1 and legacy.closed == [1100] and not legacy.signals


def test_packet_timestamp_takes_precedence_over_legacy_flag(legacy):
    legacy.ages[100] = [0]
    (row,) = legacy.run(include_legacy=True)["entries"]
    assert row["action"] == "kept" and row["source"] == "packet-timestamp"
    assert not legacy.login_reads and not legacy.signals


def test_fresh_timestamp_on_final_query_overrides_legacy_unreachable(legacy):
    legacy.ages[100] = [None, 0]
    (row,) = legacy.run(include_legacy=True)["entries"]
    assert row["action"] == "kept" and row["source"] == "packet-timestamp"
    assert row["idle_seconds"] == 0 and row["reachability"] is None
    assert legacy.login_reads == 1 and legacy.closed == [1100] and not legacy.signals


def test_failed_final_timestamp_query_never_downgrades_to_legacy(legacy):
    legacy.ages[100] = [30, None]
    assert legacy.run(include_legacy=True)["entries"][0]["action"] == "skipped"
    assert not legacy.login_reads and not legacy.signals


@pytest.mark.parametrize("cutoff", [1, 29, 31, 60])
def test_legacy_mode_cannot_claim_arbitrary_packet_age(legacy, cutoff):
    with pytest.raises(ValueError, match="30-second"):
        legacy.run(include_legacy=True, older_than=cutoff)
    assert not legacy.login_reads and not legacy.opened


def test_reconnect_between_scan_and_final_query_cancels_cleanup(runtime):
    runtime.ages[100] = [60, 0]
    (row,) = runtime.run()["entries"]
    assert row["action"] == "kept" and row["idle_seconds"] == 0
    assert row["detail"] == "client resumed before cleanup"
    assert not runtime.signals and runtime.closed == [1100]


@pytest.mark.parametrize(
    "change",
    [
        {"started": 124},
        {"uid": os.getuid() + 1},
        {"executable": "fish"},
        {"state": "Z"},
    ],
)
@pytest.mark.parametrize("at_final_lookup", [False, True])
def test_identity_changes_never_signal(runtime, change, at_final_lookup):
    owner = runtime.processes[100]
    values = (
        [owner, replace(owner, **change)]
        if at_final_lookup
        else [replace(owner, **change)]
    )
    runtime.lookup = lambda pid: values.pop(0) if len(values) > 1 else values[0]
    assert runtime.run()["entries"][0]["action"] == "skipped"
    assert not runtime.signals and runtime.closed == [1100]


def test_foreign_dead_and_non_mosh_processes_are_not_candidates(runtime):
    owner = runtime.processes[100]
    for pid, fields in (
        (101, {"uid": os.getuid() + 1}),
        (102, {"state": "Z"}),
        (103, {"state": "X"}),
        (104, {"executable": "tmux"}),
        (105, {"executable": "sshd"}),
    ):
        runtime.processes[pid] = replace(owner, pid=pid, **fields)
    result = runtime.run()
    assert [row["mosh_pid"] for row in result["entries"]] == [100]
    assert runtime.signals == [(1100, signal.SIGTERM)]


@pytest.mark.parametrize("exc", [ProcessLookupError(), FileNotFoundError()])
def test_exit_race_is_normal_and_fd_is_closed(runtime, exc):
    def lookup(_pid):
        raise exc

    runtime.lookup = lookup
    assert runtime.run()["entries"][0]["action"] == "skipped"
    assert runtime.closed == [1100] and not runtime.signals


def test_permission_failure_is_reported_without_force_kill(runtime, monkeypatch):
    def denied(*_args):
        raise PermissionError("denied")

    monkeypatch.setattr(mosh_cleanup, "send_termination", denied)
    result = runtime.run()
    assert result["summary"]["error"] == 1
    assert runtime.closed == [1100] and not runtime.signals


def test_missing_pidfd_support_fails_closed(runtime, monkeypatch):
    def unavailable(_pid):
        raise OSError(errno.ENOSYS, "unavailable")

    monkeypatch.setattr(mosh_cleanup, "pidfd_open", unavailable)
    assert runtime.run()["entries"][0]["action"] == "error"
    assert not runtime.signals


def test_libc_wrappers_use_pidfd_flags_and_only_sigterm(monkeypatch):
    calls = []

    def open_fd(*args):
        calls.append(args)
        return 12

    def send_signal(*args):
        calls.append(args)
        return 0

    monkeypatch.setattr(mosh_cleanup, "pidfd_api", lambda: (open_fd, send_signal))
    assert mosh_cleanup.pidfd_open(100) == 12
    mosh_cleanup.send_termination(12)
    assert calls == [(100, 0), (12, signal.SIGTERM, None, 0)]


def test_libc_error_is_reported_not_interpreted_as_a_descriptor(monkeypatch):
    monkeypatch.setattr(mosh_cleanup.ctypes, "get_errno", lambda: errno.ESRCH)
    with pytest.raises(ProcessLookupError):
        mosh_cleanup.checked_call(lambda *_args: -1, 100, 0)


@pytest.mark.parametrize("cutoff", [0, -1, True, 0.5])
def test_invalid_cutoffs_are_rejected(runtime, cutoff):
    with pytest.raises(ValueError, match="positive"):
        runtime.run(older_than=cutoff)
    assert not runtime.opened


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("include_legacy", [False, True])
def test_cli_cleanup_does_not_require_or_touch_tmux(
    runtime, monkeypatch, capsys, dry_run, include_legacy
):
    result = runtime.run(dry_run=True)
    calls = []

    def cleanup(lookup, **kwargs):
        calls.append(kwargs)
        assert lookup is clients.process
        return result

    def forbidden(*_args):
        raise AssertionError("cleanup must not query/start a tmux server")

    monkeypatch.setattr(mosh_cleanup, "cleanup", cleanup)
    monkeypatch.setattr(clients, "Server", forbidden)
    monkeypatch.setattr(
        "sys.argv",
        ["tmux-mosh", "-S", "/missing/server.sock", "cleanup", "--json"]
        + (["--dry-run"] if dry_run else [])
        + (["--include-legacy"] if include_legacy else []),
    )
    assert clients.public_main() == 0
    assert calls == [
        {
            "older_than": 30,
            "dry_run": dry_run,
            "include_legacy": include_legacy,
            "read_logins": clients.login_records,
        }
    ]
    assert json.loads(capsys.readouterr().out) == result


@pytest.mark.parametrize("cutoff", ["0", "-2", "1.5", "bad"])
def test_cli_rejects_bad_threshold_without_discovering_sessions(monkeypatch, cutoff):
    monkeypatch.setattr("sys.argv", ["tmux-mosh", "cleanup", "--older-than", cutoff])
    monkeypatch.setattr(
        mosh_cleanup, "cleanup", lambda *_a, **_k: pytest.fail("must not run")
    )
    with pytest.raises(SystemExit) as exc:
        clients.public_main()
    assert exc.value.code == 2


def test_cli_rejects_legacy_with_other_cutoffs_before_discovery(monkeypatch):
    monkeypatch.setattr(
        "sys.argv", ["tmux-mosh", "cleanup", "--include-legacy", "--older-than", "60"]
    )
    monkeypatch.setattr(
        mosh_cleanup, "cleanup", lambda *_a, **_k: pytest.fail("must not run")
    )
    with pytest.raises(SystemExit) as exc:
        clients.public_main()
    assert exc.value.code == 2


def test_legacy_table_names_the_source_without_a_fake_age(legacy, capsys):
    clients.show_cleanup(legacy.run(include_legacy=True, dry_run=True))
    output = capsys.readouterr().out
    assert "SOURCE" in output and "legacy-login" in output and "UNREACHABLE" in output
    assert "30s+" not in output


@pytest.mark.parametrize("through_tmux", [False, True], ids=["direct", "tmux"])
def test_real_cleanup_only_signals_owned_loopback_server(
    backend, tmp_path, monkeypatch, through_tmux
):
    with tempfile.TemporaryDirectory(prefix="mosh-cleanup-") as directory:
        monkeypatch.setenv("XDG_RUNTIME_DIR", directory)
        backend.env["XDG_RUNTIME_DIR"] = directory
        program = backend.program()
        if through_tmux:
            program = backend.cli("new-session", "-s", "test", "--", *program)
        session = Session(tmp_path, server=str(SERVER), program=program)
        try:
            session.attachment.until(
                lambda state: b"READY" in state["screen"], timeout=8
            )
            owner = clients.process(owned_pid(session))
            snapshot = ({owner.pid: owner}, "")
            result = mosh_cleanup.cleanup(clients.process, snapshot=snapshot)
            assert result["summary"]["kept"] == 1
            pane = backend.field("pane_pid") if through_tmux else None
            session.link.paused = True
            session.attachment.pump(1.25)
            preview = mosh_cleanup.cleanup(
                clients.process, snapshot=snapshot, older_than=1, dry_run=True
            )
            assert preview["summary"]["would-close"] == 1
            assert not select.select([session.pidfd], [], [], 0)[0]
            result = mosh_cleanup.cleanup(
                clients.process, snapshot=snapshot, older_than=1
            )
            assert result["summary"]["closing"] == 1, result
            assert select.select([session.pidfd], [], [], 15)[0]
            if through_tmux:
                assert backend.run("has-session", "-t", "=test").returncode == 0
                assert backend.field("pane_pid") == pane
        finally:
            session.close()


@pytest.mark.parametrize("through_tmux", [False, True], ids=["direct", "tmux"])
def test_legacy_cleanup_on_owned_server_without_packet_metadata(
    backend, tmp_path, monkeypatch, through_tmux
):
    with tempfile.TemporaryDirectory(prefix="mosh-legacy-cleanup-") as directory:
        monkeypatch.setenv("XDG_RUNTIME_DIR", directory)
        backend.env["XDG_RUNTIME_DIR"] = directory
        # Reproduce an older server's absent endpoint without installing stock Mosh.
        (Path(directory) / "mosh-status").write_text("blocked test status directory")
        program = backend.program()
        if through_tmux:
            program = backend.cli("new-session", "-s", "test", "--", *program)
        session = Session(tmp_path, program=program)
        try:
            session.attachment.until(
                lambda state: b"READY" in state["screen"], timeout=8
            )
            owner = clients.process(owned_pid(session))
            processes, note = mosh_cleanup.mosh_sessions.read_processes(clients.process)
            anchor = next(
                info
                for info in processes.values()
                if info.parent == owner.pid and info.tty_device
            )
            snapshot = ({owner.pid: owner, anchor.pid: anchor}, note)
            tty = mosh_cleanup.mosh_sessions.process_tty(anchor)
            assert tty and mosh_cleanup.mosh_status.read(owner, clients.process) is None
            state = "CONNECTED"

            def read_logins():
                return {(owner.pid, tty): (state, "-")}, ""

            # Model the old server's coarse record without editing system utmp or waiting 30s.
            started = owner.started / os.sysconf("SC_CLK_TCK")
            monkeypatch.setattr(
                mosh_cleanup.time, "clock_gettime", lambda _clock: started + 31
            )
            args = {
                "snapshot": snapshot,
                "read_logins": read_logins,
                "include_legacy": True,
            }
            result = mosh_cleanup.cleanup(clients.process, **args)
            assert result["summary"]["kept"] == 1
            pane = backend.field("pane_pid") if through_tmux else None
            session.link.paused = True
            state = "UNREACHABLE"
            preview = mosh_cleanup.cleanup(clients.process, dry_run=True, **args)
            assert preview["summary"]["would-close"] == 1
            assert preview["entries"][0]["source"] == "legacy-login"
            assert not select.select([session.pidfd], [], [], 0)[0]
            result = mosh_cleanup.cleanup(clients.process, **args)
            assert result["summary"]["closing"] == 1, result
            assert select.select([session.pidfd], [], [], 15)[0]
            if through_tmux:
                assert backend.run("has-session", "-t", "=test").returncode == 0
                assert backend.field("pane_pid") == pane
        finally:
            session.close()
