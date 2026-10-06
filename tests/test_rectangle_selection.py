import pytest
from test_interaction import assert_not_copied, history, prepare_clipboard, scroll


def viewport(backend):
    offset = int(backend.field("scroll_position") or b"0")
    return backend.run(
        "capture-pane",
        "-p",
        "-M",
        "-S",
        str(-offset),
        "-E",
        str(23 - offset),
        "-t",
        "=test:.",
    ).stdout.splitlines()


def box_drag(app, release_control=False, start=(9, 5), end=(12, 7)):
    sx, sy = start
    ex, ey = end
    app.send(f"\x1b[<16;{sx};{sy}M\x1b[<48;{ex};{ey}M".encode())
    app.pump(0.1)
    release = 0 if release_control else 16
    app.send(f"\x1b[<{release};{ex};{ey}m".encode())
    app.pump(0.15)


def copy_directly(backend):
    client = backend.run("list-clients", "-F", "#{client_name}").stdout.strip().decode()
    backend.run("send-keys", "-c", client, "-t", "=test:.", "-X", "copy-pipe-no-clear")


def selection_state(backend):
    return backend.run(
        "display-message",
        "-p",
        "-t",
        "=test:.",
        "#{scroll_position}:#{copy_cursor_x}:#{copy_cursor_y}:"
        "#{selection_start_x}:#{selection_start_y}:"
        "#{selection_end_x}:#{selection_end_y}:#{selection_active}",
    ).stdout


@pytest.mark.parametrize("kind", ["termux", "vte"])
@pytest.mark.parametrize("in_history", [False, True])
@pytest.mark.parametrize("release_control", [False, True])
def test_control_drag_selects_only_the_rectangle_and_copies_explicitly(
    backend, kind, in_history, release_control
):
    app = backend.attach(kind)
    try:
        history(backend, app)
        if in_history:
            scroll(backend, app)
        rows = viewport(backend)
        expected = b"\n".join(row[8:12] for row in rows[4:7])
        position = backend.field("scroll_position") or b"0"
        copied = prepare_clipboard(backend, app, kind)
        before = backend.received()
        box_drag(app, release_control)
        assert backend.field("rectangle_toggle") == b"1"
        assert backend.field("selection_present") == b"1"
        assert backend.field("selection_active") == b"0"
        assert_not_copied(backend, app, copied, kind)
        app.send(b"\x03")
        app.until(lambda state: copied.exists())
        assert copied.read_bytes() == expected
        assert backend.run("show-buffer").stdout == expected
        assert backend.field("pane_in_mode") == b"1"
        assert backend.field("scroll_position") == position
        assert backend.received() == before
    finally:
        app.close()


@pytest.mark.parametrize("kind", ["termux", "vte"])
@pytest.mark.parametrize(
    "start,end",
    [((9, 5), (12, 7)), ((12, 5), (9, 7)), ((9, 7), (12, 5)), ((12, 7), (9, 5))],
)
def test_rectangle_direct_copy_matches_highlight_in_every_drag_direction(
    backend, kind, start, end
):
    app = backend.attach(kind)
    try:
        history(backend, app)
        scroll(backend, app)
        rows = viewport(backend)
        expected = b"\n".join(row[8:12] for row in rows[4:7])
        copied = prepare_clipboard(backend, app, kind)
        box_drag(app, start=start, end=end)
        assert_not_copied(backend, app, copied, kind)
        selection = selection_state(backend)
        # XFCE's explicit copy integration invokes this command, not a key binding.
        copy_directly(backend)
        app.until(lambda state: copied.exists())
        assert copied.read_bytes() == expected
        assert backend.run("show-buffer").stdout == expected
        assert selection_state(backend) == selection
    finally:
        app.close()


@pytest.mark.parametrize("kind", ["termux", "vte"])
@pytest.mark.parametrize("direct", [False, True])
def test_rectangle_survives_scrolling_offscreen_and_repeated_copy(
    backend, kind, direct
):
    app = backend.attach(kind)
    try:
        history(backend, app)
        scroll(backend, app)
        rows = viewport(backend)
        expected = b"\n".join(row[8:12] for row in rows[4:7])
        copied = prepare_clipboard(backend, app, kind)
        box_drag(app)
        position = int(backend.field("scroll_position"))
        app.send(b"\x1b[<64;2;8M" * 8)
        app.until(lambda state: int(backend.field("scroll_position")) >= position + 40)
        assert backend.field("selection_active") == b"0"
        assert_not_copied(backend, app, copied, kind)
        selection = selection_state(backend)
        screen = app.term.state["screen"]
        for _ in range(2):
            if copied.exists():
                copied.unlink()
            if direct:
                copy_directly(backend)
            else:
                app.send(b"\x03")
            app.until(lambda state: copied.exists())
            assert copied.read_bytes() == expected
            assert selection_state(backend) == selection
            assert app.term.state["screen"] == screen
            assert backend.field("pane_in_mode") == b"1"
    finally:
        app.close()


@pytest.mark.parametrize("kind", ["termux", "vte"])
def test_plain_drag_after_a_box_returns_to_linear_selection(backend, kind):
    app = backend.attach(kind)
    try:
        history(backend, app)
        scroll(backend, app)
        box_drag(app)
        assert backend.field("rectangle_toggle") == b"1"
        app.send(b"\x1b[<0;9;5M\x1b[<32;12;7M\x1b[<0;12;7m")
        app.pump(0.15)
        assert backend.field("rectangle_toggle") == b"0"
        assert backend.field("selection_present") == b"1"
        assert backend.field("selection_active") == b"0"
        copied = prepare_clipboard(backend, app, kind)
        rows = viewport(backend)
        expected = b"\n".join([rows[4][8:], rows[5], rows[6][:11]])
        app.send(b"\x03")
        app.until(lambda state: copied.exists())
        assert copied.read_bytes() == expected
    finally:
        app.close()


@pytest.mark.parametrize("kind", ["termux", "vte"])
def test_rectangle_copies_wide_unicode_by_display_columns(backend, kind):
    app = backend.attach(kind)
    try:
        app.until(lambda state: b"READY" in state["screen"])
        app.send(b"B")
        app.until(lambda state: b"BOX-READY" in state["screen"])
        copied = prepare_clipboard(backend, app, kind)
        box_drag(app, start=(8, 1), end=(11, 3))
        assert backend.field("selection_active") == b"0"
        assert_not_copied(backend, app, copied, kind)
        app.send(b"\x03")
        app.until(lambda state: copied.exists())
        assert (
            copied.read_bytes() == "\u4e2d\u6587\n\u4e2d\u6587\n\u4e2d\u6587".encode()
        )
    finally:
        app.close()


@pytest.mark.parametrize("kind", ["termux", "vte"])
def test_rectangle_can_select_one_column_across_multiple_rows(backend, kind):
    app = backend.attach(kind)
    try:
        history(backend, app)
        scroll(backend, app)
        rows = viewport(backend)
        copied = prepare_clipboard(backend, app, kind)
        box_drag(app, start=(9, 5), end=(9, 7))
        assert backend.field("selection_present") == b"1"
        assert backend.field("selection_active") == b"0"
        assert_not_copied(backend, app, copied, kind)
        app.send(b"\x03")
        app.until(lambda state: copied.exists())
        assert copied.read_bytes() == b"\n".join(row[8:9] for row in rows[4:7])
    finally:
        app.close()


@pytest.mark.parametrize("kind", ["termux", "vte"])
def test_control_drag_still_reaches_mouse_aware_applications(backend, kind):
    app = backend.attach(kind)
    try:
        app.until(lambda state: b"READY" in state["screen"])
        app.send(b"A")
        app.until(lambda state: b"CLICK-HEADER" in state["screen"])
        before = backend.received()
        payload = b"\x1b[<16;9;5M\x1b[<48;12;7M\x1b[<16;12;7m"
        app.send(payload)
        app.until(lambda state: backend.received() == before + payload)
        assert backend.field("pane_in_mode") == b"0"
    finally:
        app.close()
