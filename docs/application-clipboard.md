# Deliver application clipboard writes to the attached viewer

Application-owned OSC 52 clipboard writes reach the interacting terminal and its
configured desktop helper. Plain and write-only tmux-wrapped requests share the
same policy checks; clipboard reads and general passthrough remain disabled.
A desktop viewer attaching to a remotely started session supplies the display
environment, and a phone with no display variables does not clear that access.

input.c selects the correct native client and session environment for delivery.
scripts/attach-environment.sh SOCKET CLIENT_PID SESSION_ID reads only the owned
live client's nonempty DISPLAY, WAYLAND_DISPLAY and XAUTHORITY values. It quotes
tmux command syntax and checks the PID/session pair before applying updates.
The native installer links the helper and the canonical integration config using
resolved paths; it does not assume adjacent repositories or own Mosh commands.
Integration hooks may call the separately installed tmux-mosh app, but this
repository does not implement that app or its monitor.

## Verification

```sh
uv run pytest -q tests/test_application_clipboard.py tests/test_attach_hooks.py tests/test_config.py tests/test_repo_paths.py
```

Tests use disposable sockets, processes, HOME directories and emulator/GUI displays.

Real XFCE tests run under a private Xvfb/D-Bus display. VM clipboard permissions and physical phone clipboard behavior remain separate deployment checks.
