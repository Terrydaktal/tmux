"""Read-only ownership reporting against real, private native tmux servers."""

import pytest
from terminal_harness import Attachment

import tmux_clients as clients


@pytest.fixture(params=[(40, 16), (100, 30)], ids=["different-size", "same-size"])
def viewers(backend, request):
    desktop = backend.attach(cols=100, rows=30)
    phone = None
    try:
        desktop.until(lambda state: b"READY" in state["screen"])
        phone = backend.attach(
            existing=True, cols=request.param[0], rows=request.param[1]
        )
        phone.until(lambda state: b"READY" in state["screen"])
        phone.send(b"phone-active")
        phone.until(lambda state: backend.received().endswith(b"phone-active"))
        server = clients.Server(backend.binary, backend.socket, backend.env)
        yield desktop, phone, server
    finally:
        if phone is not None:
            phone.close()
        desktop.close()


def assert_owner(server, expected_pid):
    rows, _ = server.snapshot(include_control=True)
    assert {row.sizing_client_pid for row in rows} == {expected_pid}
    assert [row.pid for row in rows if row.active_sizing] == [expected_pid]
    assert all(row.sizing_policy == "latest" for row in rows)
    return rows


def test_ownership_follows_input_not_dimensions_or_report_queries(viewers, backend):
    desktop, phone, server = viewers
    rows = assert_owner(server, phone.pid)
    assert len({row.window_id for row in rows}) == 1
    pane_pid = backend.field("pane_pid")
    before = backend.run(
        "display-message", "-p", "#{window_width}:#{window_height}:#{pane_in_mode}"
    ).stdout
    for _ in range(3):
        result = clients.inventory(server)
        assert [row["pid"] for row in result["clients"] if row["active_sizing"]] == [
            phone.pid
        ]
        assert sorted(
            clients.format_sizing(row)
            for row in result["entries"]
            if row["type"] == "TMUX"
        ) == ["active", "standby"]
    assert (
        before
        == backend.run(
            "display-message", "-p", "#{window_width}:#{window_height}:#{pane_in_mode}"
        ).stdout
    )
    desktop.send(b"desktop-active")
    desktop.until(lambda state: backend.received().endswith(b"desktop-active"))
    assert_owner(server, desktop.pid)
    phone.send(b"phone-again")
    phone.until(lambda state: backend.received().endswith(b"phone-again"))
    assert_owner(server, phone.pid)
    assert backend.field("pane_pid") == pane_pid


def test_ignored_latest_does_not_override_the_actual_eligible_owner(viewers, backend):
    _, phone, server = viewers
    rows = assert_owner(server, phone.pid)
    desktop = next(row for row in rows if row.pid != phone.pid)
    backend.run(
        "refresh-client",
        "-t",
        next(row.tty for row in rows if row.pid == phone.pid),
        "-f",
        "ignore-size",
    )
    backend.run("set-option", "-g", "@test-sizing-recalculate", "1")
    assert_owner(server, desktop.pid)
    result = clients.inventory(server)
    ignored = next(row for row in result["clients"] if row["pid"] == phone.pid)
    assert ignored["sizing"] == "ignored-manual"
    assert clients.format_sizing(ignored) == "manual-off"


def test_resizing_a_viewer_transfers_ownership_without_application_input(
    viewers, backend
):
    desktop, phone, server = viewers
    assert_owner(server, phone.pid)
    before = backend.received()
    desktop.resize(104, 32)
    desktop.until(
        lambda state: (
            backend.field("window_size_client_pid") == str(desktop.pid).encode()
        )
    )
    assert_owner(server, desktop.pid)
    phone.resize(42, 20)
    phone.until(
        lambda state: backend.field("window_size_client_pid") == str(phone.pid).encode()
    )
    assert_owner(server, phone.pid)
    assert backend.received() == before


def test_all_ignored_clients_use_native_fallback_not_an_invented_exclusion(
    viewers, backend
):
    _, phone, server = viewers
    for row in assert_owner(server, phone.pid):
        backend.run("refresh-client", "-t", row.tty, "-f", "ignore-size")
    backend.run("set-option", "-g", "@test-sizing-recalculate", "1")
    assert_owner(server, phone.pid)
    result = clients.inventory(server)
    active = next(row for row in result["clients"] if row["active_sizing"])
    assert active["sizing"] == "ignored-manual"
    assert clients.format_sizing(active) == "active"


def test_owner_detaches_and_remaining_viewer_controls_the_window(viewers, backend):
    desktop, phone, server = viewers
    row = next(row for row in assert_owner(server, phone.pid) if row.pid == phone.pid)
    backend.run("detach-client", "-t", row.tty)
    desktop.until(lambda state: len(server.snapshot()[0]) == 1)
    assert_owner(server, desktop.pid)


@pytest.mark.parametrize("policy", ["manual", "smallest", "largest"])
def test_non_latest_policies_do_not_claim_a_single_active_viewer(
    viewers, backend, policy
):
    _, _, server = viewers
    backend.run("set-option", "-w", "window-size", policy)
    rows, _ = server.snapshot()
    assert {row.sizing_client_pid for row in rows} == {0}
    assert {row.sizing_policy for row in rows} == {policy}
    assert all(row.active_sizing is False for row in rows)
    result = clients.inventory(server)
    assert {clients.format_sizing(row) for row in result["clients"]} == {
        "manual" if policy == "manual" else "shared"
    }


def test_older_server_reports_unknown_not_a_guessed_owner(viewers, monkeypatch):
    _, _, server = viewers
    run = server.run

    def without_ownership_format(*args):
        # Unknown native formats expand to an empty string on an old server.
        return (
            "\n".join(
                line.rsplit("\t", 1)[0] + "\t" if line.startswith("CLIENT\t") else line
                for line in run(*args).splitlines()
            )
            + "\n"
        )

    monkeypatch.setattr(server, "run", without_ownership_format)
    result = clients.inventory(server)
    assert all(row["active_sizing"] is None for row in result["clients"])
    assert all(row["sizing_client_pid"] is None for row in result["clients"])
    assert {clients.format_sizing(row) for row in result["clients"]} == {"unknown"}
    assert "older tmux server" in result["note"]
    assert "Existing sessions have not been restarted" in result["note"]


def test_separate_windows_have_separate_sizing_owners(backend):
    first = backend.attach(cols=100, rows=30)
    other = None
    try:
        first.until(lambda state: b"READY" in state["screen"])
        backend.run("new-session", "-d", "-s", "other", "sleep 60")
        other = Attachment(
            backend,
            cols=40,
            rows=16,
            argv=backend.cli("attach-session", "-t", "=other"),
        )
        server = clients.Server(backend.binary, backend.socket, backend.env)
        other.until(lambda state: len(server.snapshot()[0]) == 2)
        rows, _ = server.snapshot()
        assert len({row.window_id for row in rows}) == 2
        assert all(row.active_sizing for row in rows)
        assert {row.sizing_client_pid for row in rows} == {first.pid, other.pid}
    finally:
        if other is not None:
            other.close()
        first.close()


@pytest.mark.parametrize(
    "participation,active,policy,expected",
    [
        ("participating", True, "latest", "active"),
        ("participating", False, "latest", "standby"),
        ("participating", None, "latest", "unknown"),
        ("participating", False, "manual", "manual"),
        ("participating", False, "smallest", "shared"),
        ("participating", False, "largest", "shared"),
        ("ignored-auto", False, "latest", "auto-off"),
        ("ignored-manual", False, "latest", "manual-off"),
        ("ignored-manual", True, "latest", "active"),
        ("ignored-unknown", None, "latest", "unknown"),
        ("not-applicable", False, "latest", "-"),
        (None, None, None, "-"),
    ],
)
def test_sizing_labels_do_not_conflate_participation_with_ownership(
    participation, active, policy, expected
):
    assert (
        clients.format_sizing(
            {"sizing": participation, "active_sizing": active, "sizing_policy": policy}
        )
        == expected
    )
