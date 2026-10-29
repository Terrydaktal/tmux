# Report which attached viewer controls window sizing

Native formats report the viewer that actually controls latest-client window
sizing, rather than inferring ownership from coincidentally matching dimensions.
Eligible active/standby viewers, ignored clients and manual/shared sizing policies
remain distinguishable; reporting does not start a transport monitor.

The native selection path in resize.c records ownership, tmux.h retains it and
format.c exposes it. The tmux manual documents the added formats. Both viewers
still share one application size; this is accurate ownership reporting, not
independent per-viewer layouts or Mosh management.

## Verification

```sh
uv run pytest -q tests/test_active_sizing.py tests/test_sizing.py
```

Tests use disposable sockets, processes, HOME directories and emulator/GUI displays.
