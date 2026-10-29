# tmux

An upstream-based tmux fork. Each improvement is a complete native feature or
fix, with its own documentation and executable regression coverage under `docs/`
and `verification/`. The baseline is upstream tmux 3.7c, not a custom launcher.

## Project Structure

```text
tmux/
  *.c, *.h, tmux.1        Upstream native source and manual, at their original paths
  autogen.sh              Upstream Autotools bootstrap
  configure.ac            Upstream configuration with fork build-version input
  Makefile.am             Upstream make definitions
  compat/, regress/       Upstream portability source and regressions
  tools/, .github/        Upstream tools and project metadata
  scripts/                Native build, safe symlink installation and integration
  tests/                  Local PTY, VTE, Termux, CLI and loopback tests
  docs/                   Complete behavior and verification notes for each feature
  verification.json       Inventory format and evidence policy
  verification/           Per-feature behavior contracts and test references
  tmux-simple.conf        Pointer to the separately maintained canonical config
  pyproject.toml          uv test dependencies and tooling
  uv.lock                 Locked test dependencies
  build/                  Ignored source snapshots, binaries and build metadata
  artifacts/              Ignored per-run verification and diagnostic evidence
  patches/                Ignored one-patch-per-commit exports
```

Upstream files retain their original repository-root paths. Additional scripts,
tests and documentation coexist with the upstream layout; there is no source
relocation step. Generated parser C, Autotools support files, binaries, caches
and per-run reports are not tracked source.

## Build And Install

The build requires a C compiler, make, autoconf, automake, pkg-config, yacc/Bison,
and ncurses/libevent development files. Run from the repository root:

```sh
scripts/build.sh
scripts/link.sh
```

`scripts/source-snapshot.sh DESTINATION` snapshots current tracked/new source and
writes file/checksum manifests. Git-free exports must contain `SOURCE-FILES`.
`scripts/build.sh` takes that snapshot, bootstraps Autotools, configures and
compiles it, and publishes a symlink only after success. Input is the maintained
source; output is `build/release.XXXXXX/tmux` and its logs/checksums.
`BUILD_RUNTIME=/absolute/directory` selects a private publication directory.
`scripts/link.sh` symlinks `~/.local/bin/tmux` and this fork's clipboard helpers;
it preflights owned destinations and refuses foreign files or links.
`scripts/clipboard.sh` receives selected text on stdin and uses wl-copy or xclip
only when an explicit display is available. It does not guess displays.
Additional feature scripts are documented with their feature under `docs/`.

The command invokes the compiled tmux executable directly. No Python launcher,
custom command grammar, package removal or session migration is involved.
Building and installing do not replace a running server or stop its programs.

## Native Commands

```sh
tmux new-session -A -s work fish -l
tmux attach-session -t work
tmux list-sessions
tmux list-clients
tmux detach-client -s work
tmux kill-session -t work
```

`-A` creates or attaches without replacing the existing program. Closing an
attachment leaves its program running; killing a session deliberately ends it.
Use native `-S /absolute/path/server.sock` or `-L NAME` to select another server.
One program has one terminal size; its viewers cannot have independent layouts.

## Configuration And Transports

Native tmux reads `~/.tmux.conf`; `-f FILE` selects another configuration when
starting a server. Configuration installation is separate from building tmux.
The local `tmux-simple.conf` symlink points at
`~/Dev/config/tmux-simple/tmux.conf`; keep it valid when using this setup.
Install that repository's configuration with its own `link.sh`. A generic user
may instead configure native tmux normally; feature notes document required
options and bindings. Never overwrite an unrelated user configuration.

SSH and Mosh can run the same native attachment command, for example:

```sh
ssh -t HOST 'tmux -S /path/server.sock attach-session -t work'
mosh HOST -- tmux -S /path/server.sock attach-session -t work
```

Mosh transport, phone builds, connection inventory and cleanup belong to their
separate projects. The optional `tmux-mosh` application is an integration/test
dependency, not a launcher implemented by this repository.

## Verification

Use uv with the locked development dependencies. Tests also need Java, Xvfb,
and system Python with GTK3/VTE; desktop integration uses a private D-Bus/X11
display and the separately built XFCE terminal fork.

```sh
uv sync --group dev
uv run python tests/oracles/prepare_termux.py
uv run pytest -q
shellcheck scripts/*.sh
shfmt -d scripts/*.sh
```

The emulator preparation script fetches immutable upstream Termux sources and
compiles the local Java test host. It writes provenance and classes under
`build/termux/`; it does not install or change an Android application.
Tests use private HOME/XDG directories, sockets, loopback transports and synthetic
applications. They must not attach to real sessions, query a live VM or use the
desktop clipboard. Optional real-application checks use disposable saved data.
`TMUX_MOSH_ROOT`, `TEST_XFCE_TERMINAL`, `TEST_XFCE_LINKS` and `TEST_CODEX` select
explicit integration dependencies when their default paths are unavailable.
See the feature inventories for actual coverage and manual device limitations.

Verification contracts describe behavior, not an asserted permanent pass.
Per-run reports, hashes, old failures and diagnostic captures stay in ignored
`artifacts/`; they are not published as current product state.

## Limits And Security

PTY/session persistence does not survive reboot or user-process cleanup.
Clipboard writes require terminal support and policy permission; reads and
general terminal passthrough are not enabled by clipboard fixes. Local desktop
file opening remains the terminal's responsibility. Keep secrets out of command
lines, logs and public verification records. Source changes require a deliberate
server restart to affect existing sessions; reattachment is not a live upgrade.

## Patch Series

The tracked source is authoritative. Export one mail patch per logical commit
from the upstream baseline into a fresh ignored directory:

```sh
mkdir -p patches
output=$(mktemp -d patches/series.XXXXXX)
git format-patch --full-index --binary --output-directory "$output" e476c1230b958df0cb12977517d24b3dc931375b..HEAD
```

Apply the ordered patches with `git am` on that upstream baseline. Patch exports
are not copied back into tracked source or replayed by the build. Version
provenance anchors to the stable upstream commit, so different committer dates
or regenerated fork commit IDs do not invalidate the exported series.
