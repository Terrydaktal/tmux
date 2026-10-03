import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest
from conftest import ROOT, mosh

import tmux_clients


def environment(backend, name):
    result = backend.run("show-environment", "-t", "=test", name, check=False)
    return result.stdout.rstrip(b"\n")


@pytest.mark.parametrize(
    "runtime,inherited,expected",
    [
        ("", "", "/tmp"),
        ("relative", "", "/tmp"),
        ("/owned/runtime", "", "/owned/runtime"),
        ("/owned/runtime", "/inherited.sock,123,0", "/inherited.sock"),
        ("/owned/runtime", "/invalid.sock,x,y", "/owned/runtime"),
    ],
)
def test_mosh_helper_default_socket_matches_fish(
    runtime, inherited, expected, monkeypatch
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
    monkeypatch.setenv("TMUX", inherited)
    assert tmux_clients.default_socket() == (
        Path(expected)
        if expected.endswith(".sock")
        else Path(expected) / f"tmux-simple-{os.getuid()}" / "server.sock"
    )


def test_desktop_display_is_updated_and_phone_does_not_clear_it(backend):
    desktop = backend.attach()
    later_desktop = phone = None
    try:
        desktop.until(lambda state: b"READY" in state["screen"])
        backend.env.update(
            DISPLAY=":73",
            WAYLAND_DISPLAY="owned-wayland",
            XAUTHORITY="/private/test auth",
        )
        later_desktop = backend.attach(existing=True)
        later_desktop.until(
            lambda state: (
                environment(backend, "WAYLAND_DISPLAY")
                == b"WAYLAND_DISPLAY=owned-wayland"
            )
        )
        assert environment(backend, "DISPLAY") == b"DISPLAY=:73"
        assert environment(backend, "XAUTHORITY") == b"XAUTHORITY=/private/test auth"
        for name in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"):
            backend.env.pop(name)
        backend.env["SSH_AUTH_SOCK"] = "/owned/phone-agent"
        phone = backend.attach(existing=True)
        phone.until(
            lambda state: len(backend.run("list-clients").stdout.splitlines()) == 3
        )
        phone.pump(0.2)
        assert (
            environment(backend, "WAYLAND_DISPLAY") == b"WAYLAND_DISPLAY=owned-wayland"
        )
        assert environment(backend, "DISPLAY") == b"DISPLAY=:73"
        assert environment(backend, "XAUTHORITY") == b"XAUTHORITY=/private/test auth"
        assert (
            environment(backend, "SSH_AUTH_SOCK") == b"SSH_AUTH_SOCK=/owned/phone-agent"
        )
    finally:
        if phone:
            phone.close()
        if later_desktop:
            later_desktop.close()
        desktop.close()


def test_attachment_environment_is_quoted_and_empty_values_are_ignored(backend):
    app = backend.attach()
    next_app = None
    try:
        app.until(lambda state: b"READY" in state["screen"])
        value = "owned path'; set -g @injected yes; 'tail"
        backend.env.update(WAYLAND_DISPLAY=value, DISPLAY="")
        backend.run("set-environment", "-t", "=test", "DISPLAY", ":74")
        next_app = backend.attach(existing=True)
        next_app.until(
            lambda state: (
                environment(backend, "WAYLAND_DISPLAY")
                == b"WAYLAND_DISPLAY=" + value.encode()
            )
        )
        assert environment(backend, "DISPLAY") == b"DISPLAY=:74"
        assert not backend.run("show-options", "-gv", "@injected", check=False).stdout
    finally:
        if next_app:
            next_app.close()
        app.close()


@pytest.mark.parametrize("create", [False, True])
def test_native_mosh_attach_starts_one_monitor_automatically(backend, create):
    desktop = backend.attach()
    remote = None
    try:
        desktop.until(lambda state: b"READY" in state["screen"])
        assert not backend.run(
            "show-options", "-gv", "@tmux-simple-sizing-monitor", check=False
        ).stdout
        remote = mosh.Session(
            backend.socket.parent,
            "termux",
            program=(
                backend.cli("new-session", "-s", "phone", "--", *backend.program())
                if create
                else backend.cli("attach-session", "-t", "=test")
            ),
        )
        remote.attachment.until(
            lambda state: bool(
                backend.run(
                    "show-options", "-gv", "@tmux-simple-sizing-monitor", check=False
                ).stdout.strip()
            )
        )
        watcher = backend.run(
            "show-options", "-gv", "@tmux-simple-sizing-monitor"
        ).stdout
        for _ in range(2):
            result = subprocess.run(
                backend.mosh_cli("ensure"),
                env=backend.env,
                capture_output=True,
                timeout=6,
            )
            assert result.returncode == 0, result.stderr
            assert (
                backend.run("show-options", "-gv", "@tmux-simple-sizing-monitor").stdout
                == watcher
            )
        status = json.loads(
            subprocess.run(
                backend.mosh_cli("clients", "--json"),
                env=backend.env,
                capture_output=True,
                check=True,
                timeout=6,
            ).stdout
        )
        assert status["monitor_running"]
        assert {client["transport"] for client in status["clients"]} >= {"MOSH"}
    finally:
        if remote:
            remote.close()
        desktop.close()


@pytest.mark.parametrize(
    "args,explicit",
    [
        (["new-session", "-A", "-s", "diet"], False),
        (["-S", "/owned/socket", "list-sessions"], True),
        (["-Llabel", "list-clients"], True),
        (["-uS", "/owned/socket", "list-sessions"], True),
        (["-f", "/owned/config", "-S", "/owned/socket", "list-sessions"], True),
        (["new-session", "-s", "test", "python3", "-S"], False),
    ],
)
def test_fish_preserves_native_arguments_and_explicit_sockets(backend, args, explicit):
    tools = backend.socket.parent / "tools"
    tools.mkdir()
    fake = tools / "tmux"
    fake.write_text("#!/bin/sh\nprintf '%s\\0' \"$@\"\n")
    fake.chmod(0o700)
    env = backend.env | {"PATH": str(tools) + os.pathsep + backend.env["PATH"]}
    function = ROOT.parent / "config/tmux-simple/tmux.fish"
    result = subprocess.run(
        [
            "fish",
            "--no-config",
            "-c",
            "source " + shlex.quote(str(function)) + "; tmux $argv",
            "--",
            *args,
        ],
        env=env,
        capture_output=True,
        timeout=5,
        check=True,
    )
    actual = result.stdout.rstrip(b"\0").decode().split("\0")
    expected = (
        args
        if explicit
        else [
            "-S",
            env["XDG_RUNTIME_DIR"] + f"/tmux-simple-{os.getuid()}/server.sock",
            *args,
        ]
    )
    assert actual == expected
    if not explicit:
        directory = fake.parent.parent / "home" / f"tmux-simple-{os.getuid()}"
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o777 == 0o700


def test_fish_uses_inherited_socket_inside_a_pane(backend):
    tools = backend.socket.parent / "tools"
    tools.mkdir()
    fake = tools / "tmux"
    fake.write_text("#!/bin/sh\nprintf '%s\\0' \"$@\"\n")
    fake.chmod(0o700)
    env = backend.env | {
        "PATH": str(tools) + os.pathsep + backend.env["PATH"],
        "TMUX": "/owned/inherited.sock,123,0",
    }
    result = subprocess.run(
        [
            "fish",
            "--no-config",
            "-c",
            "source "
            + shlex.quote(str(ROOT.parent / "config/tmux-simple/tmux.fish"))
            + "; tmux list-sessions",
        ],
        env=env,
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert result.stdout == b"list-sessions\0"
    assert not (tools.parent / "home" / f"tmux-simple-{os.getuid()}").exists()


@pytest.mark.parametrize("kind", ["symlink", "permissions"])
def test_fish_refuses_unsafe_default_socket_directory(backend, kind):
    directory = backend.socket.parent / "home" / f"tmux-simple-{os.getuid()}"
    if kind == "symlink":
        directory.symlink_to(backend.socket.parent, target_is_directory=True)
    else:
        directory.mkdir(mode=0o755)
    result = subprocess.run(
        [
            "fish",
            "--no-config",
            "-c",
            "source "
            + shlex.quote(str(ROOT.parent / "config/tmux-simple/tmux.fish"))
            + "; tmux new-session -s test",
        ],
        env=backend.env,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert b"unsafe private socket directory" in result.stderr
    assert not backend.socket.exists()
