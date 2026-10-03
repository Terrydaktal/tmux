"""Keep the visible history anchor across resize, not the copy-mode cursor."""

import base64
import re
import shlex
import shutil
import sys
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def visible_prefix(app, rows=1):
    state = app.term.state
    if "alternate" in state:
        return b"".join(base64.b64decode(line) for line in state["lines"][:rows])
    # VTE joins soft wraps; measure the cells used by these controlled fixtures.
    text = state["screen"].splitlines()[0].decode()
    cells = 0
    prefix = []
    for char in text:
        width = 0 if unicodedata.combining(char) else 1
        if width and unicodedata.east_asian_width(char) in ("W", "F"):
            width = 2
        if cells + width > state["cols"] * rows:
            break
        prefix.append(char)
        cells += width
    return "".join(prefix).encode()


def first_line(app):
    return visible_prefix(app).rstrip()


def compact(text):
    return b"".join(text.split())


def prepare(backend, kind, style="short"):
    backend.program = lambda: [
        sys.executable,
        str(ROOT / "tests/reading_position_workload.py"),
        str(backend.log),
        style,
    ]
    app = backend.attach(kind=kind, cols=121, rows=45)
    app.until(lambda state: b"READY" in state["screen"])
    app.send(b"H")
    app.until(lambda state: b"HISTORY-END" in state["screen"])
    app.send(b"\x1b[<64;2;8M" * 8)
    app.until(lambda state: int(backend.field("scroll_position") or b"0") >= 40)
    app.pump(0.1)
    return app


def resized(app, backend, cols, rows):
    app.resize(cols, rows)
    app.until(
        lambda state: (
            backend.field("pane_width") == str(cols).encode()
            and backend.field("pane_height") == str(rows).encode()
        )
    )
    app.pump(0.15)
    assert backend.field("pane_in_mode") == b"1"
    assert 0 <= int(backend.field("copy_cursor_y")) < rows


@pytest.mark.parametrize("kind", ["termux", "vte"])
def test_short_history_keeps_the_top_line_through_width_and_height_changes(
    backend, kind
):
    app = prepare(backend, kind)
    try:
        top = first_line(app)
        assert b"record-" in top
        for cols, rows in ((73, 45), (121, 45), (121, 15), (121, 65), (121, 45)):
            resized(app, backend, cols, rows)
            assert first_line(app) == top
            assert backend.field("scroll_position") != b"0"
    finally:
        app.close()


@pytest.mark.parametrize("kind", ["termux", "vte"])
@pytest.mark.parametrize("style", ["wrapped", "unicode"])
def test_wrapped_history_round_trips_without_anchor_drift(backend, kind, style):
    app = prepare(backend, kind, style)
    try:
        top = first_line(app)
        fragment = compact(top)[:24]
        for _ in range(3):
            for cols, rows in ((73, 15), (149, 65), (97, 31), (121, 45)):
                resized(app, backend, cols, rows)
                visible_start = compact(visible_prefix(app, rows=2))
                assert fragment in visible_start
            if style == "wrapped":
                assert first_line(app) == top
            else:
                # Native reflow can repack wide/combining cells on a round trip.
                assert fragment in compact(first_line(app))
        before = backend.received()
        app.send(b"typed-after-resize")
        app.until(lambda state: backend.received() == before + b"typed-after-resize")
        assert backend.field("pane_in_mode") == b"0"
    finally:
        app.close()


@pytest.mark.parametrize("kind", ["termux", "vte"])
def test_scrolling_after_resize_establishes_a_new_reading_position(backend, kind):
    app = prepare(backend, kind, "wrapped")
    try:
        resized(app, backend, 73, 45)
        previous = backend.field("scroll_position")
        app.send(b"\x1b[<64;2;8M")
        app.until(lambda state: backend.field("scroll_position") != previous)
        top = first_line(app)
        for cols, rows in ((121, 25), (149, 65), (73, 45)):
            resized(app, backend, cols, rows)
        assert first_line(app) == top
    finally:
        app.close()


@pytest.mark.parametrize("kind", ["termux", "vte"])
def test_selecting_and_copying_do_not_discard_the_resize_anchor(backend, kind):
    app = prepare(backend, kind, "wrapped")
    try:
        top = first_line(app)
        resized(app, backend, 73, 45)
        copied = backend.socket.parent / "copied"
        backend.run(
            "set-option", "-s", "copy-command", f"cat > {shlex.quote(str(copied))}"
        )
        position = backend.field("scroll_position")
        app.send(b"\x1b[<0;2;3M\x1b[<32;20;3M\x1b[<0;20;3m")
        app.until(lambda state: backend.field("selection_present") == b"1")
        assert not copied.exists()
        app.send(b"\x03")
        app.until(lambda state: copied.exists() and copied.stat().st_size > 0)
        assert backend.field("scroll_position") == position
        resized(app, backend, 121, 45)
        assert first_line(app) == top
    finally:
        app.close()


@pytest.mark.parametrize("kind", ["termux", "vte"])
def test_tiny_viewport_and_oldest_history_are_bounded(backend, kind):
    app = prepare(backend, kind, "unicode")
    try:
        backend.run("send-keys", "-X", "history-top")
        app.pump(0.15)
        top = first_line(app)
        for cols, rows in ((15, 5), (2, 2), (121, 45)):
            resized(app, backend, cols, rows)
        assert first_line(app) == top
        assert b"record-0000" in app.term.state["screen"]
    finally:
        app.close()


@pytest.mark.parametrize("kind", ["termux", "vte"])
def test_copy_mode_at_the_bottom_retains_native_live_tail_behavior(backend, kind):
    app = prepare(backend, kind)
    try:
        backend.run("send-keys", "-X", "history-bottom")
        app.pump(0.15)
        assert backend.field("scroll_position") == b"0"
        for cols, rows in ((73, 15), (149, 65), (121, 45)):
            resized(app, backend, cols, rows)
            assert backend.field("scroll_position") == b"0"
            assert b"HISTORY-END" in app.term.state["screen"]
    finally:
        app.close()


@pytest.mark.parametrize("kind", ["termux", "vte"])
def test_anchor_returns_after_a_viewport_taller_than_the_saved_history(backend, kind):
    app = prepare(backend, kind)
    try:
        top = first_line(app)
        resized(app, backend, 121, 450)
        assert backend.field("scroll_position") == b"0"
        resized(app, backend, 121, 45)
        assert first_line(app) == top
        assert backend.field("scroll_position") != b"0"
    finally:
        app.close()


@pytest.mark.parametrize("kind", ["termux", "vte"])
def test_another_viewer_resizing_retains_the_shared_reading_position(backend, kind):
    desktop = prepare(backend, kind, "wrapped")
    phone = None
    try:
        fragment = compact(first_line(desktop))[:24]
        pane_pid = backend.field("pane_pid")
        phone = backend.attach(kind="termux", existing=True, cols=73, rows=15)
        phone.until(lambda state: backend.field("pane_width") == b"73")
        phone.pump(0.15)
        assert fragment in compact(visible_prefix(phone, rows=2))
        resized(desktop, backend, 149, 65)
        assert fragment in compact(visible_prefix(desktop, rows=2))
        resized(phone, backend, 97, 31)
        assert fragment in compact(visible_prefix(phone, rows=2))
        assert backend.field("pane_pid") == pane_pid
        assert backend.field("window-size") == b"latest"
    finally:
        if phone is not None:
            phone.close()
        desktop.close()


def test_fish_scroll_viewport_survives_the_reported_resize_round_trip(backend):
    fish = shutil.which("fish")
    if fish is None:
        pytest.skip("fish is not installed")
    backend.program = lambda: [fish, "--no-config", "-i"]
    app = backend.attach(kind="vte", cols=120, rows=45)
    try:
        app.pump(0.3)
        app.send(
            b"for n in (seq 1 150); printf 'row-%03d %s\\n' $n "
            b"'abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz"
            b"abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz'; end; "
            b"printf 'HISTORY-END\\n'\r"
        )
        app.until(lambda state: b"HISTORY-END" in state["screen"])
        app.pump(0.15)
        app.send(b"\x1b[<64;2;8M" * 8)
        app.until(lambda state: int(backend.field("scroll_position") or b"0") >= 40)
        top = first_line(app)
        assert re.match(rb"row-\d+", top)
        for cols, rows in ((80, 45), (120, 45), (120, 25), (120, 65), (120, 45)):
            resized(app, backend, cols, rows)
            assert first_line(app).startswith(top[:20])
        assert backend.field("scroll_position") != b"0"
    finally:
        app.close()
