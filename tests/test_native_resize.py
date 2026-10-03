"""Only copy-mode reading anchors extend the restored native resize behavior."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def before_resize_workaround(tmp_path_factory):
    source = tmp_path_factory.mktemp("native-resize-source")
    subprocess.run(
        [
            "tar",
            "-xzf",
            str(ROOT / "build/downloads/tmux-3.7c.tar.gz"),
            "--strip-components=1",
            "-C",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    for patch in sorted((ROOT / "patches").glob("*.patch")):
        if patch.name.startswith(("0006-", "0007-")):
            continue
        with patch.open("rb") as stream:
            subprocess.run(
                ["patch", "--batch", "--forward", "-d", str(source), "-p1"],
                stdin=stream,
                check=True,
                capture_output=True,
                timeout=10,
            )
    return source


@pytest.mark.parametrize("filename", ["input.c", "tmux.h"])
def test_resize_sources_match_the_pre_workaround_release(
    before_resize_workaround, filename
):
    binary = Path(os.environ.get("TEST_TMUX", ROOT / "build/runtime/tmux"))
    built_source = binary.resolve().parent
    assert (built_source / filename).read_bytes() == (
        before_resize_workaround / filename
    ).read_bytes()


def test_retired_resize_patch_cannot_be_applied_by_future_builds():
    assert not (ROOT / "patches/0006-copy-mode-resize-reflow.patch").exists()
    version_patch = ROOT / "patches/0006-native-resize-behavior.patch"
    assert version_patch.read_text().count("@@") == 2
    assert "+++ b/configure.ac" in version_patch.read_text()


def test_copy_mode_contains_only_the_focused_reading_position_patch(
    before_resize_workaround,
):
    for name in ("0006-native-resize-behavior.patch", "0007-reading-position.patch"):
        with (ROOT / "patches" / name).open("rb") as stream:
            subprocess.run(
                [
                    "patch",
                    "--batch",
                    "--forward",
                    "-d",
                    str(before_resize_workaround),
                    "-p1",
                ],
                stdin=stream,
                check=True,
                capture_output=True,
                timeout=10,
            )
    binary = Path(os.environ.get("TEST_TMUX", ROOT / "build/runtime/tmux"))
    assert (binary.resolve().parent / "window-copy.c").read_bytes() == (
        before_resize_workaround / "window-copy.c"
    ).read_bytes()
    patch = (ROOT / "patches/0007-reading-position.patch").read_text()
    assert {
        line.removeprefix("+++ b/")
        for line in patch.splitlines()
        if line.startswith("+++ b/")
    } == {"window-copy.c", "configure.ac"}
