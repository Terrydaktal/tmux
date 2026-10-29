# Copy frozen rectangular selections from their stored corners

A released rectangular selection copies exactly its highlighted cells in either
drag direction, even after the cursor moves. Ordinary drag/word/line selection
remains linear, and copying preserves the selection and viewport.

Upstream rectangle extraction inferred width from the moving cursor and dragged
endpoint. window-copy.c instead uses the stored selection corners and includes
the highlighted edge cells. Both explicit Ctrl+C and the terminal's direct
native copy command use this extraction path. Mouse-aware applications retain
their own gestures; the native fix does not capture their input.

## Verification

```sh
uv run pytest -q tests/test_rectangle_selection.py
```

Tests use disposable sockets, processes, HOME directories and emulator/GUI displays.
