"""Session grouping and expandable inventory without touching user sessions."""

import base64
import curses
import json
import os
import re
import signal
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from terminal_harness import Attachment
from test_inventory_table import snapshot as snapshot_fixture

import client_tree as tree
import tmux_clients as clients


@pytest.fixture
def snapshot():
    return snapshot_fixture.__wrapped__()


@pytest.fixture
def vm_snapshot(snapshot):
    base = snapshot["other_mosh_sessions"][0]
    snapshot["other_mosh_sessions"] = []
    snapshot["other_opsec_sessions"] = [
        base
        | {
            "pid": 100 + index,
            "app_pid": 200 + index,
            "mode": "OPSEC-TMUX",
            "transport": transport,
            "frontend": frontend,
            "frontend_outdated": False,
            "app_label": "pi-opsec",
            "idle_seconds": 12 if transport == "MOSH" else None,
            "peer": None if transport == "LOCAL" else "100.70.36.28",
            "opsec_connection": {
                "session": "pi-live",
                "target": "opsec-qwen",
                "server_socket": "/run/user/1000/guest.sock",
                "server_binary_outdated": False,
            },
        }
        for index, (transport, frontend) in enumerate(
            [("LOCAL", "xfce4-terminal"), ("MOSH", "mosh"), ("SSH", "ssh")]
        )
    ]
    return snapshot


def test_session_roots_show_one_app_and_connection_count(snapshot):
    before = deepcopy(snapshot)
    roots = tree.build_tree(snapshot)
    assert [root.cells[0] for root in roots] == [
        "changing",
        "quiet",
        "work",
        "-",
        "-",
        "-",
    ]
    work = next(root for root in roots if root.cells[0] == "work")
    assert work.cells[1:4] == ("tmux", "codex (11)", "2")
    assert [child.cells[0] for child in work.children] == ["xfce4-terminal", "mosh"]
    assert all(child.cells[2] == "" for child in work.children)
    assert work.children[0].cells[4:] == ("-", "-", "120x40", "active")
    assert work.children[1].cells[4:] == ("1m01s", "-", "120x40", "auto-off")
    quiet = next(root for root in roots if root.cells[0] == "quiet")
    assert quiet.cells[2:4] == ("python (12)", "0") and not quiet.children
    assert snapshot == before


def test_standalone_and_unknown_tmux_connections_are_not_lost(snapshot):
    connections = tree.build_tree(snapshot)[3:]
    assert len(connections) == 3
    assert all(len(node.children) == 1 and node.cells[3] == "1" for node in connections)
    assert {(n.cells[0], n.cells[1], n.cells[2]) for n in connections} == {
        ("-", "direct", "fish (31)"),
        ("-", "direct", "fish (51)"),
        ("-", "other-tmux", "tmux (41)"),
    }
    assert [node.children[0].cells[0] for node in connections] == [
        "mosh [recent]",
        "ssh",
        "mosh [recent]",
    ]


@pytest.mark.parametrize("fold", [None, "c", "e"])
def test_direct_parents_stay_visible_while_transport_children_can_fold(snapshot, fold):
    state = tree.TreeState(snapshot)
    if fold:
        state.key(ord(fold), 20)
    rows = [
        row for row in state.rows if row.parent is None and row.node.key[0] == "remote"
    ]
    assert len(rows) == 3
    assert all(row.cells[0] == ("[+]" if fold == "c" else "[-]") for row in rows)
    assert sorted(row.cells[1] for row in rows) == ["direct", "direct", "other-tmux"]
    assert all(row.cells[3] and row.cells[4] == "1" for row in rows)
    for row in rows:
        children = [child for child in state.rows if child.parent == row.node.key]
        assert len(children) == (0 if fold == "c" else 1)
        if children:
            assert children[0].cells[0] == "└──"
            assert children[0].cells[4] == "" and children[0].cells[7] == "110x77"
    assert not any(row.cells[2] == "other connections" for row in state.rows)


@pytest.mark.parametrize("width", [80, 140, 240])
def test_top_level_connections_align_with_session_rows(snapshot, capsys, width):
    state = tree.TreeState(snapshot)
    widths, gap = tree.layout(state.rows, width)
    header = tree.line(tree.HEADERS, widths, gap)
    for row in state.rows:
        if row.parent is not None or row.node.key[0] != "remote":
            continue
        text, _ = tree.render_row(row, widths, gap, width=width)
        assert text.index(row.cells[1]) == header.index("TYPE")
        assert text.startswith("[-]") and not any(
            branch in text for branch in ("├──", "└──")
        )
        assert row.cells[3] in text
        assert row.cells[4] == "1" and not any(row.cells[5:])
        child = next(child for child in state.rows if child.parent == row.node.key)
        text, _ = tree.render_row(child, widths, gap, width=width)
        assert text.startswith(" " * header.index("TYPE") + "└── ")
        assert child.node.cells[0] in text and row.cells[3] not in text
        for column in (5, 6, 7, 8):
            start = sum(widths[:column]) + gap * column
            assert text[start : start + widths[column]].strip() == tree.clip(
                child.cells[column], widths[column]
            )
    tree.print_snapshot(snapshot, width=width)
    text = capsys.readouterr().out
    assert "other connections" not in text
    assert re.search(r"^\[-\]\s+direct\s+.*fish \(31\)", text, re.MULTILINE)
    assert re.search(r"^\[-\]\s+direct\s+.*fish \(51\)", text, re.MULTILINE)


def test_selected_direct_root_survives_new_connections_and_folds(snapshot):
    state = tree.TreeState(snapshot)
    state.selected = next(
        i for i, row in enumerate(state.rows) if row.cells[3] == "fish (51)"
    )
    selected = state.rows[state.selected].node.key
    updated = deepcopy(snapshot)
    updated["other_ssh_sessions"].insert(
        0, updated["other_ssh_sessions"][0] | {"app": "aaa", "app_pid": 91, "pid": 90}
    )
    state.update(updated)
    for key in (ord("c"), ord("e"), 10, curses.KEY_LEFT, curses.KEY_RIGHT):
        state.key(key, 4)
        row = state.rows[state.selected]
        assert row.node.key == selected and row.parent is None
    state.key(curses.KEY_RIGHT, 4)
    assert state.rows[state.selected].parent == selected
    child_key = state.rows[state.selected].node.key
    state.update(updated)
    assert state.rows[state.selected].node.key == child_key
    state.key(curses.KEY_LEFT, 4)
    assert state.rows[state.selected].node.key == selected


def test_three_direct_sessions_have_three_parents_not_one_shared_app_group(snapshot):
    base = snapshot["other_mosh_sessions"][0]
    snapshot["other_mosh_sessions"] = [base, base | {"pid": 90, "tty": "/dev/pts/90"}]
    roots = [root for root in tree.build_tree(snapshot) if root.cells[1] == "direct"]
    assert len(roots) == len({root.key for root in roots}) == 3
    assert [root.cells[2] for root in roots].count("fish (31)") == 2
    assert all(root.cells[3] == "1" and len(root.children) == 1 for root in roots)
    assert all(root.children[0].key != root.key for root in roots)
    assert sorted(root.children[0].cells[0] for root in roots) == [
        "mosh [recent]",
        "mosh [recent]",
        "ssh",
    ]


def test_conns_are_populated_on_parents_not_repeated_on_children(vm_snapshot):
    state = tree.TreeState(vm_snapshot)
    parents = [row for row in state.rows if row.parent is None]
    counts = {(row.cells[1], row.cells[2]): row.cells[4] for row in parents}
    assert counts == {
        ("tmux", "changing"): "1",
        ("tmux", "quiet"): "0",
        ("tmux", "work"): "2",
        ("opsec-tmux", "pi-live"): "3",
        ("direct", "-"): "1",
    }
    assert all(row.cells[4] == "" for row in state.rows if row.parent is not None)


@pytest.mark.parametrize("attached", [0, 2, 4])
def test_tmux_count_keeps_observed_connections_and_server_attachment_totals(
    snapshot, attached
):
    snapshot["sessions"][0]["attached_clients"] = attached
    work = next(root for root in tree.build_tree(snapshot) if root.cells[0] == "work")
    assert work.cells[3] == str(max(attached, 2))


@pytest.mark.parametrize("transport", ["mosh", "ssh"])
def test_direct_fold_and_selection_survive_foreground_app_changes(snapshot, transport):
    bucket = "other_mosh_sessions" if transport == "mosh" else "other_ssh_sessions"
    original = snapshot[bucket][0]
    state = tree.TreeState(snapshot)
    state.selected = next(
        i
        for i, row in enumerate(state.rows)
        if row.parent is None and row.cells[3] == f"fish ({original['app_pid']})"
    )
    identity = state.rows[state.selected].node.key
    state.key(10, 30)
    updated = deepcopy(snapshot)
    updated[bucket][0].update(app="python3.14", app_pid=131)
    state.update(updated)
    assert identity not in state.expanded
    row = state.rows[state.selected]
    assert row.node.key == identity and row.cells[3] == "python3.14 (131)"
    state.key(10, 30)
    assert any(child.parent == identity for child in state.rows)


def test_sessions_group_by_identity_not_lowercase_name(snapshot):
    original = snapshot["sessions"][0]
    snapshot["sessions"][0] = original | {"name": "Work"}
    snapshot["sessions"].append(
        original | {"id": "$4", "name": "WORK", "attached_clients": 0}
    )
    roots = [root for root in tree.build_tree(snapshot) if root.cells[0] == "work"]
    assert len(roots) == 2
    assert {root.key for root in roots} == {("tmux", "$1"), ("tmux", "$4")}
    assert sorted(len(root.children) for root in roots) == [0, 2]


def test_snapshot_races_keep_clients_and_do_not_invent_viewers(snapshot):
    snapshot["sessions"] = []
    work, *connections = tree.build_tree(snapshot)
    assert work.cells[0] == "work" and work.cells[3] == "2"
    assert len(work.children) == 2 and len(connections) == 3
    snapshot["clients"] = []
    snapshot["sessions"] = [{"id": "$1", "name": "racing", "attached_clients": 1}]
    racing = tree.build_tree(snapshot)[0]
    assert racing.cells[3] == "1"
    assert racing.children[0].cells[0] == "viewer details unavailable"


def test_vm_viewers_group_by_guest_socket_and_session(snapshot):
    base = snapshot["other_mosh_sessions"][0]
    rows = []
    for pid, socket in [
        (30, "/run/user/1000/a.sock"),
        (40, "/run/user/1000/a.sock"),
        (50, "/run/user/1000/b.sock"),
    ]:
        rows.append(
            base
            | {
                "pid": pid,
                "app_pid": pid + 1,
                "mode": "OPSEC-TMUX",
                "app_label": "pi-opsec",
                "opsec_connection": {
                    "session": "pi-live",
                    "target": "opsec-qwen",
                    "server_socket": socket,
                    "server_binary_outdated": True,
                },
            }
        )
    snapshot["other_mosh_sessions"] = rows
    roots = [
        root for root in tree.build_tree(snapshot) if root.cells[1] == "opsec-tmux"
    ]
    assert len(roots) == 2
    assert sorted(root.cells[3] for root in roots) == ["1", "2"]
    assert {root.cells[2] for root in roots} == {
        "pi-opsec (31, 41)",
        "pi-opsec (51)",
    }
    assert all(root.highlights == ((1, "opsec-tmux"),) for root in roots)
    assert all(
        child.cells[1:3] == ("", "") for root in roots for child in root.children
    )


def test_host_session_with_vm_app_keeps_host_name_and_guest_destination(snapshot):
    snapshot["clients"][0].update(
        type="OPSEC-TMUX",
        opsec_connection={"session": "pi-live", "server_binary_outdated": True},
    )
    snapshot["sessions"][0].update(app_label="pi-opsec", app="python")
    work = next(root for root in tree.build_tree(snapshot) if root.cells[0] == "work")
    assert work.cells[1:4] == ("tmux", "pi-opsec (11)", "2")
    assert work.children[0].cells[0] == "xfce4-terminal -> opsec-tmux pi-live"
    assert work.children[0].cells[1:3] == ("", "")
    assert (0, "opsec-tmux") in work.children[0].highlights


@pytest.mark.parametrize("width", [80, 120, 240])
def test_vm_connections_use_the_same_tree_format_as_host_connections(
    vm_snapshot, width
):
    state = tree.TreeState(vm_snapshot)
    widths, gap = tree.layout(state.rows, width)
    root = next(row for row in state.rows if row.node.cells[1] == "opsec-tmux")
    parent_text, _ = tree.render_row(root, widths, gap, width=width)
    assert root.cells[1:5] == (
        "opsec-tmux",
        "pi-live",
        "pi-opsec (200, 201, 202)",
        "3",
    )
    children = [row for row in state.rows if row.parent == root.node.key]
    assert [row.cells[2] for row in children] == ["xfce4-terminal", "mosh", "ssh"]
    for index, row in enumerate(children):
        text, _ = tree.render_row(row, widths, gap, width=width)
        branch = "└──" if index == len(children) - 1 else "├──"
        assert text.startswith(" " * parent_text.index("opsec-tmux") + branch + " ")
        assert row.cells[1] == row.cells[3] == ""
        assert "opsec-tmux" not in text and "pi-opsec" not in text
        for column in (4, 5, 6, 7, 8):
            start = sum(widths[:column]) + gap * column
            assert text[start : start + widths[column]].strip() == tree.clip(
                row.cells[column], widths[column]
            )
        assert tree.cell_width(text) <= width
    assert (
        state.line_rows[state.line_rows.index(state.rows.index(children[-1])) + 1]
        is None
    )
    expanded_layout = tree.layout(state.rows, width)
    state.selected = state.rows.index(root)
    state.key(10, 20)
    assert tree.layout(state.rows, width) == expanded_layout
    assert not any(row.parent == root.node.key for row in state.rows)


def test_vm_tree_keeps_parent_and_connection_build_warnings(vm_snapshot, capsys):
    for row in vm_snapshot["other_opsec_sessions"]:
        row["opsec_connection"]["server_binary_outdated"] = True
        row["frontend_outdated"] = True
    tree.print_snapshot(vm_snapshot, color=True)
    text = capsys.readouterr().out
    assert text.count("\033[41mopsec-tmux\033[0m") == 1
    assert "├── \033[41mxfce4-terminal\033[0m" in text
    assert "├── \033[41mmosh\033[0m" in text
    assert "VIEWER" not in text and text.count("pi-opsec") == 1


@pytest.mark.parametrize("key", [10, 13, curses.KEY_ENTER, ord(" "), curses.KEY_RIGHT])
def test_expand_collapse_preserves_details(snapshot, key):
    state = tree.TreeState(snapshot)
    state.key(ord("c"), 20)
    state.selected = 2
    assert len(state.rows) == 6
    assert state.key(key, 20)
    assert len(state.rows) == 8
    assert state.rows[3].cells[:4] == ("├──", "", "xfce4-terminal", "")
    assert state.rows[4].cells[:4] == ("└──", "", "mosh", "")
    state.key(curses.KEY_LEFT, 20)
    assert len(state.rows) == 6 and state.rows[state.selected].node.key == (
        "tmux",
        "$1",
    )


def test_left_on_viewer_selects_parent_and_expand_all_is_reversible(snapshot):
    state = tree.TreeState(snapshot)
    state.key(ord("c"), 20)
    state.selected = 2
    state.key(curses.KEY_RIGHT, 3)
    state.key(curses.KEY_RIGHT, 3)
    assert state.rows[state.selected].parent == ("tmux", "$1")
    state.key(curses.KEY_LEFT, 3)
    assert state.rows[state.selected].node.key == ("tmux", "$1")
    state.key(ord("e"), 3)
    assert len(state.rows) == 12
    state.key(curses.KEY_END, 3)
    assert state.offset > 0
    state.key(ord("c"), 3)
    assert (
        len(state.rows) == 6
        and state.rows[state.selected].node.cells[1] == "other-tmux"
    )


def test_mouse_click_toggles_but_release_does_not_and_wheel_moves_five(snapshot):
    state = tree.TreeState(snapshot)
    state.key(ord("c"), 20)
    state.mouse(tree.BODY_START + 2, curses.BUTTON1_PRESSED, 20)
    assert len(state.rows) == 8
    state.mouse(tree.BODY_START + 2, curses.BUTTON1_RELEASED, 20)
    assert len(state.rows) == 8
    state.mouse(0, curses.BUTTON1_PRESSED, 20)
    assert len(state.rows) == 8
    state.mouse(tree.BODY_START + 2, curses.BUTTON1_PRESSED, 20)
    assert len(state.rows) == 6
    state.key(ord("e"), 20)
    state.selected = 7
    state.mouse(0, curses.BUTTON4_PRESSED, 20)
    assert state.selected == 2


def test_expanded_groups_have_one_nonselectable_separator(snapshot):
    state = tree.TreeState(snapshot)
    assert len(state.rows) == 12
    assert state.line_rows.count(None) == 5
    for line, index in enumerate(state.line_rows):
        if index is None:
            previous = state.rows[state.line_rows[line - 1]]
            assert previous.parent is not None
            if line + 1 < len(state.line_rows):
                assert state.rows[state.line_rows[line + 1]].parent is None
    state.key(ord("c"), 20)
    assert state.line_rows == list(range(len(state.rows)))
    state.key(ord("e"), 20)
    state.selected = next(
        i for i, row in enumerate(state.rows) if row.node.key == ("tmux", "$1")
    )
    state.key(10, 20)
    assert state.line_rows.count(None) == 4


@pytest.mark.parametrize("offset", [0, 1, 3])
def test_mouse_clicks_account_for_spacing_and_ignore_blank_lines(snapshot, offset):
    state = tree.TreeState(snapshot)
    state.offset = offset
    gap = state.line_rows.index(None)
    if offset <= gap < offset + 5:
        selected, expanded = state.selected, state.expanded.copy()
        state.mouse(tree.BODY_START + gap - offset, curses.BUTTON1_PRESSED, 5)
        assert state.selected == selected and state.expanded == expanded
        assert state.offset == offset
    work = next(i for i, row in enumerate(state.rows) if row.node.key == ("tmux", "$1"))
    y = tree.BODY_START + state.line_rows.index(work) - offset
    state.mouse(y, curses.BUTTON1_PRESSED, 5)
    assert state.selected == work
    assert ("tmux", "$1") not in state.expanded


@pytest.mark.parametrize("height", [1, 2, 4, 20])
def test_scrolling_keeps_selected_rows_visible_around_separators(snapshot, height):
    state = tree.TreeState(snapshot)
    for key in (curses.KEY_DOWN, curses.KEY_UP):
        for _ in state.rows:
            state.key(key, height)
            line = state.line_rows.index(state.selected)
            assert state.offset <= line < state.offset + height
            assert state.line_rows[line] is not None
    state.key(curses.KEY_END, height)
    assert state.selected == len(state.rows) - 1
    state.key(ord("c"), height)
    assert state.line_rows == list(range(len(state.rows)))
    assert state.offset <= state.selected < state.offset + height


def test_page_navigation_counts_display_lines_and_skips_separators(snapshot):
    state = tree.TreeState(snapshot)
    state.key(curses.KEY_NPAGE, 2)
    assert state.rows[state.selected].node.cells[0] == "quiet"
    state.key(curses.KEY_PPAGE, 1)
    assert state.rows[state.selected].parent == state.roots[0].key
    state.key(curses.KEY_NPAGE, 200)
    assert state.selected == len(state.rows) - 1
    state.key(curses.KEY_PPAGE, 200)
    assert state.selected == 0


def test_refresh_preserves_fold_and_selected_viewer_when_rows_are_inserted(snapshot):
    state = tree.TreeState(snapshot)
    state.key(ord("c"), 20)
    state.selected = 2
    state.key(10, 20)
    state.selected = 4
    selected = state.rows[4].node.key
    changed = deepcopy(snapshot)
    changed["clients"].insert(0, changed["clients"][0] | {"tty": "/dev/pts/99"})
    changed["sessions"].append(
        {"id": "$0", "name": "aaa", "app": "fish", "app_pid": 99, "attached_clients": 0}
    )
    state.update(changed)
    assert state.rows[state.selected].node.key == selected
    assert ("tmux", "$1") in state.expanded
    changed["clients"] = [
        row for row in changed["clients"] if row["transport"] != "MOSH"
    ]
    state.update(changed)
    assert state.rows[state.selected].node.key == ("tmux", "$1")


def test_default_tree_is_expanded_with_type_before_viewer(snapshot):
    state = tree.TreeState(snapshot)
    assert len(state.rows) == 12
    assert all(root.key in state.expanded for root in state.roots)
    assert tree.HEADERS[:5] == ("", "TYPE", "SESSION", "APP", "CONNS")
    assert "VIEWER" not in tree.HEADERS
    work = next(row for row in state.rows if row.node.key == ("tmux", "$1"))
    assert work.cells[:3] == ("[-]", "tmux", "work")
    xfce = next(row for row in state.rows if "xfce4-terminal" in row.cells[2])
    assert xfce.cells[:4] == ("├──", "", "xfce4-terminal", "")


def test_connections_branch_from_parent_type_with_aligned_details(snapshot):
    state = tree.TreeState(snapshot)
    roots = [row for row in state.rows if row.parent is None]
    connections = [row for row in state.rows if row.parent is not None]
    assert all(row.cells[2] == row.node.cells[0] for row in roots + connections)
    assert all(len(row.cells) == len(tree.HEADERS) for row in state.rows)
    ssh = next(row for row in connections if row.cells[2] == "ssh")
    assert ssh.cells[1:4] == ("", "ssh", "")
    assert next(row for row in roots if row.node.key == ssh.parent).cells[1:5] == (
        "direct",
        "-",
        "fish (51)",
        "1",
    )
    widths, gap = tree.layout(state.rows)
    work = next(row for row in roots if row.node.key == ("tmux", "$1"))
    parent_text, _ = tree.render_row(work, widths, gap)
    xfce = next(row for row in connections if row.cells[2] == "xfce4-terminal")
    text, _ = tree.render_row(xfce, widths, gap)
    assert text.startswith(" " * parent_text.index("tmux") + "├── xfce4-terminal")
    assert "120x40" in text and "active" in text
    assert "size=" not in text and "sizing=" not in text
    assert "codex (11)" not in text
    mosh = next(row for row in connections if row.cells[2] == "mosh")
    text, _ = tree.render_row(mosh, widths, gap)
    assert text.startswith(" " * parent_text.index("tmux") + "└── mosh")
    assert "1m01s" in text and "auto-off" in text and "idle=" not in text
    text, _ = tree.render_row(ssh, widths, gap)
    assert "└── ssh" in text and "fish (51)" not in text
    assert "100.1.2.3" in text and "110x77" in text


@pytest.mark.parametrize("width", [80, 120, 240])
def test_connection_details_align_with_headers_after_floating_tree_label(
    snapshot, width
):
    state = tree.TreeState(snapshot)
    widths, gap = tree.layout(state.rows, width)
    for row in state.rows:
        text, _ = tree.render_row(row, widths, gap, width=width)
        for column in (4, 5, 6, 7, 8):
            start = sum(widths[:column]) + gap * column
            assert text[start : start + widths[column]].strip() == tree.clip(
                row.cells[column], widths[column]
            )


@pytest.mark.parametrize("width", [80, 120, 240])
def test_viewer_names_remain_readable_across_terminal_widths(snapshot, width):
    state = tree.TreeState(snapshot)
    widths, gap = tree.layout(state.rows, width)
    for viewer in ("xfce4-terminal", "mosh"):
        row = next(row for row in state.rows if row.cells[2] == viewer)
        text, _ = tree.render_row(row, widths, gap, width=width)
        assert viewer in text and tree.cell_width(text) <= width


@pytest.mark.parametrize("width", [80, 140])
def test_long_wide_tree_labels_cannot_overwrite_connection_columns(snapshot, width):
    snapshot["clients"][0]["frontend"] = "xfce4-terminal " + "列" * 80
    state = tree.TreeState(snapshot)
    widths, gap = tree.layout(state.rows, width)
    header = tree.line(tree.HEADERS, widths, gap)
    row = next(
        row
        for row in state.rows
        if row.parent and row.node.cells[0].startswith("xfce4-terminal")
    )
    text, _ = tree.render_row(row, widths, gap, width=width)
    assert tree.cell_width(text[: text.index("120x40")]) == header.index("SIZE")
    assert tree.cell_width(text[: text.index("active")]) == header.index("SIZING")
    assert tree.cell_width(text) <= width


@pytest.mark.parametrize("width", [80, 140, 240])
def test_direct_connections_with_wide_apps_keep_aligned_details(snapshot, width):
    snapshot["other_mosh_sessions"][0]["app"] = "列"
    state = tree.TreeState(snapshot)
    widths, gap = tree.layout(state.rows, width)
    header = tree.line(tree.HEADERS, widths, gap)
    row = next(row for row in state.rows if row.node.cells[2] == "列 (31)")
    parent_text, _ = tree.render_row(row, widths, gap, width=width)
    assert "列 (31)" in parent_text
    child = next(child for child in state.rows if child.parent == row.node.key)
    text, _ = tree.render_row(child, widths, gap, width=width)
    assert tree.cell_width(text[: text.index("110x77")]) == header.index("SIZE")
    assert tree.cell_width(text) <= width


def test_clipped_tree_label_keeps_its_red_build_warning(snapshot):
    snapshot["clients"][0]["frontend_outdated"] = True
    state = tree.TreeState(snapshot)
    widths, gap = tree.layout(state.rows, 20)
    row = next(row for row in state.rows if row.cells[2] == "xfce4-terminal")
    text, spans = tree.render_row(row, widths, gap, width=20)
    assert "xfce" in text
    assert any("xfce" in label for _, label in spans)
    assert "\033[41m" in tree.paint(text, spans)


def test_parent_columns_do_not_move_when_children_are_folded(snapshot):
    state = tree.TreeState(snapshot)
    expanded = tree.layout(state.rows, 80)
    state.key(ord("c"), 20)
    assert tree.layout(state.rows, 80) == expanded


def test_aligned_warning_positions_account_for_wide_tree_labels(snapshot):
    snapshot["clients"][0].update(
        type="OPSEC-TMUX",
        frontend="xfce4-terminal 列",
        frontend_outdated=True,
        opsec_connection={"session": "pi-live", "server_binary_outdated": True},
    )
    state = tree.TreeState(snapshot)
    index, row = next(
        (i, row)
        for i, row in enumerate(state.rows)
        if row.parent and row.node.cells[0].startswith("xfce4-terminal 列")
    )
    widths, gap = tree.layout(state.rows, 139)
    text, spans = tree.render_row(row, widths, gap, width=139)
    start = next(start for start, label in spans if label == "opsec-tmux")
    x = tree.cell_width(text[:start])
    assert x > start
    painted = tree.paint(text, spans)
    assert "\033[41mopsec-tmux\033[0m" in painted
    assert "\033[41mxfce4-terminal\033[0m" in painted

    class Screen:
        def getmaxyx(self):
            return 20, 140

        def erase(self):
            self.writes = []

        def addstr(self, y, x, value, attribute):
            self.writes.append((y, x, value, attribute))

        def refresh(self):
            pass

    screen = Screen()
    tree.draw(screen, state, red=128)
    assert any(
        write[:3] == (tree.BODY_START + state.line_rows.index(index), x, "opsec-tmux")
        for write in screen.writes
    )
    assert all(
        write[0] != tree.BODY_START + line
        for line, index in enumerate(state.line_rows)
        if index is None and tree.BODY_START + line < screen.getmaxyx()[0] - 1
        for write in screen.writes
    )


def test_connections_have_first_and_last_child_branches(snapshot):
    state = tree.TreeState(snapshot)
    connections = [row for row in state.rows if row.parent is not None]
    assert connections
    for root in state.roots:
        siblings = [row for row in connections if row.parent == root.key]
        if siblings:
            assert all(row.cells[0] == "├──" for row in siblings[:-1])
            assert siblings[-1].cells[0] == "└──"
    assert {row.cells[2] for row in connections} >= {"xfce4-terminal", "mosh"}


def test_refresh_opens_new_groups_without_reopening_manual_folds(snapshot):
    state = tree.TreeState(snapshot)
    state.selected = next(
        i for i, row in enumerate(state.rows) if row.node.key == ("tmux", "$1")
    )
    state.key(10, 20)
    updated = deepcopy(snapshot)
    updated["sessions"].append({"id": "$4", "name": "new", "attached_clients": 1})
    state.update(updated)
    assert ("tmux", "$1") not in state.expanded
    assert ("tmux", "$4") in state.expanded
    assert any(row.cells[:3] == ("[-]", "tmux", "new") for row in state.rows)


def test_gutter_precedes_type_and_stays_aligned_when_folding(snapshot):
    state = tree.TreeState(snapshot)
    for expanded in (True, False):
        if not expanded:
            state.key(ord("c"), 20)
        widths, gap = tree.layout(state.rows)
        header = tree.line(tree.HEADERS, widths, gap)
        work = next(row for row in state.rows if row.node.key == ("tmux", "$1"))
        text, _ = tree.render_row(work, widths, gap)
        assert text.startswith("[-]" if expanded else "[+]")
        assert text.index("tmux") == header.index("TYPE")
        assert text.index("work") == header.index("SESSION")
        assert widths[0] == 4


@pytest.mark.parametrize("width", [1, 2, 8, 20, 40, 60, 79, 80, 81, 99, 100, 120, 240])
def test_tree_lines_fit_terminal_width_without_control_characters(snapshot, width):
    snapshot["sessions"][0]["name"] = "列" * 60 + "\n\033[2J"
    roots = tree.build_tree(snapshot)
    rows = tree.visible_rows(roots, {root.key for root in roots})
    widths, gap = tree.layout(rows, width)
    texts = [tree.line(tree.HEADERS, widths, gap)]
    texts.extend(tree.render_row(row, widths, gap, width=width)[0] for row in rows)
    for text in texts:
        assert tree.cell_width(text) <= width
        assert "\n" not in text and "\033" not in text


def test_empty_tree_layout_and_tiny_screen_are_valid(snapshot):
    assert tree.layout([], 20)[0][0] <= 20
    for key in ("sessions", "clients", "other_mosh_sessions", "other_ssh_sessions"):
        snapshot[key] = []
    state = tree.TreeState(snapshot)
    assert not state.rows
    assert state.key(curses.KEY_DOWN, 0)
    assert not state.key(ord("q"), 0)


def test_plain_tree_is_expanded_lowercase_and_keeps_commands_out(snapshot, capsys):
    snapshot["note"] = "DO NOT PRINT THIS LEGEND"
    snapshot["clients"][0]["command"] = "SECRET TOKEN"
    tree.print_snapshot(snapshot)
    text = capsys.readouterr().out
    assert "SESSION" in text and "APP" in text and "CONNS" in text
    assert "SESSION / VIEWER" not in text
    assert re.search(r"\[-\]\s+tmux\s+work\b", text)
    assert re.search(r"^\s+├── xfce4-terminal\b", text, re.MULTILINE)
    assert re.search(r"^\s+└── mosh\b", text, re.MULTILINE)
    assert text.count("codex (11)") == 1
    assert "SECRET" not in text and snapshot["note"] not in text
    assert not {"COMMAND", "TTY", "BUILDVER", "VIEWER"} & set(text.split())
    assert "\033" not in text


def test_snapshot_separators_match_live_display_without_moving_columns(
    snapshot, capsys
):
    state = tree.TreeState(snapshot)
    tree.print_snapshot(snapshot, width=120)
    body = capsys.readouterr().out.splitlines()[tree.BODY_START :]
    widths, gap = tree.layout(state.rows, 120)
    assert len(body) == len(state.line_rows)
    for text, index in zip(body, state.line_rows):
        expected = (
            ""
            if index is None
            else tree.render_row(state.rows[index], widths, gap, width=120)[0]
        )
        assert text == expected


def test_red_background_marks_parent_tmux_and_child_frontends(snapshot, capsys):
    snapshot["server_binary_outdated"] = True
    snapshot["clients"][0]["frontend_outdated"] = True
    rows = tree.TreeState(snapshot).rows
    xfce = next(row for row in rows if row.cells[2] == "xfce4-terminal")
    work = next(row for row in rows if row.node.key == ("tmux", "$1"))
    assert xfce.highlights == ((2, "xfce4-terminal"),)
    assert work.highlights == ((1, "tmux"),)
    tree.print_snapshot(snapshot, color=True)
    text = capsys.readouterr().out
    assert "\033[41mtmux\033[0m" in text
    assert "\033[41mxfce4-terminal\033[0m" in text
    assert "\033[41mmosh\033[0m" in text
    tree.print_snapshot(snapshot)
    plain = capsys.readouterr().out
    assert re.sub(r"\033\[[0-9;]*m", "", text) == plain


@pytest.mark.parametrize(
    "once,stdin_tty,stdout_tty,term,live",
    [
        (False, True, True, "xterm-256color", True),
        (True, True, True, "xterm-256color", False),
        (False, False, True, "xterm-256color", False),
        (False, True, False, "xterm-256color", False),
        (False, True, True, "dumb", False),
    ],
)
def test_dispatch_uses_live_tree_only_for_interactive_terminals(
    snapshot, monkeypatch, capsys, once, stdin_tty, stdout_tty, term, live
):
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: stdin_tty)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: stdout_tty)
    monkeypatch.setenv("TERM", term)
    seen = []
    monkeypatch.setattr(tree, "watch", lambda load, initial: seen.append(initial))
    clients.show_clients(SimpleNamespace(), once=once)
    assert bool(seen) is live
    assert (
        bool(re.search(r"\[-\]\s+tmux\s+work\b", capsys.readouterr().out)) is not live
    )


def test_json_and_flat_views_remain_available(snapshot, monkeypatch, capsys):
    snapshot["entries"] = clients.unified_entries(snapshot)
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    monkeypatch.setattr(tree, "watch", lambda *_: pytest.fail("snapshot opened UI"))
    clients.show_clients(SimpleNamespace(), as_json=True)
    assert json.loads(capsys.readouterr().out) == snapshot
    clients.show_clients(SimpleNamespace(), flat=True)
    assert "VIA  " in capsys.readouterr().out


def test_unavailable_terminal_ui_falls_back_to_a_snapshot(
    snapshot, monkeypatch, capsys
):
    monkeypatch.setattr(clients, "inventory", lambda server: snapshot)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")

    def unavailable(*_):
        raise curses.error("synthetic terminfo failure")

    monkeypatch.setattr(tree, "watch", unavailable)
    clients.show_clients(SimpleNamespace())
    output = capsys.readouterr()
    assert re.search(r"\[-\]\s+tmux\s+work\b", output.out)
    assert "showing a snapshot" in output.err


@pytest.mark.parametrize(
    "args,once,flat",
    [([], False, False), (["--once"], True, False), (["--flat"], False, True)],
)
def test_public_cli_passes_view_flags(monkeypatch, args, once, flat):
    monkeypatch.setattr(sys, "argv", ["tmux-mosh", "clients", *args])
    seen = []
    monkeypatch.setattr(clients, "show_clients", lambda *a, **kw: seen.append(kw))
    assert clients.public_main() == 0
    assert seen[0]["once"] is once and seen[0]["flat"] is flat


VIEWER = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import tmux_clients as clients
path = Path(sys.argv[1])
def load():
    if path.with_suffix('.fail').exists():
        raise RuntimeError('synthetic inventory outage')
    return json.loads(path.read_text())
clients.inventory = lambda server: load()
sys.argv = ['tmux-mosh', 'clients']
code = clients.public_main()
print('TREE_EXITED', code, flush=True)
"""


def screen_text(state):
    return "\n".join(
        base64.b64decode(line).decode("utf-8", "replace") for line in state["lines"]
    )


@pytest.mark.parametrize("stop", ["q", "escape", "ctrl-c", "hup", "term"])
def test_private_pty_click_resize_refresh_and_clean_exit(backend, snapshot, stop):
    path = backend.socket.parent / "tree.json"
    path.write_text(json.dumps(snapshot))
    view = Attachment(
        backend,
        cols=140,
        rows=20,
        argv=[
            sys.executable,
            "-c",
            VIEWER,
            str(path),
            str(Path(clients.__file__).parent),
        ],
    )
    try:
        view.until(
            lambda state: re.search(r"\[-\]\s+tmux\s+work\b", screen_text(state))
        )
        assert "xfce4-terminal" in screen_text(view.term.state)
        lines = screen_text(view.term.state).splitlines()
        assert any("├── xfce4-terminal" in line for line in lines)
        last_child = next(i for i, line in enumerate(lines) if "└── mosh" in line)
        assert not lines[last_child + 1].strip()

        def click_work():
            y = next(
                i + 1
                for i, text in enumerate(screen_text(view.term.state).splitlines())
                if re.search(r"\[[+-]\]\s+tmux\s+work\b", text)
            )
            view.send(f"\033[<0;2;{y}M\033[<0;2;{y}m".encode())

        click_work()
        view.until(
            lambda state: re.search(r"\[\+\]\s+tmux\s+work\b", screen_text(state))
        )
        assert "xfce4-terminal" not in screen_text(view.term.state)
        click_work()
        view.until(
            lambda state: re.search(r"\[-\]\s+tmux\s+work\b", screen_text(state))
        )
        view.resize(81, 12)
        view.until(
            lambda state: (
                "CONNS" in screen_text(state) and "codex (11)" in screen_text(state)
            )
        )
        changed = deepcopy(snapshot)
        changed["sessions"][0]["app"] = "fresh"
        path.write_text(json.dumps(changed))
        view.send(b"r")
        view.until(lambda state: "fresh (11)" in screen_text(state))
        assert "xfce4-terminal" in screen_text(view.term.state)
        if stop in ("hup", "term"):
            os.kill(view.pid, signal.SIGHUP if stop == "hup" else signal.SIGTERM)
        else:
            view.send({"q": b"q", "escape": b"\033", "ctrl-c": b"\003"}[stop])
        view.until(
            lambda state: (
                b"TREE_EXITED" in state["screen"]
                and not state["mouse"]
                and not state["alternate"]
            )
        )
        assert "Traceback" not in screen_text(view.term.state)
        assert not backend.socket.exists()
    finally:
        view.close()


def test_private_pty_refresh_failure_is_visible_and_recovers(backend, snapshot):
    path = backend.socket.parent / "tree.json"
    path.write_text(json.dumps(snapshot))
    view = Attachment(
        backend,
        cols=120,
        rows=20,
        argv=[
            sys.executable,
            "-c",
            VIEWER,
            str(path),
            str(Path(clients.__file__).parent),
        ],
    )
    try:
        view.until(
            lambda state: re.search(r"\[-\]\s+tmux\s+work\b", screen_text(state))
        )
        path.with_suffix(".fail").touch()
        view.send(b"r")
        view.until(
            lambda state: (
                "Refresh failed: synthetic inventory outage" in screen_text(state)
            )
        )
        assert "codex (11)" in screen_text(view.term.state)
        path.with_suffix(".fail").unlink()
        view.send(b"r")
        view.until(lambda state: "Refresh failed" not in screen_text(state))
        view.send(b"q")
        view.until(
            lambda state: b"TREE_EXITED" in state["screen"] and not state["mouse"]
        )
    finally:
        view.close()
