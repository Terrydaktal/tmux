"""Bounded argv reads and read-only size queries for owned session terminals."""

import os
import shlex
import termios
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import mosh_sessions
import tmux_clients as clients


@pytest.fixture
def app(tmp_path):
    info = clients.Process(424242, 1, 1234, "python3.14", os.getuid(), "S")
    directory = tmp_path / str(info.pid)
    directory.mkdir()
    argv = ["/Runtime/python3.14", "Script with spaces.py", "", "$(touch nope)", "A'B"]
    (directory / "cmdline").write_bytes(
        b"\0".join(arg.encode() for arg in argv) + b"\0"
    )
    return info, argv


def test_command_keeps_case_argument_boundaries_and_literal_shell_tokens(tmp_path, app):
    info, argv = app
    command = clients.process_command(info, lambda pid: info, tmp_path)
    assert shlex.split(command) == argv
    assert not (tmp_path / "nope").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"pid": 42},
        {"started": 1235},
        {"uid": os.getuid() + 1},
        {"executable": "fish"},
        {"state": "Z"},
        {"state": "X"},
    ],
)
@pytest.mark.parametrize("when", ["before", "after"])
def test_command_rejects_exited_reused_or_replaced_process(tmp_path, app, change, when):
    info, _ = app
    changed = replace(info, **change)
    reads = iter([changed] if when == "before" else [info, changed])
    assert clients.process_command(info, lambda pid: next(reads), tmp_path) is None


@pytest.mark.parametrize("info", [None, "foreign", "zombie"])
def test_command_never_reads_an_unknown_or_foreign_app(tmp_path, app, info):
    original, _ = app
    info = (
        replace(original, uid=os.getuid() + 1)
        if info == "foreign"
        else replace(original, state="Z")
        if info == "zombie"
        else None
    )

    def unexpected(pid):
        pytest.fail("an unavailable app must not be looked up")

    assert clients.process_command(info, unexpected, tmp_path) is None


@pytest.mark.parametrize("exception", [FileNotFoundError, PermissionError, ValueError])
def test_command_metadata_failure_is_unavailable(tmp_path, app, exception):
    info, _ = app

    def lookup(pid):
        raise exception()

    assert clients.process_command(info, lookup, tmp_path) is None


def test_command_file_failure_is_unavailable(tmp_path, app):
    info, _ = app
    (tmp_path / str(info.pid) / "cmdline").unlink()
    assert clients.process_command(info, lambda pid: info, tmp_path) is None


@pytest.mark.parametrize("raw", [b"", b"x" * 17, b"python\0invalid-\xff.py\0"])
def test_command_read_is_bounded_and_invalid_utf8_is_safe(
    tmp_path, app, monkeypatch, raw
):
    info, _ = app
    monkeypatch.setattr(clients, "MAX_COMMAND_BYTES", 16 if b"\xff" not in raw else 64)
    (tmp_path / str(info.pid) / "cmdline").write_bytes(raw)
    command = clients.process_command(info, lambda pid: info, tmp_path)
    if b"\xff" in raw:
        assert command == "python 'invalid-\ufffd.py'"
    else:
        assert command is None


def test_compact_command_exposes_script_not_long_runtime_prefix():
    home = str(Path.home())
    command = shlex.join(
        [home + "/.cache/uv/python/bin/python3.14", home + "/Work/A B.py"]
    )
    assert shlex.split(clients.compact_command(command)) == [
        "python3.14",
        "~/Work/A B.py",
    ]


@pytest.mark.parametrize(
    "flags",
    [
        [],
        ["--verbose"],
        ["--verbose", "--json"],
        ["--once"],
        ["--flat"],
        ["--verbose", "--json", "--once", "--flat"],
    ],
)
def test_clients_cli_passes_verbose_without_starting_a_server(
    tmp_path, monkeypatch, flags
):
    socket = tmp_path / "absent.sock"
    calls = []
    monkeypatch.setattr(
        clients.sys, "argv", ["tmux-mosh", "-S", str(socket), "clients", *flags]
    )
    monkeypatch.setattr(
        clients,
        "show_clients",
        lambda server, as_json, *, verbose, once, flat: calls.append(
            (server.socket, as_json, verbose, once, flat)
        ),
    )
    assert clients.public_main() == 0
    assert calls == [
        (
            socket,
            "--json" in flags,
            "--verbose" in flags,
            "--once" in flags,
            "--flat" in flags,
        )
    ]
    assert not socket.exists()


@pytest.fixture
def pty():
    master, slave = os.openpty()
    try:
        yield master, slave, os.ttyname(slave)
    finally:
        os.close(master)
        os.close(slave)


def test_terminal_size_is_columns_by_rows_without_consuming_input_or_resizing(pty):
    master, slave, path = pty
    termios.tcsetwinsize(slave, (77, 110))
    attributes = termios.tcgetattr(slave)
    os.set_blocking(slave, False)
    os.write(master, b"pending\n")
    assert mosh_sessions.terminal_size(path, os.fstat(slave).st_rdev) == (110, 77)
    assert os.read(slave, 8) == b"pending\n"
    assert termios.tcgetwinsize(slave) == (77, 110)
    assert termios.tcgetattr(slave) == attributes


@pytest.mark.parametrize("dimensions", [(0, 0), (0, 110), (77, 0)])
def test_zero_terminal_dimensions_remain_unavailable(pty, dimensions):
    _, slave, path = pty
    termios.tcsetwinsize(slave, dimensions)
    assert mosh_sessions.terminal_size(path) == (None, None)


@pytest.mark.parametrize("path", [None, "", "/dev/tty", "/dev/pts/../1", "/tmp/1"])
def test_only_pts_paths_are_opened(path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("an unverified terminal path must not be opened")

    monkeypatch.setattr(mosh_sessions.os, "open", unexpected)
    assert mosh_sessions.terminal_size(path) == (None, None)


@pytest.mark.parametrize("change", ["foreign-owner", "regular-file", "replaced-device"])
def test_terminal_validation_failure_closes_the_descriptor(pty, monkeypatch, change):
    _, slave, path = pty
    metadata = os.fstat(slave)
    fake = SimpleNamespace(
        st_uid=metadata.st_uid + (change == "foreign-owner"),
        st_mode=0o100600 if change == "regular-file" else metadata.st_mode,
        st_rdev=metadata.st_rdev,
    )
    opened = []
    original = os.open

    def record(*args, **kwargs):
        flags = args[1]
        assert flags & os.O_ACCMODE == os.O_RDONLY
        for flag in (os.O_NONBLOCK, os.O_NOCTTY, os.O_CLOEXEC, os.O_NOFOLLOW):
            assert flags & flag
        opened.append(original(*args, **kwargs))
        return opened[-1]

    with monkeypatch.context() as patch:
        patch.setattr(mosh_sessions.os, "open", record)
        patch.setattr(mosh_sessions.os, "fstat", lambda fd: fake)
        device = metadata.st_rdev + (change == "replaced-device")
        assert mosh_sessions.terminal_size(path, device) == (None, None)
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_terminal_ioctl_failure_is_unavailable_and_closes_the_descriptor(
    pty, monkeypatch
):
    _, _, path = pty
    opened = []

    def fail(fd, *args):
        opened.append(fd)
        raise OSError("PTY disappeared")

    monkeypatch.setattr(mosh_sessions.fcntl, "ioctl", fail)
    assert mosh_sessions.terminal_size(path) == (None, None)
    with pytest.raises(OSError):
        os.fstat(opened[0])
