"""Running binary dates survive installed replacements without restarting apps."""

import os
from dataclasses import replace
from datetime import UTC, datetime

import pytest

import opsec_sessions
import tmux_clients as clients

STAMP = datetime(2026, 9, 30, 16, 32, tzinfo=UTC).timestamp()


@pytest.fixture
def binary(tmp_path):
    info = clients.Process(424242, 1, 1234, "xfce4-terminal", os.getuid(), "S")
    root = tmp_path / "proc"
    directory = root / str(info.pid)
    directory.mkdir(parents=True)
    fields = ["0"] * 20
    fields[0], fields[19] = "S", str(info.started)
    (directory / "stat").write_text(f"{info.pid} (xfce4-terminal) " + " ".join(fields))
    installed = tmp_path / "xfce4-terminal"
    installed.write_bytes(b"old build")
    os.utime(installed, (STAMP, STAMP))
    os.link(installed, directory / "exe")
    return info, root, installed


@pytest.fixture(params=["host", "guest"])
def read_mtime(request):
    if request.param == "host":
        return lambda info, root: clients.process_binary_mtime(
            info, lambda pid: info, root
        )
    namespace = {}
    prefix = opsec_sessions.STATUS_SCRIPT.split("requests = json.load", 1)[0]
    exec(compile(prefix, "guest-binary-metadata", "exec"), namespace)  # noqa: S102 - fixed guest code, fake proc root
    return lambda info, root: namespace["binary_mtime"](info.pid, root)


def test_timestamp_comes_from_running_inode_not_installed_replacement(
    binary, read_mtime
):
    info, root, installed = binary
    installed.unlink()
    installed.write_bytes(b"new build")
    os.utime(installed, (STAMP + 3600, STAMP + 3600))
    assert read_mtime(info, root) == STAMP
    assert installed.stat().st_mtime == STAMP + 3600


def test_missing_executable_date_is_unavailable(binary, read_mtime):
    info, root, _ = binary
    (root / str(info.pid) / "exe").unlink()
    assert read_mtime(info, root) is None


def test_executable_symlink_can_refer_to_an_old_deleted_build(binary, read_mtime):
    info, root, installed = binary
    executable = root / str(info.pid) / "exe"
    executable.unlink()
    old = installed.with_name("xfce4-terminal (deleted)")
    installed.rename(old)
    executable.symlink_to(old)
    assert read_mtime(info, root) == STAMP


@pytest.mark.parametrize(
    "change",
    [
        {"pid": 42},
        {"started": 1235},
        {"uid": os.getuid() + 1},
        {"executable": "mosh-server"},
        {"state": "Z"},
        {"state": "X"},
    ],
)
@pytest.mark.parametrize("when", ["before", "after"])
def test_host_rejects_reused_exited_or_changed_process(binary, change, when):
    info, root, _ = binary
    changed = replace(info, **change)
    reads = iter([changed] if when == "before" else [info, changed])
    assert clients.process_binary_mtime(info, lambda pid: next(reads), root) is None


def test_host_rejects_exec_change_with_same_name_and_start_time(binary):
    info, root, _ = binary
    executable = root / str(info.pid) / "exe"
    calls = 0

    def lookup(pid):
        nonlocal calls
        calls += 1
        if calls == 2:
            executable.unlink()
            executable.write_bytes(b"another build of the same program")
            os.utime(executable, (STAMP + 3600, STAMP + 3600))
        return info

    assert clients.process_binary_mtime(info, lookup, root) is None


@pytest.mark.parametrize("kind", ["unknown", "foreign", "zombie"])
def test_host_does_not_read_unavailable_process(binary, kind):
    info, root, _ = binary
    info = (
        None
        if kind == "unknown"
        else replace(info, uid=os.getuid() + 1)
        if kind == "foreign"
        else replace(info, state="Z")
    )

    def unexpected(pid):
        pytest.fail("unavailable process must not be queried")

    assert clients.process_binary_mtime(info, unexpected, root) is None


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError, ValueError])
def test_failed_metadata_reads_leave_date_unavailable(binary, error):
    info, root, _ = binary

    def lookup(pid):
        raise error()

    assert clients.process_binary_mtime(info, lookup, root) is None


def test_via_names_do_not_embed_dates_or_process_identifiers():
    assert clients.format_via("LOCAL", "xfce4-terminal") == "xfce4-terminal"
    assert clients.format_via("MOSH", idle_seconds=0) == "mosh"
