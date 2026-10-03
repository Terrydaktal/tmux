import base64
import re
import shlex

import pytest
from conftest import mosh
from terminal_harness import Attachment


def history(backend, app):
    app.until(lambda s: b"READY" in s["screen"])
    app.send(b"H")
    app.until(lambda s: b"HISTORY-END" in s["screen"])


def scroll(backend, app):
    app.send(b"\x1b[<64;2;8M" * 8)
    app.until(
        lambda s: b"history-02" in s["screen"] and b"HISTORY-END" not in s["screen"]
    )
    assert int(backend.field("scroll_position")) > 24
    assert not re.search(rb"\[\d+/\d+\]", app.term.state["screen"])


ORIGINAL_CLIPBOARD = b"clipboard-before-selection"


def prepare_clipboard(backend, app, kind):
    copied = backend.socket.parent / "copied"
    backend.run("set-buffer", ORIGINAL_CLIPBOARD.decode())
    backend.run("set-option", "-s", "copy-command", f"cat > {shlex.quote(str(copied))}")
    if kind == "termux":
        app.term.feed(b"\x1b]52;c;" + base64.b64encode(ORIGINAL_CLIPBOARD) + b"\x07")
    return copied


def assert_not_copied(backend, app, copied, kind):
    assert backend.run("show-buffer").stdout == ORIGINAL_CLIPBOARD
    assert not copied.exists()
    assert b"\x1b]52;" not in app.wire
    if kind == "termux":
        assert base64.b64decode(app.term.state["clipboard"]) == ORIGINAL_CLIPBOARD


@pytest.mark.parametrize(
    "payload",
    [
        b"f",
        b"q",
        b"/",
        b"\x03",
        b"\x02",
        b"\x14",
        b"\x16",
        b"\r",
        b"\x1b[A",
        "\u4e2d\u6587".encode(),
        b"\x1b[200~a pasted line\nsecond line\x1b[201~",
    ],
)
def test_typing_and_paste_leave_history_without_losing_input(backend, payload):
    app = backend.attach()
    try:
        history(backend, app)
        scroll(backend, app)
        before = backend.received()
        app.send(payload)
        app.until(lambda s: backend.received() == before + payload)
        assert backend.field("pane_in_mode") == b"0"
    finally:
        app.close()


@pytest.mark.parametrize("kind", ["termux", "vte"])
@pytest.mark.parametrize("copy_key", [b"\x03", b"\x1b[99;5u", b"\x1b[27;5;99~"])
def test_selection_only_copies_on_ctrl_c_and_keeps_position(backend, kind, copy_key):
    app = backend.attach(kind)
    try:
        history(backend, app)
        scroll(backend, app)
        copied = prepare_clipboard(backend, app, kind)
        position = backend.field("scroll_position")
        screen = app.term.state["screen"]
        app.send(b"\x1b[<0;1;5M\x1b[<32;12;5M\x1b[<0;12;5m")
        app.until(
            lambda s: (
                backend.field("selection_present") == b"1"
                and backend.field("selection_active") == b"0"
            )
        )
        app.pump(0.2)
        assert_not_copied(backend, app, copied, kind)
        before = backend.received()
        app.send(copy_key)
        app.until(lambda s: copied.exists() and b"history-" in copied.read_bytes())
        selected = backend.run("show-buffer").stdout
        assert b"history-" in selected
        assert copied.read_bytes() == selected
        app.pump(0.35)
        assert backend.field("pane_in_mode") == b"1"
        assert backend.field("selection_present") == b"1"
        assert backend.field("scroll_position") == position
        assert app.term.state["screen"] == screen
        assert backend.received() == before
        if kind == "termux":
            assert b"\x1b]52;" in app.wire, (
                backend.run("show-options", "-sv", "set-clipboard").stdout,
                backend.run("show-options", "-gv", "scroll-on-input").stdout,
                [
                    line
                    for line in backend.run("info").stdout.splitlines()
                    if b"Ms:" in line
                ],
                backend.run(
                    "list-clients", "-F", "#{client_name} #{client_flags}"
                ).stdout,
            )
            app.until(lambda s: base64.b64decode(s["clipboard"]) == selected)
        # Neutral selection is configured, not the upstream orange mode style.
        assert (
            backend.run(
                "show-options", "-gv", "copy-mode-selection-style"
            ).stdout.strip()
            == b"fg=default,bg=default,reverse"
        )
    finally:
        app.close()


@pytest.mark.parametrize("clicks", [2, 3])
@pytest.mark.parametrize("in_history", [False, True])
def test_multiple_clicks_only_select_and_ctrl_c_copies(backend, clicks, in_history):
    app = backend.attach()
    try:
        history(backend, app)
        if in_history:
            scroll(backend, app)
        copied = prepare_clipboard(backend, app, "termux")
        position = backend.field("scroll_position")
        for _ in range(clicks):
            app.send(b"\x1b[<0;2;5M\x1b[<0;2;5m")
            app.pump(0.05)
        app.until(lambda s: backend.field("selection_present") == b"1")
        assert_not_copied(backend, app, copied, "termux")
        app.send(b"\x03")
        app.until(lambda s: copied.exists())
        selected = backend.run("show-buffer").stdout
        assert b"history" in selected
        assert (b"-" in selected) == (clicks == 3)
        assert copied.read_bytes() == selected
        assert backend.field("pane_in_mode") == b"1"
        assert backend.field("scroll_position") == (position or b"0")
    finally:
        app.close()


@pytest.mark.parametrize("payload", [b"f", b"\r", b"\x14", b"\x16"])
def test_typing_after_selection_does_not_copy(backend, payload):
    app = backend.attach()
    try:
        history(backend, app)
        scroll(backend, app)
        copied = prepare_clipboard(backend, app, "termux")
        app.send(b"\x1b[<0;1;5M\x1b[<32;12;5M\x1b[<0;12;5m")
        app.until(lambda s: backend.field("selection_present") == b"1")
        before = backend.received()
        app.send(payload)
        app.until(lambda s: backend.received() == before + payload)
        assert backend.field("pane_in_mode") == b"0"
        assert_not_copied(backend, app, copied, "termux")
    finally:
        app.close()


def test_right_click_does_not_copy_or_move_history(backend):
    app = backend.attach()
    try:
        history(backend, app)
        scroll(backend, app)
        copied = prepare_clipboard(backend, app, "termux")
        position = backend.field("scroll_position")
        app.send(b"\x1b[<0;1;5M\x1b[<32;12;5M\x1b[<0;12;5m")
        app.until(lambda s: backend.field("selection_present") == b"1")
        app.send(b"\x1b[<2;5;5M\x1b[<2;5;5m")
        app.pump(0.2)
        assert_not_copied(backend, app, copied, "termux")
        assert backend.field("pane_in_mode") == b"1"
        assert backend.field("scroll_position") == position
    finally:
        app.close()


@pytest.mark.parametrize("kind", ["termux", "vte"])
def test_plain_application_header_click_and_wheel_pass_through(backend, kind):
    app = backend.attach(kind)
    try:
        app.until(lambda s: b"READY" in s["screen"])
        app.send(b"A")
        app.until(lambda s: b"CLICK-HEADER" in s["screen"])
        if kind == "termux":
            assert app.term.state["mouse"]
        before = backend.received()
        payload = b"\x1b[<0;3;1M\x1b[<0;3;1m\x1b[<64;4;8M"
        app.send(payload)
        app.until(lambda s: backend.received() == before + payload)
        assert backend.field("pane_in_mode") == b"0"
    finally:
        app.close()


def test_wheel_down_returns_to_live_view_and_focus_does_not(backend):
    app = backend.attach()
    try:
        history(backend, app)
        scroll(backend, app)
        position = backend.field("scroll_position")
        app.send(b"\x1b[I\x1b[O")
        app.pump(0.2)
        assert backend.field("scroll_position") == position
        app.send(b"\x1b[<65;2;8M" * 10)
        app.until(lambda s: b"HISTORY-END" in s["screen"])
        assert backend.field("pane_in_mode") == b"0"
    finally:
        app.close()


def test_shift_page_browses_but_ordinary_arrows_are_input(backend):
    app = backend.attach()
    try:
        history(backend, app)
        app.send(b"\x1b[5;2~")
        app.until(lambda s: backend.field("pane_in_mode") == b"1")
        before = int(backend.field("scroll_position"))
        app.send(b"\x1b[5;2~")
        app.until(lambda s: int(backend.field("scroll_position") or b"0") > before)
        app.send(b"\x1b[B")
        app.until(lambda s: backend.received().endswith(b"\x1b[B"))
        assert backend.field("pane_in_mode") == b"0"
    finally:
        app.close()


def test_readonly_viewer_cannot_exit_history_or_deliver_input(backend):
    app = backend.attach()
    readonly = None
    try:
        history(backend, app)
        readonly = Attachment(
            backend, argv=[*backend.prefix, "attach-session", "-r", "-t", "=test"]
        )
        readonly.until(lambda s: b"HISTORY-END" in s["screen"])
        scroll(backend, app)
        position = backend.field("scroll_position")
        before = backend.received()
        readonly.send(b"f\x1b[200~not-delivered\x1b[201~")
        readonly.pump(0.3)
        assert backend.received() == before
        assert backend.field("scroll_position") == position
    finally:
        if readonly:
            readonly.close()
        app.close()


@pytest.mark.parametrize("kind", ["termux", "vte"])
@pytest.mark.parametrize("lossy", [False, True])
def test_stock_mosh_history_copy_typing_resize_and_reattach(backend, kind, lossy):
    desktop = backend.attach(kind, cols=100, rows=30)
    remote = None
    try:
        history(backend, desktop)
        remote = mosh.Session(
            backend.socket.parent,
            kind,
            lossy,
            program=backend.cli("attach-session", "-t", "=test"),
        )
        app = remote.attachment
        app.until(lambda s: b"HISTORY-END" in s["screen"])
        assert b"history-0000" not in app.term.state["text"]
        scroll(backend, app)
        copied = prepare_clipboard(backend, app, kind)
        app.send(b"\x1b[<0;1;5M\x1b[<32;12;5M\x1b[<0;12;5m")
        app.until(
            lambda s: (
                backend.field("selection_present") == b"1"
                and backend.field("selection_active") == b"0"
            )
        )
        assert_not_copied(backend, app, copied, kind)
        app.send(b"\x03")
        app.until(lambda s: copied.exists() and b"history-" in copied.read_bytes())
        selected = backend.run("show-buffer").stdout
        assert copied.read_bytes() == selected
        if kind == "termux":
            app.until(lambda s: base64.b64decode(s["clipboard"]) == selected)
        assert int(backend.field("scroll_position")) > 24
        # Retrieve history older than the first screen through unmodified Mosh.
        app.send(b"\x1b[<64;2;8M" * 70)
        app.until(lambda s: b"history-0000" in s["screen"])
        app.send(b"f")
        app.until(lambda s: backend.received().endswith(b"f"))
        assert backend.field("pane_in_mode") == b"0"
        app.resize(45, 12)
        app.until(lambda s: b"SIZE=45x12" in backend.log.read_bytes())
        app.resize(45, 24)
        app.until(lambda s: b"SIZE=45x24" in backend.log.read_bytes())
        if lossy:
            remote.link.paused = True
            app.send(b"outage-input")
            app.pump(0.2)
            remote.link.roam = True
            remote.link.paused = False
            app.until(
                lambda s: backend.received().endswith(b"outage-input"), timeout=12
            )
        assert len(backend.run("list-clients").stdout.splitlines()) == 2
        remote.close()
        remote = None
        assert backend.run("has-session", "-t", "=test").returncode == 0
        remote = mosh.Session(
            backend.socket.parent,
            kind,
            program=backend.cli("attach-session", "-t", "=test"),
        )
        remote.attachment.until(lambda s: b"HISTORY-END" in s["screen"])
        assert len(backend.run("list-clients").stdout.splitlines()) == 2
    finally:
        if remote:
            remote.close()
        desktop.close()
