import os
import subprocess
from pathlib import Path

import pytest
from conftest import ROOT
from terminal_harness import Attachment


def test_osc8_file_links_survive_rendering_and_reattach(backend):
    app = backend.attach("vte")
    second = None
    try:
        app.until(lambda s: b"READY" in s["screen"])
        app.send(b"L")
        app.until(lambda s: b"PLAIN-TEXT" in s["screen"])
        expected = "file:///tmp/tmux-link%20test.txt"
        assert app.term.command("LINK 3 0")["link"] == expected
        assert app.term.command("LINK 3 1")["link"] is None
        # Model an already-running server with the old feature configuration.
        backend.run("set-option", "-s", "terminal-features", "xterm*:RGB:clipboard")
        second = Attachment(
            backend,
            kind="vte",
            argv=backend.cli("attach", "test", "--existing"),
        )
        second.until(lambda s: b"PLAIN-TEXT" in s["screen"])
        assert second.term.command("LINK 3 0")["link"] == expected
        app.send(b"H")
        app.until(lambda s: b"HISTORY-END" in s["screen"])
        backend.run("copy-mode", "-t", "=test:.")
        backend.run("send-keys", "-t", "=test:.", "-X", "history-top")
        app.until(lambda s: b"LINK-TARGET" in s["screen"])
        rows = app.term.state["screen"].splitlines()
        row = next(i for i, line in enumerate(rows) if b"LINK-TARGET" in line)
        assert app.term.command(f"LINK 3 {row}")["link"] == expected
    finally:
        if second:
            second.close()
        app.close()


@pytest.mark.parametrize("kind", ["osc8", "absolute", "relative", "denied", "timeout"])
def test_xfce_ctrl_click_opens_and_ctrl_shift_click_reveals(backend, kind):
    oracle = Path(
        os.environ.get(
            "TEST_XFCE_LINKS",
            str(
                ROOT.parents[1] / "repos/xfce4-terminal/build/tests/test-terminal-links"
            ),
        )
    )
    assert oracle.is_file(), "Build the XFCE terminal links integration test first"
    env = backend.env | {
        "TEST_TMUX_SIMPLE": str(backend.launcher),
        "TEST_TMUX": backend.binary,
        "TERMINAL_LINK_TEST_ISOLATED": "1",
        "GDK_BACKEND": "x11",
        "GTK_THEME": "Adwaita",
        "NO_AT_BRIDGE": "1",
        "GIO_USE_VFS": "local",
        "GTK_USE_PORTAL": "0",
        "TMPDIR": str(backend.socket.parent),
    }
    try:
        result = subprocess.run(
            [
                "xvfb-run",
                "-a",
                "dbus-run-session",
                "--",
                str(oracle),
                "-p",
                f"/links/tmux/{kind}",
            ],
            env=env,
            capture_output=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, result.stdout.decode() + result.stderr.decode()
        assert f"ok 1 /links/tmux/{kind}".encode() in result.stdout, result.stdout
        assert b"1..1" in result.stdout, result.stdout
    finally:
        # A C assertion can abort before cleanup. Only these test-owned sockets
        # are eligible; never use the real user's default server.
        for socket in backend.socket.parent.glob("xfce-tmux-links-*/server.sock"):
            subprocess.run(
                [backend.binary, "-N", "-S", str(socket), "kill-server"],
                env=env,
                capture_output=True,
                timeout=5,
                check=False,
            )
