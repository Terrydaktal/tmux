import re
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import ROOT
from terminal_harness import Attachment

CONFIG_REPO = ROOT.parent / "config"


def user_config(backend):
    return Path(backend.env["HOME"]) / ".tmux.conf"


def test_native_tmux_loads_the_canonical_user_config(backend):
    config = user_config(backend)
    config.unlink()
    config.write_text(
        (ROOT / "tmux-simple.conf").read_text() + "\nset -g @config-origin native\n"
    )
    app = backend.attach()
    try:
        app.until(lambda state: b"READY" in state["screen"])
        assert (
            backend.run("show-options", "-gv", "@config-origin").stdout == b"native\n"
        )
        assert backend.run("show-options", "-gv", "scroll-on-input").stdout == b"on\n"
        assert backend.run("show-options", "-gv", "status").stdout == b"off\n"
    finally:
        app.close()


@pytest.mark.parametrize("kind", ["vte", "termux"])
def test_active_pane_title_spinners_and_attention_reach_each_terminal(backend, kind):
    def title_sequences(wire):
        return re.findall(rb"\x1b\][02];([^\x07\x1b]*)(?:\x07|\x1b\\)", wire)

    def await_title(viewer, title, start=0):
        viewer.until(lambda state: title in title_sequences(viewer.wire[start:]))

    app = backend.attach(kind)
    second = None
    try:
        app.until(lambda state: b"READY" in state["screen"])
        assert backend.run("show-options", "-gv", "set-titles").stdout == b"on\n"
        assert backend.run("show-options", "-gv", "set-titles-string").stdout == (
            b"tmux: #{session_name} - "
            b"#{?#{E:@codex-title-enabled},#{E:@codex-title-value},"
            b"#{s/ \\| / - /g:pane_title}}\n"
        )
        second = backend.attach(kind, existing=True)
        second.until(lambda state: b"READY" in state["screen"])
        for title in [
            "\u280b Working | test",
            "\u2819 Working | test",
            "[ ! ] Action Required | test",
            "[ . ] Action Required | test",
            "Idle | test",
        ]:
            starts = [len(viewer.wire) for viewer in (app, second)]
            backend.run("select-pane", "-t", "=test:.", "-T", title)
            for viewer, start in zip((app, second), starts, strict=True):
                await_title(
                    viewer,
                    b"tmux: test - " + title.replace(" | ", " - ").encode(),
                    start,
                )
            assert backend.field("pane_title") == title.encode()

        original = backend.field("pane_id").decode()
        other = (
            backend.run(
                "split-window", "-h", "-P", "-F", "#{pane_id}", "--", *backend.program()
            )
            .stdout.strip()
            .decode()
        )
        backend.run("select-pane", "-t", other, "-T", "Other pane")
        for viewer in (app, second):
            await_title(viewer, b"tmux: test - Other pane")
        starts = [len(viewer.wire) for viewer in (app, second)]
        backend.run("select-pane", "-t", original)
        for viewer, start in zip((app, second), starts, strict=True):
            await_title(viewer, b"tmux: test - Idle - test", start)

        starts = [len(viewer.wire) for viewer in (app, second)]
        backend.run("rename-session", "-t", "=test", "renamed")
        for viewer, start in zip((app, second), starts, strict=True):
            await_title(viewer, b"tmux: renamed - Idle - test", start)
    finally:
        if second:
            second.close()
        app.close()


@pytest.mark.parametrize("kind", ["vte", "termux"])
def test_codex_native_titles_use_real_cwd_then_task_and_preserve_activity(
    backend, kind
):
    home = Path(backend.env["HOME"])
    cwd = home / "tasks" / "diet"
    cwd.mkdir(parents=True)
    program = backend.program()
    codex = home / "codex"
    codex.symlink_to(program[0])
    app = Attachment(
        backend,
        kind=kind,
        argv=backend.cli(
            "new-session", "-s", "test", "-c", str(cwd), "--", str(codex), *program[1:]
        ),
    )
    second = None
    try:
        app.until(lambda state: b"READY" in state["screen"])
        assert backend.field("pane_current_command") == b"codex"
        second = backend.attach(kind, existing=True)
        second.until(lambda state: b"READY" in state["screen"])
        last_expected = None
        for title, context in [
            (
                "Check ChatGPT link access | diet",
                "~/tasks/diet - Check ChatGPT link access",
            ),
            (
                "\u280b Check ChatGPT link access | diet",
                "\u280b ~/tasks/diet - Check ChatGPT link access",
            ),
            (
                "\u2819 Check ChatGPT link access | diet",
                "\u2819 ~/tasks/diet - Check ChatGPT link access",
            ),
            (
                "[ ! ] Action Required | Check ChatGPT link access | diet",
                "[ ! ] Action Required ~/tasks/diet - Check ChatGPT link access",
            ),
            (
                "[ . ] Action Required | Check ChatGPT link access | diet",
                "[ . ] Action Required ~/tasks/diet - Check ChatGPT link access",
            ),
            (
                "codex | ~/tasks/diet | Check ChatGPT link access",
                "~/tasks/diet - Check ChatGPT link access",
            ),
            (
                "codex \u280b ~/tasks/diet | Check ChatGPT link access",
                "\u280b ~/tasks/diet - Check ChatGPT link access",
            ),
            (
                "[ ! ] Action Required | codex | ~/tasks/diet | Check ChatGPT link access",
                "[ ! ] Action Required ~/tasks/diet - Check ChatGPT link access",
            ),
            ("codex | ~/tasks/diet | Task | literal", "~/tasks/diet - Task - literal"),
            ("codex | ~/truncated/cwd... | Task", "~/tasks/diet - Task"),
            ("codex - ~/tasks/diet - Task - literal", "~/tasks/diet - Task - literal"),
            (
                "codex \u280b ~/tasks/diet - Task - literal",
                "\u280b ~/tasks/diet - Task - literal",
            ),
            (
                "[ ! ] Action Required - codex - ~/tasks/diet - Task",
                "[ ! ] Action Required ~/tasks/diet - Task",
            ),
            (
                "[ . ] Action Required - codex - ~/tasks/diet - Task",
                "[ . ] Action Required ~/tasks/diet - Task",
            ),
            ("codex - ~", "~/tasks/diet"),
            ("Task | literal | diet", "~/tasks/diet - Task - literal"),
            ("codex repair | diet", "~/tasks/diet - codex repair"),
            ("diet | diet", "~/tasks/diet - diet"),
            ("diet", "~/tasks/diet"),
            ("\u280b diet", "\u280b ~/tasks/diet"),
            *[
                (frame + " Task | diet", frame + " ~/tasks/diet - Task")
                for frame in "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"
            ],
        ]:
            starts = [len(viewer.wire) for viewer in (app, second)]
            backend.run("select-pane", "-t", "=test:.", "-T", title)
            expected = ("tmux: test - codex: " + context).encode()
            assert (
                backend.run(
                    "display-message", "-p", "-t", "=test:.", "#{E:set-titles-string}"
                ).stdout.strip()
                == expected
            )
            for viewer, start in zip((app, second), starts, strict=True):
                if expected == last_expected:
                    continue  # tmux correctly avoids resending an unchanged title.
                viewer.until(
                    lambda state, viewer=viewer, start=start: (
                        expected
                        in re.findall(
                            rb"\x1b\][02];([^\x07\x1b]*)(?:\x07|\x1b\\)",
                            viewer.wire[start:],
                        )
                    )
                )
            assert backend.field("pane_title") == title.encode()
            last_expected = expected
        assert (
            "#("
            not in backend.run(
                "display-message", "-p", "#{E:set-titles-string}"
            ).stdout.decode()
        )
    finally:
        if second:
            second.close()
        app.close()


def test_existing_server_does_not_reload_config_when_attaching_or_creating_sessions(
    backend,
):
    config = user_config(backend)
    config.unlink()
    config.write_text(
        (ROOT / "tmux-simple.conf").read_text() + "\nset -g @config-origin original\n"
    )
    first = backend.attach()
    second = third = None
    try:
        first.until(lambda state: b"READY" in state["screen"])
        config.write_text("set -g @config-origin unexpectedly-reloaded\n")
        second = backend.attach(existing=True)
        second.until(lambda state: b"READY" in state["screen"])
        third = Attachment(
            backend,
            argv=backend.cli("new-session", "-s", "second", "--", *backend.program()),
        )
        third.until(lambda state: b"READY" in state["screen"])
        assert (
            backend.run("show-options", "-gv", "@config-origin").stdout == b"original\n"
        )
    finally:
        if third:
            third.close()
        if second:
            second.close()
        first.close()


def test_integration_only_reload_preserves_controls_and_running_program(backend):
    app = backend.attach()
    try:
        app.until(lambda state: b"READY" in state["screen"])
        backend.run("bind-key", "-T", "root", "F12", "display-message", "retained")
        keys = backend.run("list-keys", "-T", "root").stdout
        server = backend.run("display-message", "-p", "#{pid}").stdout
        pane = backend.field("pane_pid")
        backend.run("set-option", "-g", "@unrelated", "retained")
        backend.run("source-file", str(CONFIG_REPO / "tmux-simple/integration.conf"))
        assert backend.run("list-keys", "-T", "root").stdout == keys
        assert backend.run("display-message", "-p", "#{pid}").stdout == server
        assert backend.field("pane_pid") == pane
        assert backend.run("show-options", "-gv", "@unrelated").stdout == b"retained\n"
        app.send(b"hello")
        app.until(lambda state: b"hello" in backend.received())
    finally:
        app.close()


def test_titles_only_reload_preserves_controls_and_running_program(backend):
    app = backend.attach()
    try:
        app.until(lambda state: b"READY" in state["screen"])
        backend.run("bind-key", "-T", "root", "F12", "display-message", "retained")
        keys = backend.run("list-keys", "-T", "root").stdout
        server = backend.run("display-message", "-p", "#{pid}").stdout
        pane = backend.field("pane_pid")
        backend.run("set-option", "-g", "@unrelated", "retained")
        backend.run("set-option", "-g", "set-titles-string", "old-title")
        titles = "\n".join(
            line
            for line in (ROOT / "tmux-simple.conf").read_text().splitlines()
            if line.startswith(("set -g @codex-title-", "set -g set-titles"))
        )
        subprocess.run(
            [*backend.prefix, "-N", "source-file", "-"],
            input=(titles + "\n").encode(),
            env=backend.env,
            capture_output=True,
            timeout=5,
            check=True,
        )
        assert backend.run("list-keys", "-T", "root").stdout == keys
        assert backend.run("display-message", "-p", "#{pid}").stdout == server
        assert backend.field("pane_pid") == pane
        assert backend.run("show-options", "-gv", "@unrelated").stdout == b"retained\n"
        assert backend.run(
            "show-options", "-gv", "set-titles-string"
        ).stdout.startswith(b"tmux: #{session_name} - ")
    finally:
        app.close()


@pytest.fixture
def config_installer(backend):
    repo = backend.socket.parent / "config repo"
    directory = repo / "tmux-simple"
    directory.mkdir(parents=True)
    for name in ("link.sh", "tmux.conf", "tmux.fish", "integration.conf"):
        shutil.copy2(CONFIG_REPO / "tmux-simple" / name, directory / name)
    user_config(backend).unlink()

    def install():
        return subprocess.run(
            ["bash", str(directory / "link.sh")],
            env=backend.env,
            capture_output=True,
            timeout=5,
        )

    return repo, install


@pytest.mark.parametrize("legacy", ["managed", "archived", "absent"])
def test_config_installer_migrates_managed_links_and_installs_fish_idempotently(
    backend, config_installer, legacy
):
    repo, install = config_installer
    old = user_config(backend)
    if legacy == "managed":
        old.symlink_to(repo / "tmux/tmux.conf")
    elif legacy == "archived":
        old.symlink_to(repo / "legacy/tmux/tmux.conf")
    config_home = Path(backend.env["XDG_CONFIG_HOME"])
    for _ in range(2):
        result = install()
        assert result.returncode == 0, result.stderr
        assert old.resolve() == repo / "tmux-simple/tmux.conf"
        assert (config_home / "tmux-simple/tmux.conf").resolve() == old.resolve()
        assert (
            config_home / "fish/functions/tmux.fish"
        ).resolve() == repo / "tmux-simple/tmux.fish"


@pytest.mark.parametrize("conflict", ["file", "symlink", "broken-link", "fish-file"])
def test_config_installer_refuses_foreign_files_before_changing_any_link(
    backend, config_installer, conflict
):
    repo, install = config_installer
    config = user_config(backend)
    if conflict == "fish-file":
        config.symlink_to(repo / "tmux/tmux.conf")
        target = Path(backend.env["XDG_CONFIG_HOME"]) / "fish/functions/tmux.fish"
        target.parent.mkdir(parents=True)
        target.write_text("keep my function")
    elif conflict == "file":
        config.write_text("keep my config")
    else:
        config.symlink_to(config.with_name("foreign.conf"))
        if conflict == "symlink":
            config.with_name("foreign.conf").write_text("keep my config")
    before = config.readlink() if config.is_symlink() else config.read_bytes()
    result = install()
    assert result.returncode != 0
    assert b"Refusing to replace" in result.stderr
    assert (config.readlink() if config.is_symlink() else config.read_bytes()) == before
    assert not (Path(backend.env["XDG_CONFIG_HOME"]) / "tmux-simple/tmux.conf").exists()


def test_bootstrap_uses_the_canonical_config_installer():
    bootstrap = (CONFIG_REPO / "bootstrap.sh").read_text()
    assert 'bash "$REPO_DIR/tmux-simple/link.sh"' in bootstrap
