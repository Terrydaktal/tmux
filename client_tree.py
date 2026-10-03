"""Read-only, expandable session inventory for tmux-mosh clients."""

import curses
import os
import signal
import subprocess
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

import tmux_clients as clients

HEADERS = (
    "",
    "TYPE",
    "SESSION",
    "APP",
    "CONNS",
    "IDLE",
    "PEER",
    "SIZE",
    "SIZING",
)
MIN_WIDTHS = (4, 6, 7, 10, 5, 6, 6, 7, 6)
MAX_WIDTHS = (4, 10, 64, 40, 5, 10, 39, 11, 10)
BODY_START = 3
REFRESH_INTERVAL = 1.0


def clean(value):
    return "".join(c if c.isprintable() else "?" for c in str(value or ""))[:4096]


def cell_width(value):
    return sum(
        0
        if unicodedata.combining(c)
        else 2
        if unicodedata.east_asian_width(c) in "WF"
        else 1
        for c in value
    )


def clip(value, width):
    if width <= 0:
        return ""
    if cell_width(value) <= width:
        return value
    suffix = "..." if width > 3 else ""
    result, used = "", 0
    for char in value:
        size = cell_width(char)
        if used + size > width - len(suffix):
            break
        result += char
        used += size
    return result + suffix


@dataclass(frozen=True)
class Node:
    key: tuple
    cells: tuple
    children: tuple = ()
    highlights: tuple = ()


@dataclass(frozen=True)
class Row:
    node: Node
    parent: tuple | None
    cells: tuple

    @property
    def highlights(self):
        columns = (2, 1, 3, 4, 5, 6, 7, 8)
        return tuple((columns[column], label) for column, label in self.node.highlights)


def connection(entry, key, *, app=True):
    via = entry["via"].lower()
    kind = entry["type"].lower() if app or entry["type"] == "OPSEC-TMUX" else ""
    cells = (
        via,
        kind,
        clients.format_process(entry.get("app_label") or entry["app"], entry["app_pid"])
        if app
        else "",
        "",
        clients.format_age(entry["idle_seconds"]),
        entry["peer"] or "-",
        f"{entry['width']}x{entry['height']}"
        if entry["width"] and entry["height"]
        else "-",
        clients.format_sizing(entry),
    )
    label = via.split(" ", 1)[0]
    highlights = []
    if (label == "mosh" and entry.get("frontend_outdated") is not False) or (
        label == "xfce4-terminal" and entry.get("frontend_outdated") is True
    ):
        highlights.append((0, label))
    if kind.endswith("tmux") and entry.get("tmux_outdated") is True:
        highlights.append((1, kind))
    return Node(
        key, tuple(clean(v).lower() for v in cells), highlights=tuple(highlights)
    )


def entry_for_client(result, row):
    subset = result | {
        "clients": [row],
        "sessions": [],
        "other_mosh_sessions": [],
        "other_ssh_sessions": [],
        "other_opsec_sessions": [],
    }
    return clients.unified_entries(subset)[0]


def build_tree(result):
    sessions = {row["id"]: row for row in result["sessions"]}
    viewers = {}
    for row in result["clients"]:
        identity = row["session_id"]
        viewers.setdefault(identity, []).append(row)
        if identity not in sessions:
            sessions[identity] = row | {"id": identity, "name": row["session"]}
    roots = []
    for identity, session in sorted(
        sessions.items(), key=lambda item: item[1]["name"].casefold()
    ):
        key = ("tmux", identity)
        children = []
        for row in viewers.get(identity, []):
            child = connection(
                entry_for_client(result, row),
                (*key, row.get("pid"), row.get("created_at"), row.get("tty")),
                app=False,
            )
            guest = row.get("opsec_connection", {}).get("session")
            if guest or child.cells[1] == "opsec-tmux":
                destination = "opsec-tmux" + (
                    " " + clean(guest).lower() if guest else ""
                )
                child = replace(
                    child,
                    cells=(
                        child.cells[0] + " -> " + destination,
                        "",
                        "",
                        *child.cells[3:],
                    ),
                    highlights=tuple(
                        (0 if column == 1 else column, label)
                        for column, label in child.highlights
                    ),
                )
            children.append(child)
        count = max(len(children), session.get("attached_clients", 0))
        if count and not children:
            children.append(
                Node(
                    (*key, "unavailable"),
                    ("viewer details unavailable", "", "", "", "-", "-", "-", "-"),
                )
            )
        cells = (
            clean(session["name"]).lower(),
            "tmux",
            clean(
                clients.format_process(
                    session.get("app_label") or session.get("app"),
                    session.get("app_pid"),
                )
            ).lower(),
            str(count),
            "",
            "",
            "",
            "",
        )
        marks = ((1, "tmux"),) if result.get("server_binary_outdated") is True else ()
        roots.append(Node(key, cells, tuple(children), marks))

    remote = result | {"clients": [], "sessions": []}
    raw_rows = [
        row
        for name in (
            "other_mosh_sessions",
            "other_ssh_sessions",
            "other_opsec_sessions",
        )
        for row in result.get(name, [])
    ]
    remote_info = {(row["app_pid"], row["tty"]): row for row in raw_rows}
    guests, direct = {}, []
    for entry in clients.unified_entries(remote):
        info = remote_info.get((entry["app_pid"], entry["tty"]), {})
        child = connection(
            entry,
            (
                "remote",
                entry["transport"],
                entry.get("mosh_pid") or info.get("pid") or entry["app_pid"],
                entry["tty"],
            ),
        )
        if entry["type"] == "OPSEC-TMUX" and entry["session"]:
            guest = info.get("opsec_connection", {})
            key = (
                "opsec-tmux",
                guest.get("target"),
                guest.get("server_socket"),
                entry["session"],
            )
            child = replace(
                child,
                cells=(child.cells[0], "", "", *child.cells[3:]),
                highlights=tuple(mark for mark in child.highlights if mark[0] != 1),
            )
            guests.setdefault(key, []).append((entry, child))
        else:
            viewer = replace(
                child,
                key=(*child.key, "viewer"),
                cells=(child.cells[0], "", "", *child.cells[3:]),
                highlights=tuple(mark for mark in child.highlights if mark[0] != 1),
            )
            direct.append(
                Node(
                    child.key,
                    (
                        clean(entry.get("session") or "-").lower(),
                        child.cells[1],
                        child.cells[2],
                        "1",
                        "",
                        "",
                        "",
                        "",
                    ),
                    (viewer,),
                    tuple(mark for mark in child.highlights if mark[0] == 1),
                )
            )
    for key, members in sorted(guests.items(), key=lambda item: item[0][-1].casefold()):
        entry = members[0][0]
        app = clean(entry.get("app_label") or entry["app"] or "-").lower()
        pids = sorted(
            {
                e["app_pid"]
                for e, _ in members
                if type(e["app_pid"]) is int and e["app_pid"] > 0
            }
        )
        if pids:
            # These identify host attachment launchers, not an inferred guest PID.
            app += " (" + ", ".join(str(pid) for pid in pids) + ")"
        cells = (
            clean(key[-1]).lower(),
            "opsec-tmux",
            app,
            str(len(members)),
            "",
            "",
            "",
            "",
        )
        marks = (
            ((1, "opsec-tmux"),)
            if any(e.get("tmux_outdated") is True for e, _ in members)
            else ()
        )
        roots.append(Node(key, cells, tuple(child for _, child in members), marks))
    roots.extend(direct)
    return roots


def visible_rows(roots, expanded):
    rows = []
    for root in roots:
        opened = root.key in expanded
        marker = "[-]" if root.children and opened else "[+]" if root.children else ""
        rows.append(
            Row(
                root,
                None,
                (
                    marker,
                    root.cells[1],
                    root.cells[0],
                    root.cells[2],
                    *root.cells[3:],
                ),
            )
        )
        if opened:
            for index, child in enumerate(root.children):
                branch = (
                    "\u2514\u2500\u2500"
                    if index == len(root.children) - 1
                    else "\u251c\u2500\u2500"
                )
                rows.append(
                    Row(
                        child,
                        root.key,
                        child_cells(child, branch),
                    )
                )
    return rows


def display_indices(rows):
    """Map display lines to selectable rows, with a spacer after each child list."""
    indices = []
    for index, row in enumerate(rows):
        indices.append(index)
        if row.parent is not None and (
            index + 1 == len(rows) or rows[index + 1].parent != row.parent
        ):
            indices.append(None)
    return indices


def child_cells(child, branch=""):
    return (
        branch,
        child.cells[1],
        child.cells[0],
        child.cells[2],
        *child.cells[3:],
    )


def layout(rows, width=None):
    gap = 2 if width is None or width >= 100 else 1
    parents = [row for row in rows if row.parent is None]
    samples = [row.cells for row in parents]
    # Hidden children still determine widths, so folding cannot move the columns.
    samples.extend(child_cells(child) for row in parents for child in row.node.children)
    widths = [
        min(
            MAX_WIDTHS[i],
            max([cell_width(HEADERS[i]), *(cell_width(cells[i]) for cells in samples)]),
        )
        for i in range(len(HEADERS))
    ]
    # Keep the marker gutter stable when groups are collapsed or empty.
    widths[0] = MAX_WIDTHS[0]
    if width is not None:
        excess = sum(widths) + gap * (len(widths) - 1) - max(0, width)
        for i in (3, 6, 2, 1, 8, 7, 5, 4):
            reduction = min(max(0, excess), max(0, widths[i] - MIN_WIDTHS[i]))
            widths[i] -= reduction
            excess -= reduction
        # Very narrow terminals retain the leftmost fields, never wrap a row.
        remaining = max(0, width)
        for i, size in enumerate(widths):
            widths[i] = min(size, remaining)
            remaining = max(0, remaining - widths[i] - gap)
    return widths, gap


def line(cells, widths, gap, highlights=(), *, spans=None):
    marks = dict(highlights)
    output = []
    offset = 0
    for column, (value, width) in enumerate(zip(cells, widths)):
        if not width:
            continue
        value = clean(value)
        text = clip(value, width)
        padding = " " * (width - cell_width(text))
        label = marks.get(column)
        if label and label in value:
            start = value.index(label)
            end = min(len(text), start + len(label))
            if start < end:
                if spans is not None:
                    spans.append((offset + start, text[start:end]))
                else:
                    text = (
                        text[:start]
                        + "\033[41m"
                        + text[start:end]
                        + "\033[0m"
                        + text[end:]
                    )
        output.append(text + padding)
        offset += len(text) + len(padding) + gap
    return (" " * gap).join(output).rstrip()


def render_row(row, widths, gap, *, width=None):
    """Return text and warning spans indexed by string offsets, not display cells."""
    spans = []
    if row.parent is not None and not row.cells[1] and not row.cells[3]:
        # Session viewers share one tree-label area, not a separate viewer column.
        cells = ("", row.cells[1], "", row.cells[3], *row.cells[4:])
        marks = tuple(
            (column, label) for column, label in row.highlights if column != 2
        )
        text = line(cells, widths, gap, marks, spans=spans)
        start = widths[0] + gap
        end = sum(widths[:4]) + gap * 4
        raw_label = clean(row.cells[0] + " " + row.cells[2])
        label = clip(raw_label, max(0, end - start - gap))
        text = text.ljust(end)
        used = cell_width(label)
        text = text[:start] + label + text[start + used :]
        spans = [
            (offset + len(label) - used if offset >= start + used else offset, value)
            for offset, value in spans
        ]
        for column, value in row.highlights:
            if column == 2 and value in raw_label:
                offset = raw_label.index(value)
                if offset < len(label):
                    spans.append((start + offset, label[offset : offset + len(value)]))
        text = text.rstrip()
    else:
        text = line(row.cells, widths, gap, row.highlights, spans=spans)
    if width is not None:
        text = clip(text, width)
    spans = [
        (start, text[start : start + len(label)])
        for start, label in spans
        if start < len(text)
    ]
    return text, spans


def paint(text, spans):
    for start, label in sorted(spans, reverse=True):
        text = (
            text[:start] + "\033[41m" + label + "\033[0m" + text[start + len(label) :]
        )
    return text


def summary(result):
    observed = time.strftime(
        "%Y-%m-%d %H:%M:%S %Z", time.localtime(result["snapshot_at"])
    )
    return f"{observed} | {len(result['sessions'])} tmux sessions, {len(result['clients'])} connections"


def print_snapshot(result, *, width=None, color=False):
    roots = build_tree(result)
    rows = visible_rows(roots, {root.key for root in roots})
    widths, gap = layout(rows, width)
    print("Snapshot: " + summary(result))
    print(
        "Sizing monitor: " + ("running" if result["monitor_running"] else "not running")
    )
    print(line(HEADERS, widths, gap))
    for index in display_indices(rows):
        if index is None:
            print()
            continue
        text, spans = render_row(rows[index], widths, gap, width=width)
        print(paint(text, spans) if color else text)


class TreeState:
    def __init__(self, result):
        self.expanded = set()
        self.selected = 0
        self.offset = 0
        self.update(result)

    def update(self, result):
        previous = (
            self.rows[self.selected] if hasattr(self, "rows") and self.rows else None
        )
        known = {root.key for root in getattr(self, "roots", ())}
        self.result = result
        self.roots = build_tree(result)
        present = {root.key for root in self.roots}
        self.expanded.update(present - known)
        self.expanded.intersection_update(present)
        self.rebuild()
        if previous:
            candidates = (previous.node.key, previous.parent)
            self.selected = next(
                (
                    i
                    for key in candidates
                    for i, row in enumerate(self.rows)
                    if row.node.key == key
                ),
                min(self.selected, max(0, len(self.rows) - 1)),
            )

    def rebuild(self):
        self.rows = visible_rows(self.roots, self.expanded)
        self.line_rows = display_indices(self.rows)
        self.selected = min(self.selected, max(0, len(self.rows) - 1))

    def keep_visible(self, height):
        height = max(1, height)
        selected_line = self.line_rows.index(self.selected) if self.rows else 0
        self.offset = min(self.offset, max(0, len(self.line_rows) - height))
        self.offset = min(self.offset, selected_line)
        self.offset = max(self.offset, selected_line - height + 1)

    def key(self, key, height):
        if key in (ord("q"), 27):
            return False
        if not self.rows:
            return True
        current = self.rows[self.selected]
        if key in (curses.KEY_DOWN, ord("j")):
            self.selected = min(len(self.rows) - 1, self.selected + 1)
        elif key in (curses.KEY_UP, ord("k")):
            self.selected = max(0, self.selected - 1)
        elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
            direction = 1 if key == curses.KEY_NPAGE else -1
            target = self.line_rows.index(self.selected) + max(1, height) * direction
            target = max(0, min(self.line_rows.index(len(self.rows) - 1), target))
            while self.line_rows[target] is None:
                target += direction
            self.selected = self.line_rows[target]
        elif key in (curses.KEY_HOME, curses.KEY_END):
            self.selected = 0 if key == curses.KEY_HOME else len(self.rows) - 1
        elif key in (10, 13, curses.KEY_ENTER, ord(" ")) and current.node.children:
            if current.node.key in self.expanded:
                self.expanded.remove(current.node.key)
            else:
                self.expanded.add(current.node.key)
            self.rebuild()
        elif key == curses.KEY_RIGHT and current.node.children:
            if current.node.key in self.expanded:
                self.selected += 1
            else:
                self.expanded.add(current.node.key)
                self.rebuild()
        elif key == curses.KEY_LEFT:
            if current.node.key in self.expanded:
                self.expanded.remove(current.node.key)
                self.rebuild()
            elif current.parent:
                self.selected = next(
                    i
                    for i, row in enumerate(self.rows)
                    if row.node.key == current.parent
                )
        elif key in (ord("e"), ord("c")):
            self.expanded = (
                {root.key for root in self.roots} if key == ord("e") else set()
            )
            self.rebuild()
            self.selected = next(
                (
                    i
                    for i, row in enumerate(self.rows)
                    if row.node.key in (current.node.key, current.parent)
                ),
                0,
            )
        self.keep_visible(height)
        return True

    def mouse(self, y, buttons, height):
        if buttons & curses.BUTTON4_PRESSED:
            self.selected = max(0, self.selected - 5)
        elif buttons & getattr(curses, "BUTTON5_PRESSED", 0):
            self.selected = min(max(0, len(self.rows) - 1), self.selected + 5)
        elif buttons & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
            display_line = self.offset + y - BODY_START
            if not BODY_START <= y < BODY_START + height or not 0 <= display_line < len(
                self.line_rows
            ):
                return
            index = self.line_rows[display_line]
            if index is None:
                return
            self.selected = index
            self.key(10, height)
        self.keep_visible(height)


def draw(screen, state, *, warning="", red=0):
    height, width = screen.getmaxyx()
    room = max(0, height - BODY_START - 1)
    state.keep_visible(room)
    widths, gap = layout(state.rows, max(0, width - 1))
    screen.erase()

    def write(y, x, value, attribute=0):
        if 0 <= y < height and 0 <= x < width - 1:
            text = clip(clean(value), width - x - 1)
            if text:
                try:
                    screen.addstr(y, x, text, attribute)
                except curses.error:
                    pass  # A resize can invalidate coordinates between queries.

    write(0, 0, "LIVE " + summary(state.result), curses.A_BOLD)
    write(
        1,
        0,
        "Sizing monitor: "
        + ("running" if state.result["monitor_running"] else "not running"),
    )
    write(2, 0, line(HEADERS, widths, gap), curses.A_BOLD)
    for display_line in range(
        state.offset, min(len(state.line_rows), state.offset + room)
    ):
        index = state.line_rows[display_line]
        if index is None:
            continue
        row = state.rows[index]
        y = BODY_START + display_line - state.offset
        attr = curses.A_REVERSE if index == state.selected else 0
        text, spans = render_row(row, widths, gap, width=max(0, width - 1))
        write(y, 0, text, attr)
        if red:
            for start, label in spans:
                write(y, cell_width(text[:start]), label, red | curses.A_BOLD)
    if not state.rows and room:
        write(BODY_START, 0, "No sessions or connections.")
    controls = (
        "q Quit | Enter/Click Fold | Arrows Move | e Expand | c Collapse | r Refresh"
    )
    write(height - 1, 0, warning or controls, curses.A_BOLD)
    screen.refresh()


def run_screen(screen, load, initial, *, interval=REFRESH_INTERVAL):
    state = TreeState(initial)
    screen.timeout(100)
    curses.set_escdelay(25)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    mask = (
        curses.BUTTON1_PRESSED
        | curses.BUTTON1_CLICKED
        | curses.BUTTON4_PRESSED
        | getattr(curses, "BUTTON5_PRESSED", 0)
    )
    _, old_mask = curses.mousemask(mask)
    curses.mouseinterval(0)
    red = 0
    if "NO_COLOR" not in os.environ and curses.has_colors():
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_RED)
        red = curses.color_pair(1)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="client-inventory")
    pending, warning = None, ""
    next_refresh, dirty = time.monotonic() + interval, True
    try:
        while True:
            now = time.monotonic()
            if pending is not None and pending.done():
                try:
                    state.update(pending.result())
                    warning = ""
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    warning = "Refresh failed: " + clean(exc)
                pending, dirty = None, True
            if pending is None and now >= next_refresh:
                pending = executor.submit(load)
                next_refresh = now + interval
            if dirty:
                draw(screen, state, warning=warning, red=red)
                dirty = False
            key = screen.getch()
            if key == -1:
                continue
            dirty = True
            room = max(1, screen.getmaxyx()[0] - BODY_START - 1)
            if key == curses.KEY_MOUSE:
                try:
                    _, _, y, _, buttons = curses.getmouse()
                    state.mouse(y, buttons, room)
                except curses.error:
                    pass
            elif key == ord("r"):
                next_refresh = 0
            elif not state.key(key, room):
                break
    finally:
        curses.mousemask(old_mask)
        executor.shutdown(wait=False, cancel_futures=True)


def watch(load, initial):
    def interrupted(*_):
        raise KeyboardInterrupt

    previous = {
        signum: signal.signal(signum, interrupted)
        for signum in (signal.SIGTERM, signal.SIGHUP)
    }
    try:
        curses.wrapper(run_screen, load, initial)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
