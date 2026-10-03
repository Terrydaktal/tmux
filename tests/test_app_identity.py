"""Logical Pi identification with owned fake process trees, no live processes."""

import os
import shlex
from dataclasses import replace

import pytest

import tmux_clients as clients


def process(pid, parent, executable, **changes):
    return replace(
        clients.Process(pid, parent, pid, executable, os.getuid(), "S", 100, 123, 100),
        **changes,
    )


@pytest.mark.parametrize(
    ("runtime", "command", "expected"),
    [
        ("node", "pi", True),
        ("node", "pi '' ''", True),
        ("pi", "/usr/bin/pi", True),
        ("bun", "pi", True),
        (
            "node",
            "node /project/node_modules/@mariozechner/pi-coding-agent/dist/cli.js",
            True,
        ),
        (
            "node",
            "node '/a b/@mariozechner/pi-coding-agent/dist/cli.js' --continue",
            True,
        ),
        ("bash", "pi", False),
        ("python3.14", "pi", False),
        ("node", "node -e 'console.log(\"pi\")'", False),
        ("node", "node /project/cli.js pi", False),
        ("node", "node /unrelated/pi-coding-agent/dist/cli.js", False),
        ("node", "node 'unclosed", False),
        ("node", None, False),
        ("node", "", False),
    ],
)
def test_only_recognized_pi_runtime_commands_get_logical_name(
    runtime, command, expected
):
    assert clients.is_pi_program(process(100, 1, runtime), command) == expected


@pytest.fixture
def group():
    shell = process(100, 1, "bash")
    pi = process(102, 101, "node")
    wrapper = process(101, 100, "env")
    tunnel = process(103, 100, "ssh")
    return shell, pi, {row.pid: row for row in (shell, wrapper, pi, tunnel)}


def test_real_pi_child_wins_over_shell_and_tunnel(group):
    shell, pi, processes = group
    calls = []

    def command(pid):
        calls.append(pid)
        return "pi" if pid == pi.pid else "not pi"

    assert clients.foreground_pi(shell, processes, command) == pi
    assert calls == [pi.pid]


@pytest.mark.parametrize(
    "change",
    [
        {"uid": os.getuid() + 1},
        {"state": "Z"},
        {"state": "X"},
        {"tty_device": 456},
        {"pgrp": 200},
        {"started": 99},
        {"parent": 999},
        {"executable": "python3.14"},
    ],
)
def test_unrelated_invalid_or_dead_pi_is_not_used(group, change):
    shell, pi, processes = group
    processes[pi.pid] = replace(pi, **change)
    assert clients.foreground_pi(shell, processes, lambda pid: "pi") == shell


@pytest.mark.parametrize(
    "change",
    [
        {"uid": os.getuid() + 1},
        {"state": "Z"},
        {"started": 104},
        {"parent": 102},
    ],
)
def test_unverified_or_cyclic_ancestry_is_not_used(group, change):
    shell, _, processes = group
    processes[101] = replace(processes[101], **change)
    assert clients.foreground_pi(shell, processes, lambda pid: "pi") == shell


@pytest.mark.parametrize(
    "change",
    [
        {"uid": os.getuid() + 1},
        {"state": "Z"},
        {"tty_device": 0},
        {"pgrp": 0},
        {"foreground_pgrp": 200},
        {"executable": "codex"},
    ],
)
def test_background_invalid_or_non_shell_group_leader_is_unchanged(group, change):
    shell, _, processes = group
    shell = replace(shell, **change)
    assert clients.foreground_pi(shell, processes, lambda pid: "pi") == shell


def test_ambiguous_pi_children_do_not_choose_a_random_pid(group):
    shell, pi, processes = group
    processes[104] = replace(pi, pid=104, started=104)
    assert clients.foreground_pi(shell, processes, lambda pid: "pi") == shell


def test_failed_or_reused_pid_command_read_does_not_label_pi(group):
    shell, _, processes = group
    assert clients.foreground_pi(shell, processes, lambda pid: None) == shell
    assert (
        clients.foreground_pi(
            shell, processes, lambda pid: shlex.join(["node", "other.js"])
        )
        == shell
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("pi '' ''", "pi"),
        ("pi", "pi"),
        ("pi '' --continue", "pi '' --continue"),
        ("pi --session last", "pi --session last"),
        ("node 'A path/cli.js' ''", "node 'A path/cli.js' ''"),
        ("bash '' ''", "bash '' ''"),
        ("python3 ''", "python3 ''"),
    ],
)
def test_compact_view_hides_only_empty_pi_title_padding(command, expected):
    assert clients.compact_command(command) == expected
