import shlex
import subprocess
import sys

import pytest
from terminal_harness import Attachment


def tmux_argv(backend, *args):
    return backend.cli(*args)


def clients(backend):
    return backend.run(
        "list-clients", "-F", "#{client_pid}", check=False
    ).stdout.splitlines()


@pytest.mark.parametrize(
    "command",
    [
        ["new-session", "-A", "-s", "diet"],
        ["new-session", "-s", "diet", "-A"],
        ["new", "-As", "diet"],
    ],
)
def test_legacy_create_or_attach_preserves_program_and_other_viewers(backend, command):
    shell = backend.socket.parent / "login-shell"
    shell.write_text("#!/bin/sh\nexec " + shlex.join(backend.program()) + "\n")
    shell.chmod(0o700)
    backend.env["SHELL"] = str(shell)
    first = Attachment(backend, argv=tmux_argv(backend, *command))
    second = None
    try:
        first.until(lambda state: first.exited or b"READY" in state["screen"])
        assert not first.exited, first.wire
        pid = backend.run("display-message", "-p", "-t", "=diet", "#{pane_pid}").stdout
        # Like vanilla -A, a supplied program is ignored on reattachment.
        second = Attachment(
            backend,
            argv=tmux_argv(
                backend, *command, "--", str(backend.socket.parent / "not-a-program")
            ),
        )
        second.until(lambda state: second.exited or len(clients(backend)) == 2)
        assert not second.exited, second.wire
        assert len(clients(backend)) == 2
        assert backend.run("list-sessions", "-F", "#{session_name}").stdout == b"diet\n"
        assert (
            backend.run("display-message", "-p", "-t", "=diet", "#{pane_pid}").stdout
            == pid
        )
    finally:
        first.close()
        if second:
            second.close()


def test_legacy_new_without_A_refuses_an_existing_session(backend):
    first = backend.attach()
    second = None
    try:
        first.until(lambda state: b"READY" in state["screen"])
        pid = backend.field("pane_pid")
        second = Attachment(
            backend, argv=tmux_argv(backend, "new-session", "-s", "test")
        )
        second.until(lambda state: second.exited)
        assert b"duplicate session" in second.wire
        assert backend.field("pane_pid") == pid
        assert len(clients(backend)) == 1
    finally:
        first.close()
        if second:
            second.close()


@pytest.mark.parametrize("separator", [[], ["--"]])
def test_legacy_program_argv_preserves_options_and_literal_separator(
    backend, separator
):
    payload = "a space; touch SHOULD_NOT_EXIST"
    program = [
        sys.executable,
        "-c",
        "import os,sys; print('ARG='+sys.argv[1], flush=True); "
        "print('TAIL='+repr(sys.argv[2:]), flush=True); os.read(0, 1)",
        payload,
        "--",
        "-A",
        "-s",
        "not-a-session",
    ]
    app = Attachment(
        backend,
        argv=tmux_argv(backend, "new-session", "-s", "test", *separator, *program),
    )
    try:
        app.until(lambda state: app.exited or b"TAIL=" in state["screen"])
        assert not app.exited, app.wire
        screen = app.term.state["screen"]
        assert ("ARG=" + payload).encode() in screen
        assert b"TAIL=['--', '-A', '-s', 'not-a-session']" in screen
        assert not (backend.socket.parent / "SHOULD_NOT_EXIST").exists()
    finally:
        app.close()


def test_legacy_single_string_program_keeps_native_shell_command_semantics(backend):
    command = "printf 'LEGACY_COMMAND_READY\\n'; exec " + shlex.join(backend.program())
    app = Attachment(
        backend,
        argv=tmux_argv(backend, "new-session", "-A", "-s", "test", command),
    )
    try:
        app.until(
            lambda state: (
                app.exited
                or (backend.log.exists() and b"LEGACY_COMMAND_READY" in state["screen"])
            )
        )
        assert not app.exited, app.wire
        assert backend.log.exists()
    finally:
        app.close()


@pytest.mark.parametrize(
    "args",
    [
        ["new-session", "-s"],
        ["new-session", "--not-a-tmux-option"],
        ["not-a-native-command"],
        ["attach-session", "-t", "missing"],
    ],
)
def test_legacy_invalid_or_nonterminal_arguments_have_no_side_effects(backend, args):
    result = subprocess.run(
        tmux_argv(backend, *args),
        env=backend.env,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert backend.run("has-session", check=False).returncode != 0
    assert not backend.log.exists()
