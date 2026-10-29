# Use terminal-style history and explicit clipboard copying

Typing and paste leave retained history and reach the application unchanged.
Dragging, releasing, word selection and line selection only highlight; Ctrl+C
explicitly copies an existing selection while preserving it and the reading
position. With no selection, Ctrl+C keeps its normal interrupt behavior.

The native scroll-on-input option is opt-in and defaults off. The interaction
configuration enables it, forwards mouse-aware application gestures, binds
selection release to stop-selection and binds Ctrl+C to copy-pipe-no-clear.
server-client.c preserves that selected Ctrl+C instead of exiting copy mode.
window-copy.c delivers the selected clipboard write directly to the initiating
viewer when redraw scheduling would otherwise discard it; permissions and
terminal capabilities still apply. No automatic-copy intermediate is introduced.

History-wheel bindings request complete frames after movement so rapid styled
scrolling does not leave incremental redraw fragments. Hyperlink advertising and
Ctrl-click path actions remain configuration/terminal responsibilities. Tests
cover the final explicit-copy contract, full-frame repaint, literal native CLI
arguments, repeated selection and a disposable real-Codex transcript using
terminal-owned selection rather than the retired Codex-owned copy hint.

## Verification

```sh
uv run pytest -q tests/test_cli.py tests/test_compat_cli.py tests/test_interaction.py tests/test_scroll_redraw.py tests/test_hyperlinks.py tests/test_codex_clipboard.py
```

Tests use disposable sockets, processes, HOME directories and emulator/GUI displays.

Physical Termux gesture/clipboard UI and desktop clipboard permissions require manual device checks. The real-Codex adapter requires TEST_CODEX to name a compatible local build; generic terminal-copy coverage does not depend on it.
