"""Repository relocation must not require moving config or restarting sessions."""

import os
import shutil
import subprocess

import pytest
from conftest import ROOT


@pytest.mark.parametrize("old_alias", [False, True])
def test_installer_resolves_config_and_legacy_repository_alias(tmp_path, old_alias):
    repo = tmp_path / "repos/tmux"
    config = tmp_path / "Dev/config/tmux-simple"
    home = tmp_path / "home"
    (repo / "scripts").mkdir(parents=True)
    (repo / "build/runtime").mkdir(parents=True)
    config.mkdir(parents=True)
    home.mkdir()
    shutil.copyfile(ROOT / "scripts/link.sh", repo / "scripts/link.sh")
    for name in ("clipboard.sh", "attach-environment.sh"):
        (repo / "scripts" / name).write_text("fixture")
    (repo / "build/runtime/tmux").write_text("fixture")
    (config / "tmux.conf").write_text("fixture")
    (config / "integration.conf").write_text("fixture")
    (repo / "tmux-simple.conf").symlink_to("../../Dev/config/tmux-simple/tmux.conf")
    alias = tmp_path / "Dev/tmux-simple"
    alias.symlink_to(repo, target_is_directory=True)
    invocation = alias if old_alias else repo
    for _ in range(2):
        result = subprocess.run(
            ["bash", str(invocation / "scripts/link.sh")],
            env=os.environ | {"HOME": str(home)},
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    assert (home / ".local/bin/tmux").readlink() == repo / "build/runtime/tmux"
    assert not (home / ".local/bin/tmux-mosh").exists()
    assert (home / ".local/libexec/tmux/integration.conf").readlink() == (
        config / "integration.conf"
    )
    assert not (tmp_path / "repos/config").exists()
