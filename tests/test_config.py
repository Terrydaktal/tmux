import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import ROOT
from terminal_harness import Attachment

CONFIG_REPO = ROOT.parent / "config"


def user_config(backend):
    home = Path(backend.env["HOME"])
    config_home = Path(backend.env.get("XDG_CONFIG_HOME") or home / ".config")
    if not config_home.is_absolute():
        config_home = home / ".config"
    path = config_home / "tmux-simple/tmux.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.mark.parametrize("xdg", ["custom", "unset", "empty", "relative"])
def test_launcher_uses_configured_file_and_ignores_vanilla_config(backend, xdg):
    if xdg == "unset":
        backend.env.pop("XDG_CONFIG_HOME")
    elif xdg == "empty":
        backend.env["XDG_CONFIG_HOME"] = ""
    elif xdg == "relative":
        backend.env["XDG_CONFIG_HOME"] = "not-an-absolute-config-path"
    else:
        backend.env["XDG_CONFIG_HOME"] = str(backend.socket.parent / "config space")
    config = user_config(backend)
    config.write_text(
        (ROOT / "tmux-simple.conf").read_text() + "\nset -g @config-origin user\n"
    )
    (Path(backend.env["HOME"]) / ".tmux.conf").write_text("set -g @legacy-loaded yes\n")
    app = backend.attach()
    try:
        app.until(lambda state: b"READY" in state["screen"])
        assert backend.run("show-options", "-gv", "@config-origin").stdout == b"user\n"
        assert not backend.run(
            "show-options", "-gv", "@legacy-loaded", check=False
        ).stdout
    finally:
        app.close()


@pytest.mark.parametrize("invalid", ["directory", "broken-link"])
def test_invalid_user_config_fails_without_starting_a_server(backend, invalid):
    config = user_config(backend)
    if invalid == "directory":
        config.mkdir()
    else:
        config.symlink_to(config.with_name("missing.conf"))
    app = backend.attach()
    try:
        app.until(lambda state: app.exited)
        assert b"configuration is missing or not a regular file" in app.wire
        assert not backend.socket.exists()
        assert not backend.log.exists()
    finally:
        app.close()


def test_existing_server_does_not_reload_or_require_the_moved_config(backend):
    config = user_config(backend)
    config.write_text(
        (ROOT / "tmux-simple.conf").read_text() + "\nset -g @config-origin original\n"
    )
    first = backend.attach()
    second = None
    try:
        first.until(lambda state: b"READY" in state["screen"])
        config.unlink()
        config.symlink_to(config.with_name("missing.conf"))
        second = Attachment(
            backend,
            argv=backend.cli("attach", "second", "--", *backend.program()),
        )
        second.until(lambda state: b"READY" in state["screen"])
        assert (
            backend.run("show-options", "-gv", "@config-origin").stdout == b"original\n"
        )
        assert backend.run("show-options", "-gv", "mouse").stdout == b"on\n"
    finally:
        first.close()
        if second:
            second.close()


@pytest.fixture
def config_installer(backend):
    repo = backend.socket.parent / "config repo"
    directory = repo / "tmux-simple"
    directory.mkdir(parents=True)
    shutil.copy2(CONFIG_REPO / "tmux-simple/link.sh", directory / "link.sh")
    shutil.copy2(CONFIG_REPO / "tmux-simple/tmux.conf", directory / "tmux.conf")

    def install():
        return subprocess.run(
            ["bash", str(directory / "link.sh")],
            env=backend.env,
            capture_output=True,
            timeout=5,
        )

    return repo, install


@pytest.mark.parametrize(
    "legacy", ["managed", "archived", "foreign", "regular", "absent"]
)
def test_config_installer_retires_only_managed_legacy_links(
    backend, config_installer, legacy
):
    repo, install = config_installer
    old = Path(backend.env["HOME"]) / ".tmux.conf"
    if legacy == "managed":
        old.symlink_to(repo / "tmux/tmux.conf")
    elif legacy == "archived":
        old.symlink_to(repo / "legacy/tmux/tmux.conf")
    elif legacy == "foreign":
        old.symlink_to(old.with_name("unrelated.conf"))
    elif legacy == "regular":
        old.write_text("keep my old config\n")
    canonical = repo / "tmux-simple/tmux.conf"
    before = canonical.read_bytes()
    for _ in range(2):
        result = install()
        assert result.returncode == 0, result.stderr
        assert user_config(backend).resolve() == canonical
        assert canonical.read_bytes() == before
    if legacy == "foreign":
        assert old.is_symlink()
        assert old.readlink() == old.with_name("unrelated.conf")
    elif legacy == "regular":
        assert old.read_text() == "keep my old config\n"
    else:
        assert not old.exists() and not old.is_symlink()


@pytest.mark.parametrize("conflict", ["file", "symlink", "broken-link"])
def test_config_installer_refuses_conflicts_before_removing_old_link(
    backend, config_installer, conflict
):
    repo, install = config_installer
    config = user_config(backend)
    foreign = config.with_name("other.conf")
    if conflict == "file":
        config.write_text("do not replace\n")
    else:
        config.symlink_to(foreign)
        if conflict == "symlink":
            foreign.write_text("do not replace\n")
    old = Path(backend.env["HOME"]) / ".tmux.conf"
    old.symlink_to(repo / "tmux/tmux.conf")
    canonical = repo / "tmux-simple/tmux.conf"
    before = canonical.read_bytes()
    result = install()
    assert result.returncode != 0
    assert b"Refusing to replace" in result.stderr
    assert old.is_symlink()
    assert canonical.read_bytes() == before
    if conflict == "broken-link":
        assert config.is_symlink() and config.readlink() == foreign
    else:
        assert config.read_text() == "do not replace\n"


def test_bootstrap_installs_simple_not_vanilla_config():
    bootstrap = (CONFIG_REPO / "bootstrap.sh").read_text()
    assert 'bash "$REPO_DIR/tmux-simple/link.sh"' in bootstrap
    assert 'migrate_and_link "$HOME/.tmux.conf"' not in bootstrap
