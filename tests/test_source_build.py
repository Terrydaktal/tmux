"""Source export and publishing with no live sessions or compiler needed."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    return subprocess.run(args, capture_output=True, timeout=15, check=False, **kwargs)


def project(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "scripts/source-snapshot.sh", repo / "scripts/source-snapshot.sh"
    )
    (repo / ".gitignore").write_text("/build/\n")
    assert run("git", "-C", str(repo), "init", "-b", "main").returncode == 0
    return repo


def snapshot(repo, destination):
    return run("bash", str(repo / "scripts/source-snapshot.sh"), str(destination))


def test_snapshot_uses_dirty_and_new_files_preserving_links_not_build_artifacts(
    tmp_path,
):
    repo = project(tmp_path)
    (repo / "tracked.txt").write_text("old source")
    (repo / "deleted.txt").write_text("deleted source")
    assert (
        run("git", "-C", str(repo), "add", "tracked.txt", "deleted.txt").returncode == 0
    )
    (repo / "tracked.txt").write_text("edited source")
    (repo / "deleted.txt").unlink()
    (repo / "new source.txt").write_text("new source")
    (repo / "link").symlink_to("tracked.txt")
    (repo / "broken-link").symlink_to("not-built-yet")
    (repo / "build").mkdir()
    (repo / "build/artifact").write_text("not source")
    destination = tmp_path / "snapshot"
    result = snapshot(repo, destination)
    assert result.returncode == 0, result.stderr
    assert (destination / "tracked.txt").read_text() == "edited source"
    assert (destination / "new source.txt").read_text() == "new source"
    assert (destination / "link").readlink() == Path("tracked.txt")
    assert (destination / "broken-link").is_symlink()
    assert not (destination / "deleted.txt").exists()
    assert not (destination / "build").exists()
    assert not (destination / ".git").exists()
    assert (
        run("sha256sum", "--check", "SOURCE-SHA256SUMS", cwd=destination).returncode
        == 0
    )
    # Phone bundles use the source manifest instead of requiring Git metadata.
    exported = tmp_path / "exported"
    result = snapshot(destination, exported)
    assert result.returncode == 0, result.stderr
    assert (exported / "SOURCE-FILES").read_bytes() == (
        destination / "SOURCE-FILES"
    ).read_bytes()
    assert (exported / "tracked.txt").read_text() == "edited source"


@pytest.mark.parametrize("path", ["../outside", "/etc/passwd", "source/../../outside"])
def test_bundle_manifest_cannot_escape_the_source_root(tmp_path, path):
    repo = tmp_path / "bundle"
    (repo / "scripts").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "scripts/source-snapshot.sh", repo / "scripts/source-snapshot.sh"
    )
    (repo / "SOURCE-FILES").write_bytes(path.encode() + b"\0")
    result = snapshot(repo, tmp_path / "output")
    assert result.returncode != 0
    assert b"Invalid source path" in result.stderr


@pytest.mark.parametrize("failed", [False, True])
def test_builder_uses_current_source_and_publishes_only_after_success(tmp_path, failed):
    repo = project(tmp_path)
    shutil.copyfile(ROOT / "scripts/build.sh", repo / "scripts/build.sh")
    native = repo / "src" if (ROOT / "src/configure.ac").is_file() else repo
    native.mkdir(exist_ok=True)
    (native / "payload").write_text("old source")
    (native / "configure.ac").write_text("fixture")
    assert run("git", "-C", str(repo), "add", ".").returncode == 0
    (native / "payload").write_text("edited source")
    commands = tmp_path / "commands"
    commands.mkdir()

    def executable(name, script):
        target = commands / name
        target.write_text("#!/bin/sh\nset -eu\n" + script)
        target.chmod(0o755)

    executable(
        "autoreconf", "printf '#!/bin/sh\\nexit 0\\n' > configure\nchmod +x configure\n"
    )
    executable("protoc", "exit 0\n")
    executable("pkg-config", "exit 0\n")
    executable("curl", "exit 99\n")
    executable("patch", "exit 99\n")
    executable(
        "make",
        "exit 12\n"
        if failed
        else (
            "mkdir -p scripts src/frontend\n"
            "for file in tmux scripts/mosh-native src/frontend/mosh-native-client "
            'src/frontend/mosh-native-server; do cp payload "$file"; chmod +x "$file"; done\n'
        ),
    )
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    previous = tmp_path / "previous"
    previous.write_text("keep running")
    name = "tmux" if native != repo else "mosh-native-client"
    link = runtime / name
    link.symlink_to(previous)
    env = os.environ.copy()
    env.pop("PREFIX", None)
    env.update(
        PATH=str(commands) + os.pathsep + env["PATH"], BUILD_RUNTIME=str(runtime)
    )
    result = run("bash", str(repo / "scripts/build.sh"), env=env)
    if failed:
        assert result.returncode != 0
        assert link.resolve() == previous
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert link.is_symlink()
        assert link.read_text() == "edited source"
        builds = list((repo / "build").glob("release.*"))
        assert len(builds) == 1
        assert (builds[0] / "SOURCE-SHA256SUMS").is_file()
        assert (builds[0] / "BUILD-SHA256SUMS").is_file()
