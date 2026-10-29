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
        ROOT / filename
    ).read_bytes()


def test_native_build_compiles_tracked_source_directly():
    assert not list((ROOT / "patches").glob("*.patch"))
    builder = (ROOT / "scripts/build.sh").read_text()
    assert "source-snapshot.sh" in builder
    assert "build/downloads" not in builder
    assert "patch --" not in builder



@pytest.mark.parametrize("filename", [
    "tmux.c", "input.c", "window-copy.c", "configure.ac", "Makefile.am",
    "autogen.sh", "compat/queue.h", "regress/Makefile",
])
def test_upstream_source_remains_at_the_repository_root(filename):
    assert (ROOT / filename).is_file()
    assert not (ROOT / "src").exists()
