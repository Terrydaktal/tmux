# Identify source revisions and individual builds

Each new first-parent source commit advances the source revision. Rebuilding
unchanged source leaves that revision stable and reserves a fresh numeric build
ID, starting at zero. Matching source exports retain matching source fingerprints
even when host and phone have different local build numbers.

scripts/source-version.sh reports source provenance; scripts/build-version.sh
reserves a number using a locked counter and writes BUILD-ID/BUILD-VERSION only
inside the build snapshot. scripts/bump-version.sh [patch|minor|major] explicitly
advances the manual history-relative base. The public binary label has the form
3.7c-fork0.2.3-0, with no Git hash or dirty suffix. Diagnostic metadata retains
the full source commit and whether an export included uncommitted edits.

FORK-BASE identifies the stable upstream 3.7c commit, not a fork-generated commit
ID. The anchor counts as revision one. FORK-VERSION is an offset, not the current
HEAD label. This reconstructed history uses 3.7c-fork0.1.6 so the anchor plus its
six logical commits retain the previously established 3.7c-fork0.2.3 source
revision. Normal commits advance automatically; ordinary builds do not edit
either tracked base file. Mail-patch replay may change fork commit IDs and still
works against the same upstream anchor. Intentional history rewrites need an
explicit offset audit, not a silent public-version rollback.

## Verification

```sh
uv run pytest -q tests/test_build_identity.py tests/test_fork_version.py tests/test_source_build.py
```

Tests use disposable sockets, processes, HOME directories and emulator/GUI displays.
