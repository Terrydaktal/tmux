"""Real VTE under Xvfb. No real desktop or clipboard is contacted."""

import base64
import json
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, GLib, Gtk, Pango, Vte  # noqa: E402

cols, rows = map(int, sys.argv[1:3])
window = Gtk.Window()
term = Vte.Terminal()
term.set_allow_hyperlink(True)
term.set_font(Pango.FontDescription("Monospace 10"))
term.set_scrollback_lines(10000)
window.add(term)
term.set_size(cols, rows)
window.show_all()
replies = bytearray()
term.connect("commit", lambda _term, text, _size: replies.extend(text.encode()))


def settle():
    loop = GLib.MainLoop()
    GLib.timeout_add(35, lambda: (loop.quit(), False)[1])
    loop.run()


def geometry():
    padding = term.get_style_context().get_padding(Gtk.StateFlags.NORMAL)
    width = cols * term.get_char_width() + padding.left + padding.right
    height = rows * term.get_char_height() + padding.top + padding.bottom
    term.set_size(cols, rows)
    window.resize(width, height)
    settle()
    assert (term.get_column_count(), term.get_row_count()) == (cols, rows)


settle()
geometry()
for line in sys.stdin:
    fields = line.rstrip("\n").split(" ")
    link = None
    if fields[0] == "FEED":
        term.feed(base64.b64decode(fields[1]))
    elif fields[0] == "SIZE":
        cols, rows = map(int, fields[1:3])
        geometry()
    elif fields[0] == "LINK":
        column, row = map(int, fields[1:3])
        padding = term.get_style_context().get_padding(Gtk.StateFlags.NORMAL)
        event = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
        event.window = term.get_window()
        for child in term.get_window().get_children():
            if child.get_window_type() == Gdk.WindowType.CHILD:
                event.window = child
                break
        event.x = padding.left + (column + 0.5) * term.get_char_width()
        event.y = padding.top + (row + 0.5) * term.get_char_height()
        link = term.hyperlink_check_event(event)
    settle()
    assert (term.get_column_count(), term.get_row_count()) == (cols, rows), (
        "oracle geometry changed unexpectedly"
    )
    adjustment = term.get_vadjustment()
    bottom = int(adjustment.get_upper())
    text, _ = term.get_text_range_format(
        Vte.Format.TEXT, int(adjustment.get_lower()), 0, bottom - 1, cols
    )
    x, y = term.get_cursor_position()
    # VTE's adjustment can describe the saved main-screen history while the
    # alternate screen is active. Ask VTE for its actual visible viewport.
    screen = term.get_text_format(Vte.Format.TEXT)
    physical = [
        term.get_text_range_format(Vte.Format.TEXT, row, 0, row, cols)[0]
        for row in range(max(0, bottom - rows), bottom)
    ]
    print(
        json.dumps(
            {
                "text": base64.b64encode(text.encode()).decode(),
                "screen": base64.b64encode(screen.encode()).decode(),
                "replies": base64.b64encode(replies).decode(),
                "lines": [base64.b64encode(row.encode()).decode() for row in physical],
                "x": x,
                "y": y,
                "cols": term.get_column_count(),
                "rows": term.get_row_count(),
                "link": link,
            }
        ),
        flush=True,
    )
    replies.clear()
