"""Real, private Git histories and source exports; never commit in the user's repo."""

import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run(*arguments, **options):
    return subprocess.run(
        arguments, capture_output=True, text=True, timeout=15, check=False, **options
    )


def git(repo, *arguments):
    return run(
        "git",
        "-C",
        str(repo),
        "-c",
        "user.name=Terrydaktal",
        "-c",
        "user.email=9lewis9@gmail.com",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
        *arguments,
        env=os.environ | {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
    )


def script(repo, name, *arguments, counter=None):
    env = os.environ.copy()
    env.pop("BUILD_COUNTER_FILE", None)
    if counter is not None:
        env["BUILD_COUNTER_FILE"] = str(counter)
    return run(
        "bash", str(repo / "scripts" / name), *(str(arg) for arg in arguments), env=env
    )


@pytest.fixture
def repo(tmp_path):
    directory = tmp_path / "repo"
    (directory / "scripts").mkdir(parents=True)
    for name in ("source-version.sh", "build-version.sh", "source-snapshot.sh"):
        shutil.copyfile(ROOT / "scripts" / name, directory / "scripts" / name)
    (directory / "FORK-VERSION").write_text("1.4.0-fork0.0.0\n")
    (directory / "payload.c").write_text("original source\n")
    (directory / ".gitignore").write_text(
        "/build/\n/BUILD-ID\n/BUILD-VERSION\n/SOURCE-VERSION\n/SOURCE-COMMIT\n"
    )
    assert git(directory, "init", "-b", "main").returncode == 0
    assert git(directory, "add", ".").returncode == 0
    assert git(directory, "commit", "-qm", "Initial fixture").returncode == 0
    anchor = git(directory, "rev-parse", "HEAD").stdout
    (directory / "FORK-BASE").write_text(anchor)
    assert git(directory, "add", ".").returncode == 0
    assert git(directory, "commit", "-qm", "Record fixture baseline").returncode == 0
    return directory


def test_patch_advances_on_commits_not_uncommitted_edits(repo):
    first = script(repo, "source-version.sh")
    assert first.returncode == 0, first.stderr
    assert re.fullmatch(r"1\.4\.0-fork0\.0\.2\+g[0-9a-f]{12}\n", first.stdout)
    (repo / "payload.c").write_text("changed source\n")
    dirty = script(repo, "source-version.sh")
    assert dirty.stdout == first.stdout.strip() + ".dirty\n"
    assert git(repo, "add", ".").returncode == 0
    assert git(repo, "commit", "-qm", "Next revision").returncode == 0
    second = script(repo, "source-version.sh")
    commit = git(repo, "rev-parse", "HEAD").stdout.strip()
    assert second.stdout == f"1.4.0-fork0.0.3+g{commit[:12]}\n"


def test_build_numbers_start_at_zero_and_advance_without_dirtying_sources(repo):
    original = git(repo, "status", "--porcelain=v1").stdout
    first = script(repo, "build-version.sh")
    first_id = (repo / "BUILD-ID").read_text()
    second = script(repo, "build-version.sh")
    assert first.returncode == second.returncode == 0
    assert first.stdout == "1.4.0-fork0.0.2-0\n"
    assert second.stdout == "1.4.0-fork0.0.2-1\n"
    assert first.stdout.rsplit("-", 1)[0] == second.stdout.rsplit("-", 1)[0]
    assert "+g" not in second.stdout
    assert first_id == "0\n"
    assert (repo / "BUILD-ID").read_text() == "1\n"
    assert (repo / "build/build-number").read_text() == "2\n"
    assert (repo / "FORK-VERSION").read_text() == "1.4.0-fork0.0.0\n"
    assert git(repo, "status", "--porcelain=v1").stdout == original


def test_export_preserves_commit_and_source_checksums_but_not_local_build_ids(
    repo, tmp_path
):
    source = script(repo, "source-version.sh").stdout
    first, second = tmp_path / "export-one", tmp_path / "export-two"
    result = script(repo, "source-snapshot.sh", first)
    assert result.returncode == 0, result.stderr
    result = script(first, "source-snapshot.sh", second)
    assert result.returncode == 0, result.stderr
    assert (first / "SOURCE-VERSION").read_text() == source
    assert (second / "SOURCE-VERSION").read_text() == source
    commit = git(repo, "rev-parse", "HEAD").stdout
    assert (first / "SOURCE-COMMIT").read_text() == commit
    assert (second / "SOURCE-COMMIT").read_text() == commit
    assert not (first / ".git").exists()
    manifest = (first / "SOURCE-SHA256SUMS").read_bytes()
    assert (second / "SOURCE-SHA256SUMS").read_bytes() == manifest
    counter = repo / "build/build-number"
    one = script(first, "build-version.sh", counter=counter)
    two = script(second, "build-version.sh", counter=counter)
    assert one.returncode == two.returncode == 0
    assert one.stdout != two.stdout
    release = source.strip().split("+", 1)[0]
    assert one.stdout == release + "-0\n"
    assert two.stdout == release + "-1\n"
    assert (first / "SOURCE-SHA256SUMS").read_bytes() == manifest
    assert (second / "SOURCE-SHA256SUMS").read_bytes() == manifest
    assert run("sha256sum", "--check", "SOURCE-SHA256SUMS", cwd=first).returncode == 0


@pytest.mark.parametrize("anchor", ["not-a-commit", "f" * 40])
def test_missing_or_unrelated_history_fails_instead_of_guessing(repo, anchor):
    (repo / "FORK-BASE").write_text(anchor + "\n")
    assert script(repo, "source-version.sh").returncode != 0


@pytest.mark.parametrize("value", [None, "bad\x1b[2J", "1.4.0-fork9.0.0+gaaaaaaaaaaaa"])
def test_bundle_requires_valid_provenance(repo, tmp_path, value):
    exported = tmp_path / "export"
    assert script(repo, "source-snapshot.sh", exported).returncode == 0
    metadata = exported / "SOURCE-VERSION"
    if value is None:
        metadata.unlink()
    else:
        metadata.write_text(value + "\n")
    result = script(exported, "build-version.sh")
    assert result.returncode != 0
    assert not (exported / "BUILD-VERSION").exists()


@pytest.mark.parametrize("commit", [None, "bad", "f" * 40])
def test_bundle_preserves_and_validates_full_commit_metadata(repo, tmp_path, commit):
    exported = tmp_path / "export"
    assert script(repo, "source-snapshot.sh", exported).returncode == 0
    path = exported / "SOURCE-COMMIT"
    if commit is None:
        path.unlink()
    else:
        path.write_text(commit + "\n")
    assert script(exported, "build-version.sh").returncode != 0


def test_dirty_source_provenance_is_kept_out_of_the_public_version(repo):
    (repo / "payload.c").write_text("uncommitted source\n")
    result = script(repo, "build-version.sh")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "1.4.0-fork0.0.2-0\n"
    assert script(repo, "source-version.sh").stdout.endswith(".dirty\n")


def test_concurrent_snapshots_share_an_atomic_counter(repo, tmp_path):
    exports = [tmp_path / f"export-{index}" for index in range(8)]
    for exported in exports:
        assert script(repo, "source-snapshot.sh", exported).returncode == 0
    counter = repo / "build/build-number"
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(
            workers.map(
                lambda exported: script(exported, "build-version.sh", counter=counter),
                exports,
            )
        )
    assert all(result.returncode == 0 for result in results), results
    assert {result.stdout for result in results} == {
        f"1.4.0-fork0.0.2-{index}\n" for index in range(8)
    }
    assert counter.read_text() == "8\n"


def test_separate_build_machines_have_independent_counters(repo, tmp_path):
    first, second = tmp_path / "host", tmp_path / "phone"
    for exported in (first, second):
        assert script(repo, "source-snapshot.sh", exported).returncode == 0
        assert script(exported, "build-version.sh").stdout.endswith("-0\n")


def test_first_fork_commit_counts_and_merged_upstream_history_does_not(repo):
    anchor = (repo / "FORK-BASE").read_text().strip()
    tree = git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()
    upstream = git(repo, "commit-tree", tree, "-p", anchor, "-m", "Upstream change")
    assert upstream.returncode == 0, upstream.stderr
    assert (
        git(
            repo, "merge", "--no-ff", "-m", "Merge upstream", upstream.stdout.strip()
        ).returncode
        == 0
    )
    assert script(repo, "source-version.sh").stdout.startswith("1.4.0-fork0.0.3+g")


def test_release_advances_on_commit_without_resetting_build_number(repo):
    assert script(repo, "build-version.sh").stdout == "1.4.0-fork0.0.2-0\n"
    (repo / "payload.c").write_text("next release source\n")
    assert git(repo, "add", ".").returncode == 0
    assert git(repo, "commit", "-qm", "Next release").returncode == 0
    assert script(repo, "build-version.sh").stdout == "1.4.0-fork0.0.3-1\n"


@pytest.mark.parametrize(
    "base,expected",
    [
        ("0.0.7", "0.0.9"),
        ("0.0.8", "0.1.0"),
        ("0.1.6", "0.1.8"),
        ("0.1.7", "0.1.9"),
        ("0.1.8", "0.2.0"),
        ("0.2.0", "0.2.2"),
        ("0.9.7", "0.9.9"),
        ("0.9.8", "1.0.0"),
        ("9.9.8", "10.0.0"),
    ],
)
def test_commit_count_carries_and_git_free_exports_preserve_it(
    repo, tmp_path, base, expected
):
    (repo / "FORK-VERSION").write_text(f"1.4.0-fork{base}\n")
    result = script(repo, "build-version.sh")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"1.4.0-fork{expected}-0\n"
    exported = tmp_path / "export"
    assert script(repo, "source-snapshot.sh", exported).returncode == 0
    result = script(exported, "build-version.sh")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"1.4.0-fork{expected}-0\n"


@pytest.mark.parametrize("value", ["", "-1", "01", "1\n2", "invalid", "999999999999"])
def test_invalid_or_exhausted_counter_fails_without_reusing_a_number(repo, value):
    counter = repo / "build/build-number"
    counter.parent.mkdir()
    counter.write_text(value + "\n")
    result = script(repo, "build-version.sh")
    assert result.returncode != 0
    assert "Invalid or exhausted build counter" in result.stderr
    assert counter.read_text() == value + "\n"
    assert not (repo / "BUILD-VERSION").exists()



def test_published_anchor_is_the_stable_upstream_commit():
    import json

    manifest = json.loads((ROOT / "verification.json").read_text())
    assert (ROOT / "FORK-BASE").read_text().strip() == manifest["upstream_commit"]


def test_mail_patch_replay_does_not_depend_on_fork_commit_ids(repo, tmp_path):
    parent = git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "payload.c").write_text("complete feature source\n")
    assert git(repo, "add", ".").returncode == 0
    assert git(repo, "commit", "--date=2001-01-01T00:00:00+00:00", "-qm", "Feature fixture").returncode == 0
    original = git(repo, "rev-parse", "HEAD").stdout.strip()
    patch = tmp_path / "feature.patch"
    patch.write_text(git(repo, "format-patch", "-1", "--stdout").stdout)

    replay = tmp_path / "replayed"
    replay.mkdir()
    assert git(replay, "init", "-b", "main").returncode == 0
    assert git(replay, "fetch", "--no-tags", str(repo), parent).returncode == 0
    assert git(replay, "update-ref", "refs/heads/main", parent).returncode == 0
    assert git(replay, "read-tree", parent).returncode == 0
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", parent],
        capture_output=True, check=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(replay)], input=archive, check=True)
    result = git(replay, "am", "--committer-date-is-author-date", str(patch))
    assert result.returncode == 0, result.stderr
    assert git(replay, "rev-parse", "HEAD").stdout.strip() != original
    before = script(repo, "source-version.sh")
    after = script(replay, "source-version.sh")
    assert before.returncode == after.returncode == 0
    assert before.stdout.split("+g")[0] == after.stdout.split("+g")[0]
    assert (replay / "payload.c").read_text() == "complete feature source\n"
    assert git(replay, "symbolic-ref", "--short", "HEAD").stdout == "main\n"
