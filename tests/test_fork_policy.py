"""A distro command or changed PATH cannot redefine the approved fork build."""

from pathlib import Path

import pytest

import tmux_clients as clients


@pytest.mark.parametrize("name", ["tmux", "mosh-server", "mosh-native-server"])
def test_reference_ignores_distro_or_unrelated_installed_commands(
    tmp_path, monkeypatch, name
):
    directory = tmp_path / "home/.local/bin"
    directory.mkdir(parents=True)
    distro = tmp_path / "distro" / name
    distro.parent.mkdir()
    distro.write_bytes(b"\x7fELFnot our fork")
    (directory / name).symlink_to(distro)
    fork = tmp_path / "approved" / name
    fork.parent.mkdir()
    fork.write_bytes(b"\x7fELFour approved fork")
    monkeypatch.setattr(clients, "FORK_BINARIES", {name: fork}, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(clients.shutil, "which", lambda command: str(distro))
    assert clients.installed_binary(name) == fork


@pytest.mark.parametrize("name", ["tmux", "mosh-server", "mosh-native-server"])
def test_missing_fork_never_falls_back_to_a_distro_reference(
    tmp_path, monkeypatch, name
):
    directory = tmp_path / "home/.local/bin"
    directory.mkdir(parents=True)
    distro = tmp_path / name
    distro.write_bytes(b"\x7fELFnot our fork")
    (directory / name).symlink_to(distro)
    monkeypatch.setattr(
        clients, "FORK_BINARIES", {name: tmp_path / "missing-fork"}, raising=False
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(clients.shutil, "which", lambda command: str(distro))
    assert clients.installed_binary(name) is None


@pytest.mark.parametrize("name", ["tmux", "mosh-server", "mosh-native-server"])
def test_plain_path_cannot_supply_a_fork_reference(tmp_path, monkeypatch, name):
    distro = tmp_path / name
    distro.write_bytes(b"\x7fELFdistro build")
    monkeypatch.setattr(
        clients, "FORK_BINARIES", {name: tmp_path / "missing-fork"}, raising=False
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(clients.shutil, "which", lambda command: str(distro))
    assert clients.installed_binary(name) is None
