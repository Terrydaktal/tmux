# Preserve the retained-history reading position during resize

While browsing retained history, width/height resize bursts preserve the logical
top-of-view anchor instead of replacing it with the cursor position. Scrolling
establishes a new anchor; selecting/copying preserves it. Returning to the live
tail retains native behavior, and tiny or oversized viewports remain bounded.

window-copy.c maps the anchor through native grid wrapping/unwrapping and resets
it on real viewport movement. It does not replace application transcripts, force
Pi/Codex refreshes, change the live input parser or introduce a whole-screen
reflow workaround. Native-source verification always compares tracked source;
it never downloads an archive and replays obsolete patches.

## Verification

```sh
uv run pytest -q tests/test_reading_position.py tests/test_native_resize.py
```

Tests use disposable sockets, processes, HOME directories and emulator/GUI displays.
