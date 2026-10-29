import base64

import pytest


@pytest.mark.parametrize(
    "table,key",
    [
        ("root", "WheelUpPane"),
        ("copy-mode", "WheelUpPane"),
        ("copy-mode", "WheelDownPane"),
    ],
)
def test_history_wheel_bindings_request_a_complete_frame(backend, table, key):
    app = backend.attach()
    try:
        app.until(lambda state: b"READY" in state["screen"])
        bindings = backend.run("list-keys", "-T", table).stdout.splitlines()
        binding = next(line for line in bindings if key.encode() in line.split())
        assert b"refresh-client" in binding
        assert b"-N 5" in binding
        if table == "root":
            assert b"mouse_any_flag" in binding
            assert b"send-keys -M" in binding
    finally:
        app.close()


def visible_rows(data):
    return [line.rstrip() for line in data.decode().splitlines()]


def emulator_rows(state, kind):
    if kind == "termux":
        return [base64.b64decode(row).decode().rstrip() for row in state["lines"]]
    return visible_rows(state["screen"])


@pytest.mark.parametrize("kind", ["vte", "termux"])
def test_fast_styled_history_scroll_matches_the_stored_viewport(backend, kind):
    app = backend.attach(kind, cols=137, rows=66)
    try:
        app.until(lambda state: b"READY" in state["screen"])
        app.send(b"C")
        app.until(lambda state: b"COLORED-HISTORY-END" in state["screen"])
        for _ in range(12):
            for direction, count in [(64, 12), (65, 8)]:
                app.send(f"\x1b[<{direction};2;8M".encode() * count)
                app.pump(0.15)
                assert backend.field("pane_in_mode") == b"1"
                offset = int(backend.field("scroll_position"))
                # Mode capture exposes the backing grid, not its scrolled viewport.
                expected = backend.run(
                    "capture-pane",
                    "-p",
                    "-M",
                    "-S",
                    str(-offset),
                    "-E",
                    str(65 - offset),
                    "-t",
                    "=test:.",
                ).stdout
                app.until(
                    lambda state, expected=expected: (
                        emulator_rows(state, kind) == visible_rows(expected)
                    ),
                    timeout=1,
                )
        position = backend.field("scroll_position")
        app.send(b"\x1b[<0;2;5M\x1b[<32;12;5M\x1b[<0;12;5m")
        app.until(lambda state: backend.field("selection_present") == b"1")
        assert backend.field("scroll_position") == position
        assert backend.field("pane_in_mode") == b"1"
    finally:
        app.close()
