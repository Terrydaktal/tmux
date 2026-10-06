# tmux

This fork is published as [Terrydaktal/tmux](https://github.com/Terrydaktal/tmux),
with the maintained patch series and integration helpers on `main`.
Existing `tmux-simple` configuration, socket and local directory names are
compatibility paths, not a separate project or a second tmux installation.

**Our compatibility Mosh fork + patched tmux**, with normal tmux commands and simplified
terminal interaction. `tmux` is a symlink directly to the compiled binary;
`tmux-mosh` is the separate Mosh status/sizing helper. There is no Python command
launcher or `tmux-simple` command. The source directory and private socket retain
their historical names so existing sessions keep working.

The host commands `tmux`, `mosh`, `mosh-client` and `mosh-server` use our builds,
not distro binaries. The compatibility Mosh server includes authenticated
last-packet timestamp reporting while retaining the ordinary Mosh wire protocol.
`mosh-native-server` is our separate native-history fork: its client/server protocol
is incompatible with the compatibility transport. Its name keeps these endpoints
distinct and preserves existing phone commands. Neither name means an upstream
binary; our tmux server is simply named `tmux`.

`tmux-mosh clients` compares running executable hashes against our release links,
never a distro executable discovered through PATH. Mosh has a red background
unless its running image is verified as the latest release of its approved fork
variant. Unavailable verification is also red, not a claim that a build is current.
Updating links does not upgrade existing servers or the phone's client.

## Usage

In a fresh desktop Fish terminal, outside another multiplexer:

```sh
tmux new-session -A -s diet fish -l
```

Start your application there. Attach from Termux with ordinary Mosh, not
`mosh-native`:

```sh
"$PREFIX/bin/mosh" \
  --ssh="ssh -p 22 -i $HOME/storage/downloads/Telegram/client_lewis_key" \
  --server=/home/lewis/.local/bin/mosh-server \
  lewis@100.74.187.127 -- \
  /home/lewis/.local/bin/tmux \
  -S /run/user/1000/tmux-simple-1000/server.sock attach-session -t diet
```

No phone build/install is needed. Leave out `--no-init`; the ordinary alternate
screen and mouse reporting are intentional. SSH works too:
`ssh -t YOUR_HOST 'tmux attach-session -t diet'` with the installed Fish function.
For another login shell, pass the same explicit `-S` as above. Replace
`attach-session -t diet` with `new-session -A -s diet` to create if missing; the
private socket directory must already exist when bypassing the Fish function.

Both viewers control the same application. Its size follows the viewer that last
interacted or resized (`window-size latest`). The other viewer may show padding
or a cropped viewport: one application cannot have two independent terminal
sizes. History browsing/selection is shared by the pane, not independent per
viewer. Closing an attachment leaves the program running. Persistence does not
survive reboot or a policy killing user processes on logout.

## Controls

- No tmux status bar, prefix, default key tables, or `[position/history]` banner.
- At a shell or non-mouse-aware application, wheel/Termux swipe scrolls tmux's
  retained history, five rows per wheel event. Scrolling back down returns live.
- Typing, ordinary arrows, Escape, Ctrl+B, and bracketed paste leave history
  automatically and go to the running program. Ctrl+C does the same when there
  is no selection; with selected text, it copies instead. No `f` jump prompt.
- Shift+PageUp/Shift+PageDown explicitly browse history if the outer terminal
  passes those keys. Mouse-aware applications keep their own clicks and wheel.
- Drag selects using tmux, with a neutral reversed-text highlight. Release only
  stops selecting: it does not copy, clear the selection, or jump down.
- Ctrl+drag selects a rectangular box. Ordinary dragging switches back to linear
  selection. Mouse-aware applications retain their own Ctrl+drag gestures.
- Double-click selects a word; triple-click selects a line. Neither copies.
- Ctrl+C explicitly copies selected text without clearing it or moving the
  viewport. The copy-mode binding works over SSH/Mosh without XFCE integration.
- The terminal's selection override (usually Shift on desktop) still permits
  native selection/menu behaviour. This is not literally native mouse selection.

Copy sends OSC 52 to the selecting terminal, which must support and permit it.
For the host desktop, `clipboard.sh` also uses `wl-copy` or `xclip` when the
corresponding display environment is available. Start or attach from the desktop
at least once to provide that environment. A phone attachment does not unset it.
The helper does not guess a display or change clipboard permissions. Actual phone
touch/clipboard UI behaviour remains a manual check.

### Reliable Clipboard Delivery

The clipboard fix lives in `src/window-copy.c`, alongside the native
`scroll-on-input` integration. It changes copy-pipe
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

## Application Clipboard Writes

The copy-mode fix above does not cover selections drawn by Codex or Pi themselves.
An application started over SSH can lack `DISPLAY` and `WAYLAND_DISPLAY` even after
a desktop attaches. Its native clipboard cannot reach the desktop. Codex's fallback
is a tmux-wrapped OSC 52 write: unrestricted passthrough is disabled, and the installed
VTE does not implement OSC 52 writes even if they reach it. Pi uses ordinary OSC 52.
Neither path used to invoke tmux's desktop `copy-command` helper.

The application clipboard handler in `src/input.c` handles ordinary OSC 52 and exactly one
tmux-wrapped clipboard write when `scroll-on-input` is enabled. It sends explicit
`52;c;` directly to the most recently active, writable viewer of the source pane,
and pipes the same decoded text to `copy-command` using that viewer's session
environment. The attachment hook can therefore restore desktop clipboard access
without changing the already-running application's environment. Helpers are
bounded to eight simultaneous jobs. A helper failure is recorded in tmux debug logs.

`set-clipboard on` is required; `off` and `external` reject application writes.
Wrapped queries, invalid data and combined escape sequences are not forwarded by
this handler. General passthrough remains disabled. No clipboard reads, terminal
library replacement, Codex changes or VM clipboard-read permission are added.
Applications using other tmux clipboard commands retain their existing behavior.

The release identifies itself as `tmux 3.7c-simple7`. A running old server does not
load a new executable by reattaching or reloading config. Test on a fresh server
before deliberately retiring any existing sessions; rebuilding never kills them.
The input handler in `src/server-client.c` keeps Ctrl+C in copy mode when a selection exists; otherwise the
scroll-on-input path would discard it before the configured copy binding ran.
Other keyboard input still resumes the application. Changing the mouse bindings
alone disables automatic copying on old servers, but the SSH/Mosh Ctrl+C binding
needs the new server executable. Reload configuration without restarting programs
with `tmux source-file ~/.tmux.conf`; do not kill a server containing unsaved work.
`tests/test_application_clipboard.py` includes actual XFCE keyboard and X11 clipboard
checks under private Xvfb/D-Bus sessions, as well as policy and multi-viewer checks.
`tests/test_codex_clipboard.py` checks the installed Codex executable with a private
synthetic conversation, mouse selection and Ctrl+C. `TEST_CODEX` selects another
release. It uses an embedded server and a loopback-only model-provider address;
it neither submits a prompt nor loads the user's conversations or credentials.

The terminal process also needs to load its copy-key fix. Opening a new tmux
session inside an old XFCE window upgrades neither the terminal nor the tmux
server. `tmux -V` reports the client binary on disk; to check the actual server,
run `tmux display-message -p '#{version}'`. After saving work in every session,
stop that server with `tmux kill-server`, then create the session from a newly
launched XFCE process. Never stop the shared server merely to test clipboard
changes while other sessions still contain work.

## Rectangular Selection

Ctrl+left-drag enables tmux's rectangular selection at the shell and in non-mouse-aware
applications. Release freezes the box without copying; Ctrl+C copies explicitly.
Ordinary dragging and word/line selection return to linear selection.

The copy implementation in `src/window-copy.c` fixes copying a released box. The original copy code inferred its width
from the cursor and the currently dragged endpoint. Once selection stopped, it
could copy only one column or the wrong columns after moving the cursor. It now
uses the stored corners and includes the highlighted edge cells, in either drag
direction. Both the Ctrl+C binding and XFCE's direct tmux copy command use this
path, without moving the viewport or restarting the application. Existing servers
need a deliberate restart to load the native fix; reloading bindings is not enough.

## Reading Position On Resize

While browsing history, `src/window-copy.c` retains the logical text position at the top
of the visible copy-mode viewport through width and height changes. Consecutive
resizes reuse that position rather than following the copy cursor or drifting
by a wrapped line. Scrolling or navigation establishes a new position. The view
is clamped when the terminal is taller than the available history; shrinking
again restores the retained position. At the live bottom, normal tail behavior
is unchanged. Native Unicode cell packing can change the precise wrap boundary,
but the reading text remains in the top visible row.

This is not the retired copy-mode resize/repaint workaround. Tmux does not
search transcripts for matching text, replace the copy-mode snapshot after an
application repaint, or alter the application parser or live renderer.
Pi and Pi-opsec use their normal renderer without the Pi-only refresh helper.
The retired helper's installer only removes known old hooks; its legacy apply
and check switches no longer install or require that hook.

The last-active-device sizing policy, Mosh-aware client participation, ordinary
SIGWINCH forwarding, clipboard delivery and explicit-copy bindings are unchanged.
Termux or an application's own renderer may move to the bottom during resize;
tmux does not add a workaround for that behavior.

`tests/test_reading_position.py` checks actual Fish, short/wrapped/Unicode history,
resize round trips, tiny/oversized viewports, continued input and multiple viewers
using private VTE/Termux emulators. `tests/test_native_resize.py` verifies that
only the focused copy-mode patch changes the restored sources; the parser and
shared structures still match the release before the retired workaround.
The old workaround and its tests are preserved in ignored rollback artifacts.
Existing servers require a deliberate restart to load this patch; installing
the binary or reloading configuration does not replace a running server.

## Desktop File Links

The local XFCE4 Terminal fork keeps Ctrl+click to open a file and Ctrl+Shift+click
to open its parent directory with the file selected. The `hyperlinks` terminal
feature in `tmux.conf` preserves OSC 8 targets in live output, retained history
and new attachments. No launcher capability flag is needed.

Plain detected paths use the terminal fork's tmux-aware resolver. On a click it
queries the attached client's explicit local socket, matches it to the active
pane, and reads the foreground application and CWD. The application allowlist
still applies. Queries time out after 250 ms, never start servers, and do not run
on output or mouse movement. Use the Fish function or explicit `-S` when
attaching so the resolver can identify the socket. Open a new rebuilt terminal
window and reattach; do not kill or recreate sessions. These actions are local,
not remote file opening or hyperlink transport added to stock Mosh.

## Session Commands

```sh
tmux list-sessions
tmux list-clients
tmux new-session -A -s diet
tmux attach-session -t diet
tmux detach-client -s diet
tmux kill-session -t diet
```

These are native tmux commands. `new-session -A` creates or attaches without
disconnecting other viewers; `attach-session` refuses creation. Programs supplied
with `-A` are used only on creation, not reattachment. `detach-client -s`
disconnects all viewers of that session without stopping it. `kill-session`
deliberately ends it and its programs, with no launcher confirmation prompt.

Aliases such as `tmux new -As diet` work. All native commands/options are
available directly, including `new-window`, `split-window`, control mode, `-f`,
`-S` and `-L`. A single program argument in `new-session` is interpreted by the
shell; multiple program arguments are passed literally as an argument vector.

The Fish function selects `$XDG_RUNTIME_DIR/tmux-simple-UID/server.sock`, or
`/tmp/tmux-simple-UID/server.sock` without an absolute runtime directory. It
creates the mode-0700 directory if absent and refuses unsafe existing paths.
Explicit `-S PATH`, `-L NAME`, or an inherited `TMUX` pass through unchanged.
No program arguments are translated. Outside Fish, the binary uses tmux's
standard default socket unless you pass `-S` explicitly.

## Mosh Status And Sizing

```sh
tmux-mosh clients
tmux-mosh clients --once
tmux-mosh clients --flat
tmux-mosh clients --json
tmux-mosh cleanup --dry-run
tmux-mosh cleanup
tmux-mosh cleanup --include-legacy --dry-run
tmux-mosh cleanup --include-legacy
tmux-mosh ensure
tmux-mosh start
tmux-mosh -S /path/to/server.sock clients
```

`clients` opens a live, collapsible session tree in an interactive terminal,
expanded by default with columns ordered `TYPE`, `SESSION`, `APP`,
`CONNS`, `IDLE`, `PEER`, `SIZE`, `SIZING`.
Expand/collapse markers occupy a separate gutter before `TYPE` on group rows.
Connection rows use the same box-drawing branches as `fsx`
(&#9500;&#9472;&#9472; and &#9492;&#9472;&#9472;), extending from the parent type.
Each expanded group's child list is followed by one blank line; collapsed groups
do not add a spacer. Blank lines are not selectable and do not count as connections.
Viewer names float under the parent type, while their details stay in the aligned
data columns; there is no separate `VIEWER` column. Tmux, VM and standalone-session
groups show their app/type once on the parent, not on every connection.
VM app PIDs identify host attachment launchers and appear beside the parent's
app name; they are not inferred guest-process PIDs.
Each tmux session shows its foreground app and connection count once; expanding
the session reveals its XFCE, Mosh, SSH or other viewers. Sessions with no viewers
remain visible with a zero count. Verified VM session names form separate
`opsec-tmux` groups. Each standalone Mosh/SSH session has its own top-level `direct`
parent showing the app/PID and `CONNS=1`, with one Mosh/SSH child carrying idle,
peer, size and sizing. Other-server connections use the same structure with their
own type. Distinct sessions are never merged by app name or PID, and there is no
`other connections` wrapper. Parents stay visible when their transport children
are collapsed; fold/selection identity survives foreground app changes.
Existing host-tmux viewers into a VM
stay under their actual host session and show `-> opsec-tmux SESSION` in the tree label.

Click a session or press Enter/Space to fold it. Up/Down or the wheel moves the
selection; Left returns to the parent/collapses it, and Right expands/enters it.
`e` expands all, `c` collapses all, `r` refreshes, and `q`/Escape exits. Inventory
refreshes once per second in one background worker; folding and the selected
connection survive refreshes. Resizing fits/clips columns instead of wrapping.
New sessions start expanded; manually collapsed sessions stay collapsed.
The alternate-screen view restores the terminal and disables its mouse reporting
when it exits, including on interruption.

`--once` prints an expanded tree snapshot; piping output also uses that format
without entering an interactive display. `--flat` retains the previous table.
`--json` preserves the existing machine-readable schema and is always one-shot.
All these views are read-only: they neither start a sizing monitor nor create,
attach, detach or terminate a server/session. Connection counts mean tmux
attachments, not proof that a Mosh viewer is currently reachable; check `IDLE`.

`cleanup` is an explicit, one-shot operation: close this user's Mosh servers on
this host after **30 seconds without a received client packet**. It includes
standalone apps and connections to every tmux socket, regardless of `-S`.
`--dry-run` previews the targets; `--older-than SECONDS` changes the positive
integer cutoff, and `--json` returns actions and counts. It never runs as part
of the sizing monitor, inventory, or attachment hooks.

By default, cleanup uses the verified authenticated-packet timestamp, not keyboard idle time
or the coarse `UNREACHABLE` login flag. Old servers with no timestamp, failed
queries, and changed/exited processes are skipped. Before sending SIGTERM it
opens a PID descriptor and rechecks process identity and packet age, so a recycled
PID cannot target another application and a reconnect noticed by the final query
cancels cleanup. Packet receipt and signalling are separate operations: a client
can still reconnect after that final query. No force-kill or PID-only fallback is
used if safe signalling is unavailable.

Use `--include-legacy` to also clean up old servers that lack packet timestamps
but have an unambiguous, terminal-verified **`UNREACHABLE`** Mosh login record.
Stock Mosh sets this coarse flag after about 30 seconds without a client update;
it does not supply an exact packet age. `SOURCE` shows `legacy-login` and `IDLE`
stays `-` rather than inventing a timestamp. Connected, unknown, ambiguous and
newly started legacy servers are kept/skipped. Cleanup rechecks the login flag
and terminal identity after pinning the server's PID; a reconnect already visible
in the login record cancels cleanup. The coarse flag can lag a reconnect, so
this is an explicit opt-in, not equivalent to the exact packet-age guarantee.
This option is valid only with the default 30-second cutoff; older servers cannot
verify arbitrary `--older-than` values. It does not force-kill all old servers.

Tmux servers and their programs are not targeted. **A standalone Mosh shell/app
can terminate when its Mosh session closes.** A temporary signal outage can also
reach the cutoff, so preview before cleaning up. SIGTERM requests shutdown; a
`closing` result does not claim that the server has already exited.

<pre>
      TYPE        SESSION  APP           CONNS  IDLE    PEER            SIZE     SIZING
[-]   tmux        diet     codex (1234)  2
      &#9500;&#9472;&#9472; xfce4-terminal                         -       -               126x41   active
      &#9492;&#9472;&#9472; mosh                                   0s      100.70.36.28    81x85    standby

[-]   opsec-tmux  pi-live  pi-opsec      2
      &#9500;&#9472;&#9472; xfce4-terminal                         -       -               126x41   -
      &#9492;&#9472;&#9472; mosh                                   0s      100.70.36.28    81x85    -

[-]   direct      -        fish (9012)   1
      &#9492;&#9472;&#9472; mosh                                   0s      100.70.36.28    81x85    -

[-]   direct      -        fish (9013)   1
      &#9492;&#9472;&#9472; ssh                                    -       100.70.36.28    126x41   -

</pre>

Column headers are uppercase and all displayed values are lowercase, including
session/application names, types and reachability labels. This affects text
presentation only; JSON preserves actual names and its existing enum values.
Snapshots end at the tree/table: no scope footer, column legend or informational
warnings are appended, including in `--verbose`. The live view has one short
controls line, which also reports a refresh failure without pretending the old
snapshot is fresh. Diagnostic notes remain in the `note` field of `--json` output;
explicit command failures still report errors. The tree's `SESSION` column shows
session names where known and `-` for unnamed direct sessions. Box-drawing
child branches identify each session viewer (`VIA` in the legacy
flat table). Child details remain under their original column headers, without
inline `key=value` annotations; the parent's app is not repeated. `CONNS` shows the
attachment/group count: zero for detached tmux sessions, the attachment total for
tmux/VM groups, and one for each direct-session parent. Children do not repeat it.
App PIDs remain next to app names; commands,
TTYs, frontend PIDs and build dates are not added back to the text view.

- `TYPE`: `tmux` for the selected server, `direct` for a Mosh/SSH shell/app,
  `opsec-tmux` for a host-side foreground viewer into the opsec VM's persistent tmux,
  `other-tmux` for a remote connection to tmux outside that server's inventory,
  or `unknown` when its foreground process cannot be identified.
  VM viewers are recognized from owned foreground SSH processes using the fixed
  opsec route and the guest Pi/shell launcher or an explicit guest tmux attachment.
  A local terminal viewer gets a row even without a host tmux server. Existing
  Mosh/SSH or host-tmux rows leading into the VM are retyped in place, not duplicated.
  Plain SSH sessions, VM background jobs, forwarding masters, and service commands
  are not labelled `opsec-tmux`. Detection reads process metadata. One bounded,
  read-only query through the already-running VM SSH master retrieves actual
  session names. It never starts the VM, opens a TCP route, creates tmux servers,
  changes tmux sizing or installs a background poller. The live view repeats the
  bounded read-only inventory query only while open. It identifies attachment
  requests still running, not remote authentication/reachability or unseen viewers
  from other users/machines. `APP`, JSON commands and PIDs remain host-side.
- `SESSION`: the actual host or VM tmux session name; `-` for direct or out-of-scope
  Mosh rows. VM names come from the guest's existing managed servers, matched to
  the requested workspace/action or explicit socket/session target, not a guessed
  workspace hash. An unavailable or ambiguous lookup shows `-`; JSON's `note`
  field explains the unavailable metadata.
  The existing owned SSH master defaults to `/run/opsec-whonix-terminal/master`;
  `TMUX_MOSH_OPSEC_CONTROL_PATH` overrides the lookup socket. The entire query
  times out after two seconds; without VM viewers there is no query.
- `APP`: application label and PID together, for example `codex (1234)`,
  `pi (5678)` or `pi-opsec (9012)`. A verified Pi child in a waiting shell's
  foreground process group is shown as `pi` with its own PID, rather than the
  Bash launcher or SSH tunnel helper. Pi is recognized by its runtime/title or
  known coding-agent CLI path, not a session name or a substring in arbitrary
  arguments. Ambiguous, foreign, dead or unrelated children do not replace the
  shell. VM Pi attachment requests show `pi-opsec` with the host viewer's PID,
  not an invented guest PID. Shell-only VM attachments are not labelled Pi.
  Other pipelines/wrappers still use their foreground group representative,
  using the active pane for tmux sessions. Detached sessions retain app info.
  JSON retains the underlying executable in `app`, actual argv in `command`,
  and the optional friendly name in `app_label`.
- `VIA`: local viewers show the terminal application, for example `xfce4-terminal`.
  Remote viewers show `ssh` or `mosh`, based on the client's process ancestry.
  No PIDs or build dates are embedded in these labels; PIDs remain in JSON.
  Unavailable or orphaned
  ancestry is `unknown`, not assumed local; missing local frontend details show
  `local` rather than inventing a terminal. `detached` means
  the session has no clients but its programs are still running. `changing` means
  the session/client snapshot raced an attachment change; rerun to refresh.
  A listed transport does not prove the window is visible or the peer is reachable.
  Detailed process/attachment states remain available in JSON, not a table column.
- Outdated `mosh`, `xfce4-terminal` and tmux type labels have a red background.
  Only the label is highlighted, not the row, padding or Mosh reachability flag.
  Tmux and XFCE compare against the installed binary (`~/.local/bin` before PATH);
  VM tmux compares against the VM's installed tmux. Mosh also checks for newer
  locally built release executables alongside its installed release, even if
  the installation symlink has deliberately not been updated. Release discovery
  requires a build-completion manifest and a readable executable ELF image;
  incomplete builds are ignored. Standard and native Mosh are compared separately,
  never against each other. This only changes reporting: no binaries are activated
  and no connections are restarted. Running executables
  are read through `/proc/PID/exe`, including replaced/deleted builds. SHA-256
  fingerprints distinguish local rebuilds sharing a version string or timestamp;
  identical copies still match. Hashes are bounded and cached per executable in
  each snapshot, without running binaries or adding a polling worker.
  Unavailable comparisons remain unhighlighted, not falsely marked outdated.
  Output redirected to a file/pipe, `TERM=dumb`, `NO_COLOR`, and JSON stay plain.
  There are no build-date columns or dates appended to the labels.
- `IDLE`: elapsed time since the Mosh server received a fresh authenticated client
  packet. Both tmux and direct Mosh rows use the same server timestamp. Local, SSH
  and detached rows show `-`; old Mosh servers without the status API also show `-`.
  No separate `LINK` column is needed when the exact packet age is available.
  When it is unavailable, `VIA` retains the coarse login flag instead, for example
  `mosh [unreachable]`. `[recent]` is not proof Termux is foregrounded;
  `[unreachable]` normally appears after about 30 seconds without a client update.
  Missing/ambiguous login records show `[unknown]`, not an invented packet age.
- `SIZING`: `active` identifies the viewer currently controlling its window's
  size under the default `latest` policy; `standby` is eligible but not currently
  controlling it. Input or resizing from another viewer transfers ownership, even
  when both terminals have identical dimensions. Ownership is read directly from tmux's
  sizing calculation, not inferred from terminal size or rounded activity times.
  `auto-off` or `manual-off` means ignored automatically or manually. `shared`
  means the `smallest`/`largest` policy combines eligible viewers; `manual` means
  an explicitly set window size. `-` means not applicable. Older running servers
  without native active-sizing reporting show `unknown` for eligible
  viewers, with a diagnostic in JSON's `note` field; installing the binary does
  not restart existing sessions.
  The report is read-only and does not itself change sizing ownership or policy.
  JSON includes `window_id`, `sizing_policy`, `sizing_client_pid` and
  `active_sizing`; a zero owner PID means no single client controls the window,
  while `null` means ownership is unavailable.
- `SIZE`: terminal columns x rows, not pixels. Tmux rows retain the individual
  viewer's dimensions. Direct Mosh/SSH and other-tmux connections read their owned
  login PTY with a read-only size query; no input is consumed or resize performed.
  Missing, inaccessible or zero-size terminals show `-`, as do detached sessions.

Command lines are not shown in the text table. `--verbose` remains accepted for
compatibility and uses the same columns as the default view. `--json` retains
full current foreground arguments in `command`, preserving case and quoting.
These are process argv, not historical shell input, environment variables or
redirections; applications may rewrite them. Arguments can contain secrets:
review JSON output before sharing it.

Control clients are included but have no terminal size or sizing participation.
The attachment's terminal path remains in JSON's `tty` field, not a table column.
JSON also exposes session/client creation and activity timestamps, the original
tmux activity age as `activity_idle_seconds`, and the detected frontend process
name/PID. Individual `app_pid`, `mosh_pid` and `reachability` fields remain in JSON
even though their dedicated table columns are removed. `idle_seconds` now means
Mosh packet age. `server_binary_mtime` identifies the selected tmux server's
running executable; unified rows expose `frontend_binary_mtime` and
`tmux_binary_mtime` as Unix timestamps or `null` when unavailable. These are
filesystem modification times, not official release dates. The unified `entries`
array also exposes `frontend_outdated` and `tmux_outdated` as true/false or `null`
when unavailable; `server_binary_outdated` describes the selected host server.
The array uses the shared table fields. Original `clients`, `sessions` and
`other_mosh_sessions` arrays remain available, and their existing client `state`
values remain compatible. `other_ssh_sessions` adds the standalone SSH channels.
In `entries`, `state` retains the process/attachment state, while `reachability`
is always separate. Detached entries keep `transport: null` in JSON; the table
displays their lowercase state in `VIA` instead. Every combined entry adds a `via`
display label; the existing uppercase `transport`/`state` values remain compatible.
Mosh arrays expose
`network_last_rx_monotonic_ms`, the server's receive timestamp, and
`network_last_seen_at`, its conversion to Unix time at observation. These fields
remain `null` when telemetry is unavailable or no client packet has been received.

Build and link the patched stock Mosh server on the receiving computer:

```sh
scripts/build-mosh.sh
scripts/link.sh
```

The build uses the verified upstream 1.4.0 archive and only adds local status
reporting. `~/.local/bin/mosh-server` points to the release binary. Stock Mosh
clients, including the phone client, continue using the existing protocol. The
normal `mosh` command discovers `mosh-server` through the remote shell's PATH;
`mosh --server=/home/lewis/.local/bin/mosh-server HOST` selects it explicitly.
Reconnect existing Mosh attachments to launch the new server. Their tmux sessions
and applications can remain running.

Each server listens on a private local Unix socket at
`$XDG_RUNTIME_DIR/mosh-status/PID.sock`, normally under `/run/user/UID`, with a
mode-0700 directory and mode-0600 socket. If the runtime directory is unavailable,
it uses `/tmp/mosh-status-UID/PID.sock`. A query returns API version, server PID and
the existing `last_heard` timestamp. Queries verify the peer's UID and PID, reject
PID reuse and have a short timeout. The server replies only when queried; it adds
no polling worker or per-packet file writes. Replayed and unauthenticated packets
do not refresh the timestamp. This reporting runs only for `clients`; the sizing
monitor continues using the existing login records.

The `TMUX` rows cover only the selected socket, not other servers or VMs. A
closed desktop window should lose its client on the next snapshot; a reopened
window has a new attachment. No server restart is needed to use updated reporting.
The old `tmux clients`, `tmux list` and `--manage-sizing` launcher syntax is
replaced by the native session commands and this standalone helper.

The same table includes this user's other live Mosh servers and incoming OpenSSH
shell/command sessions on the current host,
without duplicating those already represented by tmux rows. It works even when
there is no tmux server at the selected socket. `OTHER-TMUX` sessions are not
queried or managed; their label prevents mistaking them for standalone apps.

Discovery reads current-user `/proc` metadata and reuses the Mosh login snapshot.
It identifies the foreground process group rather than displaying a waiting shell
or background job as the active app. SSH commands without a terminal show the
session's initial command process instead. SSH identification requires a live
`sshd`, `sshd-session`, or `sshd-auth` parent; an inherited SSH environment alone
does not make a local process a remote login. Separate session channels under one
SSH connection stay separate, and channels already hosting a listed tmux client
are not duplicated. Outgoing SSH clients, unauthenticated daemon processes and
forwarding-only connections without a shell/command are not listed.

For SSH peers, discovery extracts only the validated address from the owned login
process's `SSH_CONNECTION` variable, with a bounded read and PID-identity check.
Unavailable or malformed metadata leaves `PEER` unknown. Other environment values
are never displayed or retained; command arguments, terminal contents and clipboard
data are not read. Missing, duplicate
or mismatched login records never prove disconnection. Network contact is
unavailable for SSH and unpatched Mosh rows; it is never inferred from process
age or `who`'s terminal idle field. JSON adds `other_mosh_sessions` and
`other_ssh_sessions` separately from
tmux `clients`/`sessions`, and a `server_running` flag for the selected socket.
One process snapshot supplies tmux app identification and Mosh/SSH discovery.
The same snapshot supplies VM viewers; `other_opsec_sessions` adds previously
unrepresented host-side viewers to JSON. Retyped records retain an
`opsec_connection` object containing the outgoing SSH PID, target, requested
workspace/action or explicit guest socket/target, and the resolved guest session
name and server socket when available. Raw host `clients[].session` and
`sessions[].name` retain their host identities; `entries[].session` is the
displayed guest name for `opsec-tmux`. The unified schema stays consistent across
all row types.
It runs only for `clients`, not in the sizing monitor, and standalone remote sessions
never participate in tmux sizing.

Native `client-attached` and `after-new-session` hooks call `tmux-mosh ensure`.
It starts one monitor per server if a Mosh client is detected. `ensure` performs
the same check for an existing server; `start` explicitly starts it even without
Mosh clients. Repeated calls reuse the monitor without restarting or detaching
anyone. Startup errors are reported on stderr and in its private log; inventory
remains independently usable. The helper defaults to the inherited `TMUX` socket
inside a pane and the Fish socket outside it. `-S`/`--socket` selects a socket;
`--tmux` selects the backend. The helper refuses unmarked servers. Native tmux
itself has normal tmux behavior, without launcher restrictions on other servers.

The monitor reads client/process metadata and stock Mosh login records every two
seconds. With no Mosh clients, it checks inventory every five seconds and does
not run `who`. It exits when its server disappears. No packet capture, probes,
Mosh changes, phone update or root access is needed.

When Mosh explicitly marks a client unreachable, the monitor sets its
`ignore-size` flag. With another eligible viewer attached, the desktop expands
without ending the phone connection or application. Reconnection clears only
the monitor's flag. Manual flags and unrelated flags survive. Ownership is
recorded in private user options for crash recovery; PID/start-time and server
identity checks prevent stale updates targeting a replacement client/server.
Unchanged polls do not write options or force redraw. The configured
`window-size` policy is preserved.

Stock Mosh normally marks a lost peer after about 30 seconds, followed by at most
one polling interval. This is network reachability, not keyboard idle time or
Android foreground/background state. A backgrounded Termux still sending traffic
remains connected. Missing/ambiguous records show `UNKNOWN`, not `UNREACHABLE`,
and do not cause new exclusions. Unavailable records clear previous automatic
flags when identity can be verified. Requires Linux `/proc`, GNU `who`, and Mosh
login-record support.

Mode-0600 `.sizing.lock` and `.sizing.log` files live beside the socket. Unsafe
paths are refused. The log records startup/runtime failures and is bounded at
startup. Internal `@tmux-simple-*` option names remain compatible with running
monitors; they are not a second command interface.

## Structure And Build

```text
tmux-simple/
  tmux-mosh                     Standalone Mosh status/sizing command
  tmux_clients.py               Inventory, identity guards and monitor implementation
  client_tree.py                Inventory -> collapsible live tree / expanded snapshot
  mosh_cleanup.py               Explicit packet-age cleanup with PID-safe signals
  mosh_sessions.py              Current-user direct/other-Mosh process discovery
  mosh_status.py                Private server API -> packet age and receive timestamp
  ssh_sessions.py               Incoming SSH channels, app/peer metadata and deduplication
  opsec_sessions.py             Foreground opsec VM viewers on the existing SSH route
  tmux-simple.conf              Symlink to ../config/tmux-simple/tmux.conf
  pyproject.toml                Local uv test dependencies and tooling settings
  uv.lock                       Locked Python test dependencies
  src/                         Maintained tmux 3.7c fork source and upstream notices
    window-copy.c              History, reading anchors, rectangle and clipboard copying
    server-client.c            Nonmodal input and explicit Ctrl+C selection handling
    input.c                    Application OSC 52 and desktop clipboard helper
    format.c, resize.c, tmux.h  Active-device sizing and ownership reporting
    configure.ac, Makefile.am   Native build definitions; no patch replay
  mosh/
    last-received.patch         Expose the existing server receive timestamp
    server-status.h             Private, nonblocking Unix socket status reply
  scripts/
    build.sh                    Snapshot repository source, compile, link runtime
    source-snapshot.sh          Working-tree files -> isolated source and hash manifests
    build-mosh.sh               Verify upstream Mosh, apply status patch, build release
    link.sh                     Link tmux, tmux-mosh, helpers and the Mosh fork commands
    clipboard.sh                Selection on stdin -> available desktop clipboard
    attach-environment.sh       Owned client environment -> session display variables
  tests/
    conftest.py                 Private HOME/socket/PTY and loopback fixtures
    terminal_harness.py         Local PTY driver and VTE/Termux emulator interface
    loopback_harness.py         Compatibility-fork loss/outage/roaming transport fixtures
    workload.py                 Synthetic output and owned input/resize log
    test_cli.py                 Native CLI, lifecycle and installation checks
    test_fork_policy.py         Reject distro/PATH binaries as current fork references
    test_compat_cli.py          Native create-or-attach and argument semantics
    test_config.py             Config loading, persistence and safe installers
    test_attach_hooks.py       Display preservation, Mosh hooks and Fish arguments
    test_interaction.py        Typing, mouse, selection and stock-Mosh behavior
    test_mosh_sizing.py         Status, offline sizing and lifecycle safety
    test_mosh_status.py         Release-server packet age, outages, replay and API validation
    test_mosh_cleanup.py        Age cutoffs, safe signalling, reconnects and retained tmux apps
    test_client_inventory.py    Detached sessions, activity, origins and XFCE close
    test_inventory_table.py     Unified rows, shared columns and foreground app PIDs
    test_client_tree.py         Grouping, folds, clicks, refresh/resize and terminal cleanup
    test_process_details.py     Bounded foreground argv and read-only PTY size queries
    test_app_identity.py        Pi runtime labels and foreground child/launcher identity
    test_opsec_inventory.py     VM viewer types, routes, origins and duplicate prevention
    test_opsec_session_names.py Actual guest names, workspace aliases and safe query failures
    test_standalone_mosh.py      Direct apps, foreground identity and Mosh deduplication
    test_standalone_ssh.py       SSH channels, missing metadata and tmux deduplication
    test_sizing.py             Active viewer and keyboard/portrait-height regressions
    reading_position_workload.py  Synthetic short/wrapped/Unicode history records
    test_reading_position.py   Reading anchors, real Fish and viewport bounds
    test_native_resize.py      Guard the narrow copy-mode patch and native parser
    test_hyperlinks.py         Real VTE/XFCE widget file-link regressions
    test_test_infrastructure.py  Local dependency and emulator smoke checks
    oracles/vte.py             Headless VTE viewport observer
    oracles/prepare_termux.py  Fetch pinned Termux emulator and compile test host
    oracles/java/              Headless test host and Android API adapters
  verification.json             Behavior inventory and evidence
  build/                        Sources, release binaries, build logs (ignored)
  artifacts/                    Verification/diagnostic evidence (ignored)
../config/tmux-simple/
  tmux.conf                     Canonical interaction settings
  integration.conf              Attachment hooks and selective environment updates
  tmux.fish                     Existing-socket default; otherwise native arguments
  link.sh                       Install config/Fish symlinks; refuse foreign files
```

Keep the source/config repositories beside each other. Run in this order:

```sh
scripts/build.sh
scripts/build-mosh.sh
scripts/link.sh
bash ../config/tmux-simple/link.sh
```

Build inputs are the maintained source under `src/`, including current working-tree
edits. There is no upstream download or tmux patch replay. Requires compiler, make,
autoconf, automake, pkg-config, ncurses/libevent development files, Git and tar.
Output is a fresh `build/release.XXXXXX/src/tmux`, its source-file manifest,
source checksums and binary checksums. Only a successful build changes
`build/runtime/tmux`. Runtime uses Bash, Python 3.12+ standard library, the built
binary, and optionally `wl-copy`/`xclip` for desktop clipboard delivery.

Set `BUILD_RUNTIME=/absolute/directory` to publish a verification build somewhere
other than the normal runtime links. Source snapshots exclude ignored build and
test artifacts but include new source files and edits not yet committed. The
separate compatibility Mosh build still uses its pinned upstream archive and
standalone status patch; it does not modify this tmux source.

The installer symlinks `~/.local/bin/tmux` directly to `build/runtime/tmux`, adds
`~/.local/bin/tmux-mosh`, and links clipboard/attachment helpers and integration
config under `~/.local/libexec/tmux/`. After the compatibility Mosh build, it also
symlinks `mosh`, `mosh-client` and `mosh-server` to that build's launcher and binaries.
It removes only this project's old
`tmux-simple` command symlink. All destinations are preflighted; foreign files
or links are refused, not overwritten. Keep `~/.local/bin` ahead of system
commands in PATH. It never copies binaries, removes packages, stops servers or
migrates applications. There is no `--as-tmux` option.

## Configuration

The authoritative file is `../config/tmux-simple/tmux.conf`. This project's
`tmux-simple.conf` is a relative symlink, not a second copy. The config installer
links `~/.tmux.conf`, the compatibility pointer
`$XDG_CONFIG_HOME/tmux-simple/tmux.conf`, and
`$XDG_CONFIG_HOME/fish/functions/tmux.fish`. An empty/relative `XDG_CONFIG_HOME`
uses `~/.config`. `bootstrap.sh` invokes the same installer. Managed legacy links
can be replaced; foreign files/links are refused. Archived vanilla settings in
`../config/legacy/tmux/tmux.conf` remain untouched.

Native tmux loads `~/.tmux.conf` when starting a server; `-f FILE` selects another
config. Creating/attaching sessions on an existing server does not reload it.
`set-titles on` forwards the active pane's title to each viewing terminal with
its current session name. The bounded, acyclic `@codex-title-*` formats use
tmux's own format engine, never per-frame `#()` subprocesses, to produce
`tmux: diet - codex: ~/tasks/diet - Conversation title - Terminal` in XFCE4
Terminal. A braille spinner or attention marker appears immediately before the
directory. The real pane directory replaces Codex's abbreviated project label;
both legacy `task | project` titles and newer
`terminal_title = ["app-name", "activity", "current-dir", "thread-title"]`
selections are supported without restarting Codex. Non-Codex titles are
forwarded with the session prefix. All forwarded titles replace ` | ` separators
with ` - ` while preserving the underlying pane title, activity and session
renames. The hidden status bar and mouse/key bindings are unaffected.

Installation alone does not reconfigure/restart servers. To enable only the new
hooks on an existing patched server without rebinding controls:

```sh
tmux source-file ~/.local/libexec/tmux/integration.conf
tmux-mosh ensure
```

The integration fragment removes `DISPLAY`, `WAYLAND_DISPLAY`, and `XAUTHORITY`
from the variables tmux normally updates/unsets on attachment. Its short Bash
hook reads nonempty values from the owned attaching client's `/proc` environment
and updates only its session, after checking the PID/session pair in the server.
Desktop attachments refresh display access; phone attachments lacking it do not
erase it. Other environment updates, including `SSH_AUTH_SOCK`, retain normal
tmux behavior. `copy-command` invokes the symlinked clipboard helper without a
launcher-specific environment variable. Hyperlink support stays in `tmux.conf`.

The existing C patches, tmux renderer/history grid, PTYs, session lifecycle and
Mosh wire protocol are unchanged by the launcher removal. No native-history
export, new transport or custom Mosh client/server is used.

## Verification

The terminal and loopback harnesses live in this repository. Tests use a local
uv environment and stock Mosh; neither retired native-history project is needed.
System prerequisites are Java, Xvfb, stock Mosh, and system Python with GTK3/VTE.
Real XFCE clipboard checks also need D-Bus, xdotool, and the built terminal fork.
The preparation command fetches the pinned upstream Termux emulator and compiles
the local Java test host into `build/termux/classes`; source provenance and the
upstream license are retained in `build/termux`. Run preparation once, or after
removing that cache. Build the terminal fork's `test-terminal-links` target first;
`TEST_XFCE_LINKS` can override the oracle path.

```sh
UV_CACHE_DIR=/data/.cache/uv uv sync --locked
UV_CACHE_DIR=/data/.cache/uv uv run --locked python tests/oracles/prepare_termux.py
UV_CACHE_DIR=/data/.cache/uv uv run --locked python -m pytest -q
shellcheck scripts/*.sh ../config/tmux-simple/link.sh
shfmt -d -i 4 scripts/*.sh ../config/tmux-simple/link.sh
fish --no-config -n ../config/tmux-simple/tmux.fish
```

Tests use private HOME/XDG directories, tmux sockets, synthetic workloads, fake
clipboard executables, headless VTE/Termux observers and stock Mosh on loopback.
They exercise create/reattach without process replacement, mouse headers,
retained-history copying, desktop display preservation, native hooks, singleton
monitor startup, offline/reconnect sizing, unsafe paths and stale identities.
Real GNU `who` reads a temporary utmp fixture; `/run/utmp` is never modified.
File opens/manager requests go to isolated recorders, not real desktop apps.
No test contacts a phone, scans a network, changes the real clipboard, or drives
a live tmux/Codex session. `verification.json` records current and historical
results, release hashes, retained failed runs and manual gaps.

## Limits And Security

This is not a guarantee of native terminal equivalence. Remote history is
limited to 100,000 rows; browsing needs connectivity. Alternate-screen repaints
are not a complete transcript. A fresh Mosh attachment's native scrollback can
contain only one screen; wheel/swipe requests remote history instead.

Actual XFCE/Termux gestures, Android keyboard animations, clipboard permissions,
real roaming, long sessions and unusual terminal extensions remain manual checks.
The headless Termux observer is not the phone's Android UI.

Clipboard writes are enabled, including application OSC 52. Treat untrusted
terminal output accordingly. The desktop helper can update the host clipboard
when selecting from another attachment: intentional sharing, not isolation.
OSC 52 delivery still requires terminal capability and permission.

Changing symlinks never migrates a running program. Keep an old patched binary
available until its servers exit if switching backends. Project/test-infrastructure
cleanup does not attach to or terminate running sessions.
