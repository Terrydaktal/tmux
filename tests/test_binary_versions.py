"""Build comparisons use owned running images, never execute the applications."""

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

import opsec_sessions
import tmux_clients as clients


@pytest.fixture
def images(tmp_path):
    info = clients.Process(424242, 1, 1234, "xfce4-terminal", os.getuid(), "S")
    root = tmp_path / "proc"
    directory = root / str(info.pid)
    directory.mkdir(parents=True)
    fields = ["0"] * 20
    fields[0], fields[19] = "S", str(info.started)
    (directory / "stat").write_text(f"{info.pid} (xfce4-terminal) " + " ".join(fields))
    installed = tmp_path / "installed"
    installed.write_bytes(b"\x7fELFsame version string: old build")
    os.link(installed, directory / "exe")
    return info, root, installed


@pytest.fixture(params=["host", "guest"])
def implementation(request):
    if request.param == "host":
        return (
            clients.binary_fingerprint,
            lambda info, installed, root, fingerprints: clients.process_binary_outdated(
                info, installed, fingerprints, lambda pid: info, root
            ),
        )
    namespace = {}
    prefix = opsec_sessions.STATUS_SCRIPT.split("requests = json.load", 1)[0]
    exec(compile(prefix, "guest-binary-version", "exec"), namespace)  # noqa: S102 - fixed code against fake proc files
    return (
        namespace["binary_fingerprint"],
        lambda info, installed, root, fingerprints: namespace["binary_outdated"](
            info.pid, installed, fingerprints, root
        ),
    )


def test_current_image_matches_the_installed_binary(images, implementation):
    info, root, installed = images
    _, compare = implementation
    assert compare(info, installed, root, {}) is False


def test_replaced_binary_with_same_timestamp_and_version_is_outdated(
    images, implementation
):
    info, root, installed = images
    _, compare = implementation
    stamp = installed.stat().st_mtime_ns
    installed.unlink()
    installed.write_bytes(b"\x7fELFsame version string: new build")
    os.utime(installed, ns=(stamp, stamp))
    assert compare(info, installed, root, {}) is True


def test_identical_copy_with_a_different_timestamp_is_not_outdated(
    images, implementation
):
    info, root, installed = images
    _, compare = implementation
    copy = installed.with_name("same-build-copy")
    copy.write_bytes(installed.read_bytes())
    os.utime(copy, (1000, 1000))
    assert compare(info, copy, root, {}) is False


def test_unavailable_reference_is_unknown_not_outdated(images, implementation):
    info, root, installed = images
    _, compare = implementation
    assert compare(info, installed.with_name("missing"), root, {}) is None


def test_fingerprint_cache_reads_each_image_once(images, implementation, monkeypatch):
    _, _, installed = images
    fingerprint, _ = implementation
    opened = []
    original = Path.open

    def record(path, *args, **kwargs):
        opened.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", record)
    fingerprints = {}
    expected = hashlib.sha256(b"\x7fELFsame version string: old build").hexdigest()
    assert fingerprint(installed, fingerprints) == expected
    assert fingerprint(installed, fingerprints) == expected
    assert opened == [installed]


def test_cache_does_not_hide_an_installed_replacement(images, implementation):
    _, _, installed = images
    fingerprint, _ = implementation
    fingerprints = {}
    before = fingerprint(installed, fingerprints)
    installed.unlink()
    installed.write_bytes(b"\x7fELFnew replacement")
    assert fingerprint(installed, fingerprints) != before


@pytest.mark.parametrize(
    "kind", ["missing", "directory", "script", "oversized", "inaccessible"]
)
def test_unknown_or_unsafe_image_is_not_fingerprinted(
    images, implementation, monkeypatch, kind
):
    _, _, installed = images
    fingerprint, _ = implementation
    if kind == "missing":
        installed.unlink()
    elif kind == "directory":
        installed.unlink()
        installed.mkdir()
    elif kind == "script":
        installed.write_bytes(b"#!/bin/sh\nexit 1\n")
    elif kind == "oversized":
        with installed.open("r+b") as source:
            source.truncate(64 * 1024 * 1024 + 1)
    else:

        def denied(*args, **kwargs):
            raise PermissionError()

        monkeypatch.setattr(Path, "open", denied)
    assert fingerprint(installed, {}) is None


@pytest.mark.parametrize(
    "change",
    [
        {"started": 1235},
        {"uid": os.getuid() + 1},
        {"state": "Z"},
        {"executable": "fish"},
    ],
)
@pytest.mark.parametrize("when", ["before", "after"])
def test_host_comparison_rejects_process_identity_races(images, change, when):
    info, root, installed = images
    changed = replace(info, **change)
    reads = iter([changed] if when == "before" else [info, changed])
    assert (
        clients.process_binary_outdated(
            info, installed, {}, lambda pid: next(reads), root
        )
        is None
    )


@pytest.mark.parametrize("name", ["tmux", "mosh-server", "mosh-native-server"])
def test_reference_uses_each_programs_approved_variant(tmp_path, monkeypatch, name):
    home = tmp_path / "home"
    directory = home / ".local/bin"
    directory.mkdir(parents=True)
    binary = tmp_path / name
    binary.write_bytes(b"\x7fELFthe installed build")
    (directory / name).symlink_to(binary)
    monkeypatch.setitem(clients.FORK_BINARIES, name, binary)
    monkeypatch.setattr(Path, "home", lambda: home)
    assert clients.installed_binary(name) == binary


@pytest.fixture(params=["mosh-server", "mosh-native-server"])
def mosh_releases(tmp_path, monkeypatch, request):
    name = request.param
    prefix = "mosh-release." if name == "mosh-server" else "release."
    build = tmp_path / "repo/build"

    def make_release(label, timestamp, data):
        release = build / (prefix + label)
        binary = release / "src/frontend" / name
        binary.parent.mkdir(parents=True)
        binary.write_bytes(data)
        binary.chmod(0o755)
        os.utime(binary, ns=(timestamp, timestamp))
        (release / "BUILD-SHA256SUMS").write_text(
            f"{hashlib.sha256(data).hexdigest()}  {binary}\n"
        )
        return binary

    installed = make_release("old", 1_000_000_000, b"\x7fELFold installed Mosh build")
    newer = make_release("new", 2_000_000_000, b"\x7fELFnewer Mosh build")
    directory = tmp_path / "home/.local/bin"
    directory.mkdir(parents=True)
    (directory / name).symlink_to(installed)
    monkeypatch.setitem(clients.FORK_BINARIES, name, installed)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    return name, installed, newer, make_release


def test_new_mosh_release_is_reported_without_changing_the_installed_link(
    tmp_path, mosh_releases
):
    name, installed, newer, _ = mosh_releases
    info = clients.Process(424242, 1, 1234, name, os.getuid(), "S")
    root = tmp_path / "proc"
    directory = root / str(info.pid)
    directory.mkdir(parents=True)
    os.link(installed, directory / "exe")
    fingerprints = {}
    assert (
        clients.process_binary_outdated(
            info, installed, fingerprints, lambda pid: info, root
        )
        is False
    )
    reference = clients.latest_binary(name, fingerprints)
    assert reference == newer
    assert (
        clients.process_binary_outdated(
            info, reference, fingerprints, lambda pid: info, root
        )
        is True
    )
    assert clients.installed_binary(name) == installed


def test_mosh_reference_uses_the_newest_release_not_directory_order(mosh_releases):
    name, installed, _, make_release = mosh_releases
    newest = make_release("newest", 4_000_000_000, b"\x7fELFnewest build")
    make_release("older", 500_000_000, b"\x7fELFprevious build")
    assert clients.latest_binary(name, {}) == newest
    assert clients.installed_binary(name) == installed


@pytest.mark.parametrize(
    "invalid", ["unfinished", "script", "not_executable", "too_big"]
)
def test_unfinished_or_invalid_mosh_releases_are_not_references(
    mosh_releases, monkeypatch, invalid
):
    name, installed, newer, _ = mosh_releases
    if invalid == "unfinished":
        (newer.parents[2] / "BUILD-SHA256SUMS").unlink()
    elif invalid == "script":
        newer.write_bytes(b"#!/bin/sh\nexit 1\n")
    elif invalid == "not_executable":
        newer.chmod(0o644)
    else:
        monkeypatch.setattr(clients, "MAX_BINARY_BYTES", 8)
    assert clients.latest_binary(name, {}) == installed


def test_standard_and_native_mosh_never_share_a_build_reference(mosh_releases):
    name, installed, newer, _ = mosh_releases
    other_name = "mosh-native-server" if name == "mosh-server" else "mosh-server"
    (newer.parents[2] / "BUILD-SHA256SUMS").unlink()
    other = newer.with_name(other_name)
    newer.rename(other)
    (other.parents[2] / "BUILD-SHA256SUMS").write_text(
        f"{hashlib.sha256(other.read_bytes()).hexdigest()}  {other}\n"
    )
    assert clients.latest_binary(name, {}) == installed


def test_mosh_development_build_is_not_used_as_a_release_reference(mosh_releases):
    name, installed, newer, _ = mosh_releases
    newer.parents[2].rename(newer.parents[3] / "development")
    assert clients.latest_binary(name, {}) == installed


def test_identical_newer_mosh_copy_does_not_mark_the_running_image_outdated(
    tmp_path, mosh_releases
):
    name, installed, newer, _ = mosh_releases
    newer.write_bytes(installed.read_bytes())
    info = clients.Process(424242, 1, 1234, name, os.getuid(), "S")
    root = tmp_path / "proc"
    directory = root / str(info.pid)
    directory.mkdir(parents=True)
    os.link(installed, directory / "exe")
    fingerprints = {}
    reference = clients.latest_binary(name, fingerprints)
    assert reference == newer
    assert (
        clients.process_binary_outdated(
            info, reference, fingerprints, lambda pid: info, root
        )
        is False
    )


def test_unreadable_mosh_release_directory_falls_back_to_installed(
    mosh_releases, monkeypatch
):
    name, installed, _, _ = mosh_releases

    def inaccessible(path):
        raise PermissionError("unreadable release directory")

    monkeypatch.setattr(Path, "iterdir", inaccessible)
    assert clients.latest_binary(name, {}) == installed


def test_mosh_release_discovery_is_bounded(mosh_releases):
    name, installed, _, _ = mosh_releases
    release = installed.parents[2]
    prefix = "mosh-release." if name == "mosh-server" else "release."
    for index in range(127):
        (release.parent / f"{prefix}unused{index}").mkdir()
    assert clients.latest_binary(name, {}) == installed


def test_xfce_reference_resolves_the_launcher_without_running_it(tmp_path, monkeypatch):
    directory = tmp_path / ".local/bin"
    directory.mkdir(parents=True)
    repo = tmp_path / "repo"
    binary = repo / "build/terminal/xfce4-terminal"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\x7fELFcurrent xfce build")
    launcher = repo / "run-patched-xfce4-terminal.sh"
    launcher.write_text("#!/bin/sh\nexit 1\n")
    (directory / "xfce4-terminal").symlink_to(launcher)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert clients.installed_binary("xfce4-terminal") == binary


def test_reference_can_use_path_but_does_not_guess_unrelated_programs(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(clients.shutil, "which", lambda name: "/path/to/" + name)
    monkeypatch.setattr(Path, "resolve", lambda path, **kwargs: path)
    assert clients.installed_binary("xfce4-terminal") == Path("/path/to/xfce4-terminal")
    assert clients.installed_binary("konsole") is None
