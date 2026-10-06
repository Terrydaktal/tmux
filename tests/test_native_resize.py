"""The native build uses the maintained source, not a replayed patch series."""

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "filename", ["input.c", "tmux.h", "window-copy.c", "configure.ac", "Makefile.am"]
)
def test_native_build_matches_repository_source(filename):
    binary = Path(os.environ.get("TEST_TMUX", ROOT / "build/runtime/tmux"))
    assert (binary.resolve().parent / filename).read_bytes() == (
        ROOT / "src" / filename
    ).read_bytes()


def test_native_build_no_longer_replays_fork_patches():
    assert not list((ROOT / "patches").glob("*.patch"))
    builder = (ROOT / "scripts/build.sh").read_text()
    assert "source-snapshot.sh" in builder
    assert "build/downloads" not in builder
    assert "patch --" not in builder
