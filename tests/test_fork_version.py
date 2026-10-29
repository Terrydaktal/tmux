"""Source releases advance explicitly; rebuilding or exporting never bumps them."""

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIGURE = Path("FORK-VERSION")


@pytest.fixture
def release(tmp_path):
    script = tmp_path / "scripts/bump-version.sh"
    script.parent.mkdir()
    shutil.copyfile(ROOT / "scripts/bump-version.sh", script)
    configure = tmp_path / CONFIGURE
    configure.parent.mkdir(parents=True, exist_ok=True)

    def run(version, *arguments):
        original = version + "\n"
        configure.write_text(original)
        configure.chmod(0o640)
        result = subprocess.run(
            ["bash", str(script), *arguments],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        return result, configure, original

    return run


@pytest.mark.parametrize("upstream", ["3.7c", "1.4.0"])
@pytest.mark.parametrize(
    "arguments,expected",
    [
        ((), "2.3.5"),
        (("patch",), "2.3.5"),
        (("minor",), "2.4.0"),
        (("major",), "3.0.0"),
    ],
)
def test_bump_preserves_upstream_and_base_file(release, upstream, arguments, expected):
    old = f"{upstream}-fork2.3.4"
    new = f"{upstream}-fork{expected}"
    result, configure, original = release(old, *arguments)
    assert result.returncode == 0, result.stderr
    assert result.stdout == new + "\n"
    assert configure.read_text() == original.replace(old, new)
    assert stat.S_IMODE(configure.stat().st_mode) == 0o640
    assert list(configure.parent.glob("FORK-VERSION.*")) == []


@pytest.mark.parametrize(
    "version,arguments",
    [
        ("3.7c-simple7", ()),
        ("1.4.0-fork01.0.0", ()),
        ("1.4.0-fork0.10.0", ()),
        ("1.4.0-fork0.0.10", ()),
        ("1.4.0-fork0.0.999999999", ()),
        ("1.4.0-fork999999999.9.9", ()),
        ("1.4.0-fork0.0.0", ("bogus",)),
        ("1.4.0-fork0.0.0", ("patch", "extra")),
    ],
)
def test_invalid_bumps_leave_source_unchanged(release, version, arguments):
    result, configure, original = release(version, *arguments)
    assert result.returncode != 0
    assert configure.read_text() == original


def test_sequence_carries_at_ten(release):
    for before, after in [
        ("0.0.0", "0.0.1"),
        ("0.0.9", "0.1.0"),
        ("0.9.9", "1.0.0"),
        ("9.9.9", "10.0.0"),
    ]:
        result, _, _ = release(f"1.4.0-fork{before}")
        assert result.returncode == 0, result.stderr
        assert result.stdout == f"1.4.0-fork{after}\n"


def test_minor_increment_also_carries_at_ten(release):
    result, _, _ = release("1.4.0-fork0.9.8", "minor")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "1.4.0-fork1.0.0\n"
