import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT
from terminal_harness import Attachment, Emulator


def cli(backend, *args, **kwargs):
    return subprocess.run(
        backend.cli(*args),
        capture_output=True,
        env=backend.env,
        cwd=backend.socket.parent,
        timeout=5,
        **kwargs,
    )


def test_entrypoint_is_the_binary_and_version_does_not_start_server(backend):
    assert backend.entrypoint.resolve() == Path(backend.binary).resolve()
    assert cli(backend, "-V").stdout == b"tmux 3.7c-simple6\n"
    assert not backend.socket.exists()


def test_missing_existing_session_does_not_create_it(backend):
    app = backend.attach(existing=True)
    try:
        app.until(lambda s: app.exited)
        assert not backend.log.exists()
        assert backend.run("has-session", check=False).returncode != 0
    finally:
        app.close()


def test_native_reconnect_detach_and_kill_preserve_the_running_program(backend):
    first = backend.attach()
    second = None
    try:
        first.until(lambda s: b"READY" in s["screen"])
        pid = backend.field("pane_pid")
        assert (
            cli(backend, "list-sessions", "-F", "#{session_name}").stdout == b"test\n"
        )
        second = backend.attach(existing=True, cols=45, rows=16)
        second.until(lambda s: b"READY" in s["screen"])
        assert len(cli(backend, "list-clients").stdout.splitlines()) == 2
        assert backend.field("pane_pid") == pid
        assert cli(backend, "detach-client", "-s", "=test").returncode == 0
        first.until(lambda s: first.exited)
        second.until(lambda s: second.exited)
        assert backend.run("has-session", "-t", "=test").returncode == 0
        assert cli(backend, "kill-session", "-t", "=test").returncode == 0
        assert backend.run("has-session", "-t", "=test", check=False).returncode != 0
    finally:
        first.close()
        if second:
            second.close()


def test_native_commands_allow_additional_windows_and_session_names(backend):
    first = backend.attach()
    second = None
    try:
        first.until(lambda s: b"READY" in s["screen"])
        assert (
            cli(
                backend, "new-window", "-d", "-t", "=test", "--", "/bin/sleep", "30"
            ).returncode
            == 0
        )
        assert len(backend.run("list-windows", "-t", "=test").stdout.splitlines()) == 2
        second = Attachment(
            backend,
            argv=backend.cli(
                "new-session", "-s", "work session", "--", *backend.program()
            ),
        )
        second.until(lambda s: b"READY" in s["screen"])
        assert b"work session" in cli(backend, "list-sessions").stdout
    finally:
        if second:
            second.close()
        first.close()


def test_native_multi_argument_program_keeps_argument_boundaries(backend):
    payload = "a space; touch SHOULD_NOT_EXIST"
    program = [
        sys.executable,
        "-c",
        "import os,sys; print('ARG='+sys.argv[1], flush=True); os.read(0, 1)",
        payload,
    ]
    app = Attachment(
        backend, argv=backend.cli("new-session", "-s", "test", "--", *program)
    )
    try:
        app.until(lambda s: ("ARG=" + payload).encode() in s["screen"])
        assert not (backend.socket.parent / "SHOULD_NOT_EXIST").exists()
    finally:
        app.close()


def test_installer_links_native_binary_and_helper_and_retires_only_old_alias(backend):
    legacy = backend.entrypoint.with_name("tmux-simple")
    legacy.symlink_to(ROOT / "tmux-simple")
    command = ["bash", str(ROOT / "scripts/link.sh")]
    for _ in range(2):
        subprocess.run(
            command, env=backend.env, capture_output=True, timeout=5, check=True
        )
    assert backend.entrypoint.resolve() == (ROOT / "build/runtime/tmux").resolve()
    assert backend.mosh_helper.resolve() == ROOT / "tmux-mosh"
    for name in ("mosh", "mosh-client", "mosh-server"):
        link = backend.entrypoint.with_name(name)
        assert link.is_symlink()
        assert link.resolve() == (ROOT / "build/runtime" / name).resolve()
    assert not legacy.exists() and not legacy.is_symlink()
    assert not backend.socket.exists()


@pytest.mark.parametrize(
    "conflict",
    ["tmux", "tmux-mosh", "tmux-simple", "mosh", "mosh-client", "mosh-server"],
)
def test_installer_refuses_conflicts_without_partial_changes(backend, conflict):
    target = backend.entrypoint.with_name(conflict)
    if target.is_symlink():
        target.unlink()
    target.write_text("keep this file")
    before = {
        name: (
            backend.entrypoint.with_name(name).readlink()
            if backend.entrypoint.with_name(name).is_symlink()
            else None
        )
        for name in ("tmux", "tmux-mosh")
    }
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/link.sh")],
        env=backend.env,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert target.read_text() == "keep this file"
    for name, link in before.items():
        if link is not None:
            assert backend.entrypoint.with_name(name).readlink() == link


def test_unknown_install_option_changes_nothing(backend):
    backend.entrypoint.unlink()
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/link.sh"), "--unknown"],
        env=backend.env,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert not backend.entrypoint.exists()


def test_clipboard_helper_only_uses_available_explicit_display(backend):
    tools = backend.socket.parent / "fake-tools"
    tools.mkdir()
    tool = tools / "wl-copy"
    tool.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n"
    )
    tool.chmod(0o700)
    env = {
        **backend.env,
        "PATH": str(tools) + os.pathsep + backend.env["PATH"],
        "WAYLAND_DISPLAY": "owned-test-display",
    }
    command = ["bash", str(ROOT / "scripts/clipboard.sh")]
    result = subprocess.run(
        command,
        input=b"selected-test-text",
        env=env,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert result.stdout == b"selected-test-text"
    del env["WAYLAND_DISPLAY"]
    result = subprocess.run(
        command,
        input=b"not-for-a-clipboard",
        env=env,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert result.stdout == b""


@pytest.mark.parametrize("kind", ["termux", "vte"])
def test_oracle_reads_actual_alternate_viewport(backend, kind):
    term = Emulator(kind)
    try:
        term.feed(b"old-main\r\n" * 80)
        state = term.feed(b"\x1b[?1049h\x1b[2J\x1b[Hvisible-alt\x1b[24;1Hlast-alt")
        assert b"visible-alt" in state["screen"] and b"last-alt" in state["screen"]
        assert b"old-main" not in state["screen"]
        state = term.feed(b"\x1b[?1049l")
        assert b"old-main" in state["screen"]
        assert b"visible-alt" not in state["screen"]
    finally:
        term.close()
