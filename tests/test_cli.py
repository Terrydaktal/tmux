import os
import subprocess
import sys

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


def test_list_help_version_do_not_create_server(backend):
    assert b"No tmux-simple sessions" in cli(backend, "list").stdout
    assert b"stock Mosh" in cli(backend, "--help").stdout
    assert cli(backend, "--version").stdout == b"tmux-simple 0.1.0\n"
    assert not backend.socket.exists()


@pytest.mark.parametrize(
    "args",
    [
        ["attach", "test"],
        ["attach", "bad.name"],
        ["kill", "test"],
        ["list", "--", "touch", "bad"],
        ["attach", "test", "--"],
    ],
)
def test_invalid_or_nonterminal_invocations_have_no_session_side_effects(backend, args):
    result = cli(backend, *args)
    assert result.returncode == 2
    assert not backend.socket.exists()
    assert not (backend.socket.parent / "bad").exists()


def test_missing_existing_session_does_not_create_it(backend):
    app = backend.attach(existing=True)
    try:
        app.until(lambda s: app.exited)
        assert b"session does not exist" in app.wire
        assert not backend.socket.exists()
        assert not backend.log.exists()
    finally:
        app.close()


def test_reconnect_detach_and_explicit_kill(backend):
    first = backend.attach()
    second = None
    try:
        first.until(lambda s: b"READY" in s["screen"])
        assert b"test\t1 attached" in cli(backend, "list").stdout
        second = backend.attach(existing=True, cols=45, rows=16)
        second.until(lambda s: b"READY" in s["screen"])
        assert len(backend.run("list-clients").stdout.splitlines()) == 2
        assert backend.field("pane_width") == b"45"
        assert backend.field("pane_height") == b"16"
        assert cli(backend, "detach", "test").returncode == 0
        first.until(lambda s: first.exited)
        second.until(lambda s: second.exited)
        assert backend.run("has-session", "-t", "=test").returncode == 0
        assert cli(backend, "kill", "test", "--yes").returncode == 0
        assert backend.run("has-session", "-t", "=test", check=False).returncode != 0
    finally:
        first.close()
        if second:
            second.close()


def test_foreign_server_is_not_modified(backend):
    subprocess.run(
        [
            *backend.prefix,
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            "foreign",
            "--",
            "/bin/sleep",
            "30",
        ],
        env=backend.env,
        capture_output=True,
        timeout=5,
        check=True,
    )
    before = backend.run("show-options", "-g").stdout
    assert b"refusing to use a server" in cli(backend, "list").stderr
    assert backend.run("show-options", "-g").stdout == before
    assert backend.run("has-session", "-t", "=foreign").returncode == 0


def test_program_argv_is_not_interpreted_as_tmux_or_shell_commands(backend):
    payload = "a space; touch SHOULD_NOT_EXIST;"
    program = [
        sys.executable,
        "-c",
        "import os,sys; print('ARG='+sys.argv[1], flush=True); os.read(0, 1)",
        payload,
    ]
    app = Attachment(backend, argv=backend.cli("attach", "test", "--", *program))
    try:
        app.until(lambda s: ("ARG=" + payload).encode() in s["screen"])
        assert not (backend.socket.parent / "SHOULD_NOT_EXIST").exists()
    finally:
        app.close()


@pytest.mark.parametrize("as_tmux", [False, True])
def test_symlink_install_is_idempotent_and_refuses_conflicts(backend, as_tmux):
    command = ["bash", str(ROOT / "scripts/link.sh")]
    if as_tmux:
        command.append("--as-tmux")
    for _ in range(2):
        subprocess.run(
            command, env=backend.env, capture_output=True, timeout=5, check=True
        )
    assert backend.launcher.is_symlink()
    alias = backend.launcher.with_name("tmux")
    if as_tmux:
        assert alias.is_symlink()
        assert alias.resolve() == backend.launcher.resolve()
        assert (
            subprocess.run(
                [str(alias), "--version"],
                env=backend.env,
                capture_output=True,
                timeout=5,
                check=True,
            ).stdout
            == b"tmux-simple 0.1.0\n"
        )
    else:
        assert not alias.exists()
    backend.launcher.unlink()
    backend.launcher.write_text("do not replace")
    result = subprocess.run(command, env=backend.env, capture_output=True, timeout=5)
    assert result.returncode == 1
    assert backend.launcher.read_text() == "do not replace"


def test_tmux_alias_conflict_does_not_partially_install(backend):
    backend.launcher.unlink()
    alias = backend.launcher.with_name("tmux")
    alias.write_text("existing tmux wrapper")
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/link.sh"), "--as-tmux"],
        env=backend.env,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 1
    assert alias.read_text() == "existing tmux wrapper"
    assert not backend.launcher.exists()


def test_unknown_install_option_changes_nothing(backend):
    backend.launcher.unlink()
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/link.sh"), "--unknown"],
        env=backend.env,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 2
    assert not backend.launcher.exists()
    assert not backend.launcher.with_name("tmux").exists()


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
