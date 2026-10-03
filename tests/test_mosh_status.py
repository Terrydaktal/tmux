"""Packet age from the release server, outages/replay, and local API validation."""

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from loopback_harness import Session

import mosh_status
import tmux_clients as clients

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "build/runtime/mosh-server"


def owned_pid(session):
    return int(
        next(
            line.split()[1]
            for line in Path(f"/proc/self/fdinfo/{session.pidfd}")
            .read_text()
            .splitlines()
            if line.startswith("Pid:")
        )
    )


@pytest.mark.parametrize("through_tmux", [False, True], ids=["direct-app", "tmux-app"])
def test_release_server_reports_receive_age_during_outage_and_reconnect(
    backend, tmp_path, monkeypatch, through_tmux
):
    assert SERVER.is_file(), "run scripts/build-mosh.sh first"
    with tempfile.TemporaryDirectory(prefix="mosh-rx-") as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        backend.env["XDG_RUNTIME_DIR"] = runtime
        program = backend.program()
        if through_tmux:
            program = backend.cli("new-session", "-s", "test", "--", *program)
        session = Session(tmp_path, server=str(SERVER), program=program)
        try:
            session.attachment.until(
                lambda state: b"READY" in state["screen"], timeout=8
            )
            owner = clients.process(owned_pid(session))
            first = mosh_status.read(owner, clients.process)
            assert first is not None and first["idle_seconds"] <= 1
            assert first["network_last_seen_at"] <= time.time()

            session.link.paused = True
            session.attachment.pump(0.1)
            before = mosh_status.read(owner, clients.process)
            session.attachment.pump(1.15)
            paused = mosh_status.read(owner, clients.process)
            assert (
                paused["network_last_rx_monotonic_ms"]
                == before["network_last_rx_monotonic_ms"]
            )
            assert paused["idle_seconds"] >= 1

            # Neither a local status query, a replay nor unauthenticated UDP may
            # make a disconnected client's last-received time look fresh.
            session.link.upstream.sendto(
                session.link.last_client_packet, session.link.server
            )
            session.link.upstream.sendto(b"invalid packet", session.link.server)
            session.attachment.pump(0.1)
            assert (
                mosh_status.read(owner, clients.process)["network_last_rx_monotonic_ms"]
                == before["network_last_rx_monotonic_ms"]
            )

            snapshot = clients.inventory(clients.Server(backend.binary, backend.socket))
            (entry,) = [
                row for row in snapshot["entries"] if row["mosh_pid"] == owner.pid
            ]
            assert entry["idle_seconds"] >= 1
            assert entry["type"] == ("TMUX" if through_tmux else "DIRECT")
            result = subprocess.run(
                backend.mosh_cli("clients", "--json"),
                env=backend.env,
                capture_output=True,
                text=True,
                check=True,
                timeout=6,
            )
            assert any(
                row["mosh_pid"] == owner.pid and row["idle_seconds"] >= 1
                for row in json.loads(result.stdout)["entries"]
            )

            session.link.paused = False
            session.attachment.send(b"contact-resumed")
            session.attachment.until(
                lambda state: backend.received().endswith(b"contact-resumed"), timeout=8
            )
            resumed = mosh_status.read(owner, clients.process)
            assert (
                resumed["network_last_rx_monotonic_ms"]
                > paused["network_last_rx_monotonic_ms"]
            )
            assert resumed["idle_seconds"] <= 1
        finally:
            session.close()


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    directory = tmp_path / "status"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(mosh_status, "directories", lambda: [directory])
    owner = clients.process(os.getpid())
    return directory, owner


def query_response(endpoint, response, *, lookup=None):
    directory, owner = endpoint
    path = directory / f"{owner.pid}.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(1)
        listener.settimeout(1)

        def serve():
            with listener.accept()[0] as connection:
                connection.sendall(response)

        worker = threading.Thread(target=serve)
        worker.start()
        try:
            return mosh_status.read(owner, lookup or clients.process)
        finally:
            worker.join(timeout=2)
            assert not worker.is_alive()
            path.unlink()


def test_reader_accepts_real_socket_credentials_and_elapsed_monotonic_time(endpoint):
    _, owner = endpoint
    received = time.monotonic_ns() // 1_000_000 - 2300
    response = (
        json.dumps(
            {"version": 1, "pid": owner.pid, "last_rx_monotonic_ms": received}
        ).encode()
        + b"\n"
    )
    result = query_response(endpoint, response)
    assert result["idle_seconds"] == 2
    assert result["network_last_rx_monotonic_ms"] == received
    assert abs(time.time() - result["network_last_seen_at"] - 2.3) < 0.2


@pytest.mark.parametrize(
    "change",
    [
        {"last_rx_monotonic_ms": None},
        {"last_rx_monotonic_ms": -1},
        {"last_rx_monotonic_ms": True},
        {"last_rx_monotonic_ms": "1"},
        {"last_rx_monotonic_ms": 2**64 - 1},
        {"version": 2},
        {"version": True},
        {"pid": -1},
    ],
)
def test_never_received_or_invalid_status_does_not_invent_contact(endpoint, change):
    _, owner = endpoint
    payload = {
        "version": 1,
        "pid": owner.pid,
        "last_rx_monotonic_ms": time.monotonic_ns() // 1_000_000,
    } | change
    assert query_response(endpoint, json.dumps(payload).encode() + b"\n") is None


@pytest.mark.parametrize("response", [b"partial", b"bad json\n", b"[]\n", b"x" * 600])
def test_bounded_truncated_and_malformed_responses_are_unavailable(endpoint, response):
    assert query_response(endpoint, response) is None


def test_pid_reuse_is_rejected_after_query(endpoint):
    _, owner = endpoint
    response = (
        json.dumps({"version": 1, "pid": owner.pid, "last_rx_monotonic_ms": 1}).encode()
        + b"\n"
    )
    assert (
        query_response(
            endpoint,
            response,
            lookup=lambda pid: replace(owner, started=owner.started + 1),
        )
        is None
    )


def test_old_server_missing_endpoint_and_unsafe_directories_are_unavailable(endpoint):
    directory, owner = endpoint
    assert mosh_status.read(owner, clients.process) is None
    path = directory / f"{owner.pid}.sock"
    path.write_text("not a socket")
    assert mosh_status.read(owner, clients.process) is None
    directory.chmod(0o755)
    assert mosh_status.read(owner, clients.process) is None
    directory.chmod(0o700)
    assert mosh_status.read(replace(owner, uid=owner.uid + 1), clients.process) is None


def test_hung_endpoint_has_a_bounded_query_deadline(endpoint):
    directory, owner = endpoint
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        path = directory / f"{owner.pid}.sock"
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(1)
        started = time.monotonic()
        assert mosh_status.read(owner, clients.process, timeout=0.05) is None
        assert time.monotonic() - started < 0.25


def test_unified_table_never_uses_tmux_activity_as_network_idle():
    result = {
        "clients": [
            {
                "session_id": "$1",
                "session": "local",
                "app": "fish",
                "app_pid": 1,
                "transport": "LOCAL",
                "attachment": "ATTACHED",
                "reachability": None,
                "tty": "/dev/pts/0",
                "frontend_pid": 2,
                "idle_seconds": 123,
                "peer": "-",
                "width": 80,
                "height": 24,
                "sizing": "participating",
            }
        ],
        "sessions": [
            {
                "id": "$2",
                "name": "detached",
                "attached_clients": 0,
                "app": "fish",
                "app_pid": 3,
                "idle_seconds": 456,
            }
        ],
        "other_mosh_sessions": [],
    }
    assert all(row["idle_seconds"] is None for row in clients.unified_entries(result))
