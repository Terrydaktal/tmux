# tmux-simple

An opt-in trial of **stock Mosh + minimally patched tmux**, alongside
`tmux-native` and `mosh-native`. It does not replace them, read `~/.tmux.conf`,
change XFCE/Termux settings, or migrate running programs.

## Try It

In a fresh desktop terminal, outside another multiplexer:

```sh
tmux-simple attach simple-test -- fish -l
```

Start your application there. Attach to the same session from local Termux with
the **ordinary** Mosh executable, not `mosh-native`:

```sh
"$PREFIX/bin/mosh" \
  --ssh="ssh -p 22 -i $HOME/storage/downloads/Telegram/client_lewis_key" \
  --server=/usr/bin/mosh-server \
  lewis@100.74.187.127 -- \
  /home/lewis/.local/bin/tmux-simple attach simple-test --existing
```

No phone build or installation is needed. Leave out `--no-init`; the ordinary
alternate-screen and mouse-reporting behaviour is intentional here. SSH also
works: `ssh -t YOUR_HOST 'tmux-simple attach simple-test --existing'`.

The desktop stays attached. Both viewers control the same application. Its size
follows the viewer that most recently interacted or resized (`window-size latest`),
so a tall phone is not capped by a shorter desktop window when its keyboard closes.
The inactive viewer can show padding or a cropped viewport; one shared application
cannot have two independent terminal sizes. **History browsing/selection is shared
by the pane**, so scrolling on one viewer also affects the other. They do not have
independent history viewports. No independent scrolling is promised by this trial.

## Controls

- No tmux status bar, prefix, default key tables, or `[position/history]` banner.
- At a shell or non-mouse-aware application, wheel/Termux swipe scrolls tmux's
  retained history, five rows per wheel event. Scrolling back down returns live.
- Typing, ordinary arrows, Escape, Ctrl+C, Ctrl+B, and bracketed paste leave
  history automatically and go to the running program. No `f` jump prompt.
- Shift+PageUp/Shift+PageDown explicitly browse history if the outer terminal
  passes those keys. Mouse-aware applications keep their own clicks and wheel.
- Drag selects using tmux, with a neutral reversed-text highlight. Release
  copies without clearing the selection, exiting history, or jumping down.
- Double-click selects a word; triple-click selects a line, also without jumping.
- The terminal's selection override (usually Shift on desktop) still permits
  native selection/menu behaviour. This is not literally native mouse selection.

Copy sends OSC 52 to the selecting terminal, which must support and permit it.
For the host desktop, `clipboard.sh` also uses `wl-copy` or `xclip` when the
corresponding display environment is available. Start or attach from the desktop
at least once to provide that environment. A phone attachment does not unset it.
The helper does not guess a display or change clipboard permissions. Actual phone
touch/clipboard UI behaviour remains a manual check.

### Reliable Clipboard Delivery

The clipboard fix is isolated in `patches/0002-reliable-clipboard-delivery.patch`,
after the `scroll-on-input` option introduced by patch 0001. It changes copy-pipe
delivery, not the renderer or Mosh protocol.

Before the fix:

1. Scroll into retained history, drag a selection, and release the mouse.
2. The binding runs `copy-pipe-no-clear`, keeping the selection and viewport.
3. tmux correctly stores the selected text in its internal paste buffer, but
   the terminal clipboard can stay empty or retain its previous contents.
4. Clipboard output goes through `screen_write_setselection` and `tty_write`.
   Their normal screen-update filters skip clients with a pending window redraw
   or frozen output and panes marked for redraw/drop. Screen repaint can recover
   omitted drawing operations, but does not replay a skipped clipboard change.
5. Independently, the upstream copy path uses an empty OSC 52 selection target.
   Stock Mosh 1.4.0 recognizes the explicit `52;c;` clipboard form.

The fix keeps the normal paste-buffer and external copy-command handling, but
suppresses that path's clipboard output when direct delivery is enabled. It
extracts the selection and calls `tty_set_selection` on the selecting client's
TTY with the explicit `c` target. This bypasses screen-redraw filtering without
forcing a redraw, cancelling copy mode, clearing the selection, or broadcasting
the clipboard request to every attached viewer. The normal terminal-started and
clipboard-capability checks still apply, as do `set-clipboard` and `-C` opt-outs.
Other sessions retain the old path unless `scroll-on-input` is enabled.

Configuration already chooses the no-clear binding and enables clipboard support.
Changing those settings does not remove the internal screen-write guards. A
terminfo override can fix the Mosh selector, not a skipped write. Cancelling copy
mode would violate the requirement to stay scrolled up, and sleeps/redraw timing
tricks do not guarantee delivery. A host-side `wl-copy` or `xclip` command does not
update the phone's clipboard.

This does not prove that a source patch is the only possible solution. Stock
`load-buffer -w` and `set-buffer -w` also use direct clipboard delivery and accept
a target client. They could support an alternative configured delivery workflow,
with explicit client targeting and a compatible Mosh clipboard selector. Such a
workflow was not validated here. This patch instead repairs the existing copy
operation synchronously, using its exact selection and initiating client without
an extra helper process or later lookup of the shared paste buffer.

The regression checks the actual terminal clipboard against tmux's buffer while
asserting unchanged history position and viewport. Stock-Mosh tests also exercise
copying with both viewers attached. OSC 52 still requires terminal support and
permission; this patch does not bypass clipboard policy.

### Desktop File Links

The local XFCE4 Terminal fork keeps Ctrl+click to open a file and
Ctrl+Shift+click to open its parent directory with the file selected. The
`hyperlinks` terminal feature preserves OSC 8 targets in live output, retained
history and new attachments. The launcher also advertises this capability for
each xterm-style attachment, so an older running server needs no restart or
configuration reload.

Plain detected paths need the terminal fork's tmux-aware path resolver. On a
click it queries the attached client's explicit local socket, matches that
client to its active pane, and reads the pane's foreground application and CWD.
The application allowlist still applies. It never treats the tmux client's
startup directory as the pane's current directory. Queries time out after
250 ms, do not start servers, and do not run on output or mouse movement.

After rebuilding the terminal fork, open a new terminal window and run
`tmux-simple attach NAME --existing`. Do not kill or recreate the session.
These desktop actions are local; this does not add remote file opening or
hyperlink transport to stock Mosh.

## Session Commands

```sh
tmux-simple list
tmux-simple clients
tmux-simple attach simple-test
tmux-simple attach simple-test --existing
tmux-simple detach simple-test
tmux-simple kill simple-test --yes
```

`attach` creates a missing session, using `$SHELL -l` if no program follows `--`.
`--existing` refuses creation. Supplying a program for an existing session is an
error rather than a restart. Closing the attachment leaves the program running;
`exit` in the session's shell ends it. `detach` disconnects all viewers without
stopping the program. `kill --yes` deliberately ends the session and its program.
Persistence does not survive reboot or a policy killing user processes on logout.

The familiar create-or-attach spelling also works through either launcher name:

```sh
tmux new-session -A -s diet
tmux new -As diet
tmux new-session -A -s diet fish -l
```

`new-session` (alias `new`) requires `-s NAME`; `-A` creates the session if missing
or attaches without disconnecting other viewers. Without `-A`, an existing name
is an error. A program may follow the options directly or after `--`; with `-A`
it is used only when creating a session and ignored on reattachment, as in
vanilla tmux. The existing program is never restarted or replaced. Other native
tmux commands and options are not implicitly forwarded to the backend. As in
native `new-session`, a single command argument is interpreted by the shell;
multiple command arguments are passed as an argument vector without shell
interpretation. The simplified `attach` command retains its literal-argument
behavior.

The default socket is `$XDG_RUNTIME_DIR/tmux-simple-UID/server.sock`, or
`/tmp/tmux-simple-UID/server.sock` without that variable. `--socket PATH` selects
an isolated backend, with a user-owned mode-0700 parent; `--tmux PATH` selects a
separately built `tmux 3.7c-simple1`. An unmarked server is refused, not modified.

### Mosh Client Sizing

`tmux clients` lists each attached session/client with its transport, Mosh
connection status, peer, terminal dimensions and sizing flag. `--json` provides
the same inventory for scripts. Both `clients` and `list` ensure automatic
sizing is running on an existing server; neither creates a missing server or
session. No opt-in flag is needed. The old `--manage-sizing` spelling remains
accepted for compatibility.

```sh
tmux clients
tmux clients --json
tmux list
```

New attachments automatically start one monitor per private server if a Mosh
ancestor or an existing Mosh client is detected. For already attached sessions,
ordinary `tmux clients` or `tmux list` also starts it, without restarting or
detaching anyone. Repeated commands reuse the same monitor. If startup fails,
the command still displays its inventory and prints a warning on stderr.
The monitor reads client/process metadata and stock Mosh's login records every
two seconds; it does not capture packets, add probes, or modify Mosh. No phone
update or root access is needed. It exits when its server disappears. With no
Mosh clients, it checks only the client inventory every five seconds and does
not run `who`; remaining alive avoids racing a new phone attachment.

When Mosh explicitly marks a client unreachable, the monitor sets that client's
`ignore-size` flag. With another eligible client attached, the desktop expands
without ending the phone connection or the application. It clears its own flag
when the phone reconnects. Existing manual `ignore-size` flags and unrelated
client flags are preserved. Ownership is retained in private tmux user options
so a replacement monitor can recover after a crash. The monitor preserves the
configured size policy (`window-size latest` by default). Changing the private
ownership option also triggers size recalculation on the pinned backend.

Stock Mosh normally marks a lost peer after about 30 seconds, followed by up to
one monitor polling interval. This is network reachability, not keyboard idle
time or Android foreground/background state. A backgrounded Termux still sending
Mosh traffic remains connected. Missing/ambiguous login records show `UNKNOWN`,
not `UNREACHABLE`; they do not cause new exclusions. If records become unavailable,
previous automatic flags are cleared when the client identity can be verified.
This requires Linux `/proc`, GNU `who`, and Mosh with login-record support.

The monitor uses a mode-0600 `.sizing.lock` and `.sizing.log` beside the private
server socket. Unsafe paths are refused. The log records startup/runtime
failures; it is bounded at startup. No broad process scans, global tmux config,
clipboard, terminal preferences or existing Mosh transports are changed.

## Structure And Build

```text
tmux-simple                     Python standard-library launcher
tmux_clients.py                 Client inventory and automatic Mosh sizing monitor
tmux-simple.conf                Symlink to ../config/tmux-simple/tmux.conf
patches/0001-scroll-on-input.patch  Non-modal input option and patched version tag
patches/0002-reliable-clipboard-delivery.patch  Selecting-client clipboard fix
scripts/build.sh                Verify archive, apply patches, compile, link runtime
scripts/link.sh                 Link tmux-simple, optionally tmux; refuse conflicts
scripts/clipboard.sh            Selected text on stdin -> available desktop clipboard
tests/workload.py               Synthetic terminal output; owned input/resize log
tests/test_cli.py               CLI, lifecycle, installation, isolation checks
tests/test_interaction.py       Input, selection, stock-Mosh integration checks
tests/test_mosh_sizing.py       Client status, offline sizing and lifecycle checks
tests/test_sizing.py            Active viewer, keyboard and portrait-height regressions
tests/conftest.py               Isolated HOME/socket/PTY and loopback fixtures
tests/oracles/vte.py            Real VTE observer under Xvfb, including alternate screen
verification.json               Behaviour inventory, evidence and manual gaps
build/                          Downloaded sources, release binaries and build logs
artifacts/                      Local verification/diagnostic artifacts (not committed)
```

Run in this order:

```sh
scripts/build.sh
scripts/link.sh
```

Build inputs are the pinned tmux 3.7c archive and the numbered patches, applied in
filename order. Requires a compiler,
make, autoconf, automake, pkg-config, ncurses/libevent development files, curl,
tar and patch. Output is a fresh `build/release.XXXXXX/tmux` and SHA-256 manifest;
only a successful build updates `build/runtime/tmux`. Installation creates a
symlink, never overwrites system tmux/Mosh or removes an older build. Runtime
requires Bash, Python 3.12+ standard library and the built tmux binary.

To make `tmux` invoke this simplified launcher as well, run
`scripts/link.sh --as-tmux`. Both names are symlinks to the same launcher, and
both destinations are checked for conflicts before either is installed. Keep
`~/.local/bin` before system directories in PATH. This is the simplified CLI
(`tmux attach NAME --existing`, `tmux list`), plus `new-session [-A] -s NAME`
compatibility, not a drop-in implementation of
every vanilla tmux command or option; tools such as `fzf --tmux` that invoke
the native CLI need the explicit backend executable instead. The backend is
`build/runtime/tmux` and is independent of the distribution's tmux package.
The installer does not remove that package, stop servers, or migrate sessions.

### Configuration

The authoritative settings file is `../config/tmux-simple/tmux.conf` in the
config repository. This project's `tmux-simple.conf` is a relative symlink to
that same file, not a second copy. Keep the two repositories beside each other
when using that fallback.

`bash ../config/tmux-simple/link.sh` installs
`$XDG_CONFIG_HOME/tmux-simple/tmux.conf` (normally
`~/.config/tmux-simple/tmux.conf`) as a symlink to the canonical file. The config
repository's `bootstrap.sh` invokes the same helper. Conflicting files or links
are refused, not overwritten; only its old `~/.tmux.conf` link is retired.

When starting a new server, the launcher prefers the user config above and
otherwise follows the project symlink. An empty or relative `XDG_CONFIG_HOME`
uses `~/.config`. A broken user config link is an error, not a silent fallback.
Attaching or creating a session in an existing server neither rereads the file
nor changes that server's settings. No reload or restart is performed during
installation. Vanilla tmux settings are archived in
`../config/legacy/tmux/tmux.conf` and are never loaded by this launcher.

The patches add an opt-in `scroll-on-input` setting and prevent selection-copy
effects being discarded with a pending screen redraw. It sends an explicit `c`
clipboard selector for stock Mosh. The stock tmux renderer, history grid, PTYs,
session lifecycle and Mosh wire protocol are retained. No native-history export,
new transport, custom Mosh client/server, or general byte-stream proxy is used.

## Verification

The local suite reuses the independent terminal and loopback harnesses in the
sibling `tmux-native` and `mosh-native` projects, not their patched executables.
Prepare the pinned headless Termux emulator in `tmux-native` if absent, then link
its `build/termux/classes` into this project's `build/termux/classes`. Tests need
the sibling uv test environment, Java, Xvfb and system Python with GTK3/VTE bindings.

```sh
../tmux-native/.venv/bin/python -m pytest -q tests
shellcheck scripts/*.sh
shfmt -d -i 4 scripts/*.sh
```

Network tests run `/usr/bin/mosh-client` and `/usr/bin/mosh-server` on loopback
only, with synthetic output, loss/duplication/outage simulation, disposable
sessions and isolated HOME/XDG directories. Clipboard tests use emulator state
or a fake clipboard executable. They never read or write the real clipboard,
contact a phone, scan a network, or attach to a real tmux/Codex session.

Sizing tests drive two real private tmux clients with synthetic Mosh login and
process metadata. They verify 100x30 -> 40x16 -> 100x30 -> 40x16 transitions with
both clients and the same application PID preserved, crash/reconnect recovery,
manual flags, unknown records, singleton startup, and server/client identity
guards. A temporary binary utmp fixture is also read by the real GNU `who`;
tests never modify `/run/utmp` to simulate phone connectivity.

The initial release in `build/release.BnU6TU` passed all 33 tests, with no skips, on
2026-09-26. ShellCheck, shfmt and Ruff checks passed. The build emitted seven
const-qualifier warnings in unchanged upstream files, but no errors. Reports,
the stock-tmux typing regression and development failures are retained under
`artifacts/`; `verification.json` records their scope and the release hash.

File-link regressions additionally use the real forked `TerminalWidget` under
Xvfb and a private D-Bus session. Build its `test-terminal-links` target first;
`TEST_XFCE_LINKS` can override the test executable path. Normal application
opens and file-manager selection URIs go to temporary recorders, not the real
desktop. The tests cover encoded file links, plain absolute/relative paths,
allowlist refusal, an unresponsive private server, history and reattachment.
The updated launcher/configuration passed all 39 tests, including six link
cases, with no skips. The fork's existing key, mouse and regex suites also
passed. Reports are `artifacts/hyperlinks-release-final.xml`,
`artifacts/hyperlinks-focused-final.xml` and
`artifacts/hyperlinks-xfce-suite-final.txt`; tested executable hashes are in
`artifacts/hyperlinks-installed.sha256`.

On 2026-09-27, the Mosh sizing update passed all 89 tests with no skips, including
19 focused client-status/sizing tests, using the same release backend. Ruff lint
and formatting checks passed. Reports are
`artifacts/mosh-sizing-release-verified.xml` and
`artifacts/mosh-sizing-lifecycle.xml`; installed file hashes are in
`artifacts/mosh-sizing-installed.sha256`. No live session was reconfigured or
used for these tests. Enable monitoring for an existing server with
`tmux clients` or `tmux list`; future Mosh attachments enable it automatically.

The follow-up default-activation change passed all 92 tests, including 22 focused
sizing cases. `clients`, `clients --json`, and `list` were each tested without
the old opt-in flag; startup failures still allow inventory output. Reports are
`artifacts/default-sizing-release.xml` and `artifacts/default-sizing-focused.xml`;
file hashes are in `artifacts/default-sizing-installed.sha256`.

Active-viewer sizing passed all 95 tests, including direct and stock-Mosh
portrait-phone regressions with a 141x65 desktop and an 86x95 phone. The tests
cover repeated keyboard-size changes during history browsing, switching input
between viewers, and offline/reconnect handling under both `latest` and
`smallest`. Reports are `artifacts/active-sizing-release.xml` and
`artifacts/active-sizing-focused.xml`; hashes are in
`artifacts/active-sizing-installed.sha256`. The original smaller-height failure
is retained in `artifacts/active-sizing-red.xml`.

On 2026-09-30, the clipboard change was separated into patch 0002 for review.
The two patches concatenate to the previous combined patch byte-for-byte. A
fresh private build using the updated build script matched all four patched
source files in the installed release and passed all 95 tests with no skips.
Reports are `artifacts/clipboard-commit-release.xml` and
`artifacts/clipboard-commit-release.txt`; the build log is
`artifacts/clipboard-commit-build.txt`. ShellCheck, shfmt, Bash syntax and Ruff
checks passed. The installed runtime symlink and running sessions were unchanged.

## Limits And Security

This is a trial, not a guarantee of native terminal equivalence. The retained
history is remote and limited to 100,000 rows. Browsing it needs connectivity;
application alternate-screen repaints are not a complete output transcript.
Stock Mosh sends screen state, so a fresh attachment's **native** scrollback may
still contain only one screen: use wheel/swipe to request remote history instead.
See [Mosh's scrollback explanation](https://mosh.org/#faq).

Mouse gestures in real XFCE and the installed Termux app, keyboard animations,
real roaming, long sessions, unusual terminal extensions and large Unicode
selections still need manual validation. The GUI tests observe VTE under Xvfb
and the pinned Termux emulator, not the phone's Android UI.

Clipboard writes are enabled, including application OSC 52, as in the native
trial. Treat untrusted terminal output accordingly. The desktop helper can
update the host clipboard when a selection is made from another attachment;
this is intentional clipboard sharing, not clipboard isolation. See
[tmux's clipboard documentation](https://github.com/tmux/tmux/wiki/Clipboard).

To stop using this trial, leave its attachments and return to the existing
commands. No rollback of global configuration is required. Existing
`tmux-native` sessions remain separate; they are not adopted or killed.
