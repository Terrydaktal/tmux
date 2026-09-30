import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "tmux-native/tests"))
import terminal_harness  # noqa: E402
from terminal_harness import Attachment  # noqa: E402

terminal_harness.ROOT = ROOT

# Reuse the independently validated PTY/emulator and loopback transport harnesses,
# not either project's modified transport or renderer.
spec = importlib.util.spec_from_file_location(
    "loopback_harness", ROOT.parent / "mosh-native/tests/test_terminal.py"
)
mosh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mosh)


class Backend:
    def __init__(self, root):
        self.socket = root / "server.sock"
        self.binary = os.environ.get("TEST_TMUX", str(ROOT / "build/runtime/tmux"))
        self.prefix = [self.binary, "-S", str(self.socket)]
        self.env = os.environ.copy()
        self.log = root / "input.log"
        self.launcher = root / "home/.local/bin/tmux-simple"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.symlink_to(ROOT / "tmux-simple")

    def run(self, *argv, check=True):
        return subprocess.run(
            [*self.prefix, "-N", *argv],
            env=self.env,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=5,
            check=check,
        )

    def cli(self, *argv):
        return [
            str(self.launcher),
            "--socket",
            str(self.socket),
            "--tmux",
            self.binary,
            *argv,
        ]

    def program(self):
        return [sys.executable, str(ROOT / "tests/workload.py"), str(self.log)]

    def attach(self, kind="termux", existing=False, **kwargs):
        if os.environ.get("SIMPLE_BASELINE"):
            config = self.socket.parent / "baseline.conf"
            config.write_text(
                (ROOT / "tmux-simple.conf")
                .read_text()
                .replace("set -g scroll-on-input on\n", "")
            )
            subprocess.run(
                [
                    *self.prefix,
                    "-f",
                    str(config),
                    "new-session",
                    "-d",
                    "-s",
                    "test",
                    "--",
                    *self.program(),
                ],
                env=self.env,
                capture_output=True,
                timeout=5,
                check=True,
            )
            argv = [*self.prefix, "attach-session", "-t", "=test"]
        else:
            argv = (
                self.cli("attach", "test", "--existing")
                if existing
                else self.cli("attach", "test", "--", *self.program())
            )
        return Attachment(self, kind=kind, argv=argv, **kwargs)

    def received(self):
        return b"".join(
            bytes.fromhex(line[6:].decode())
            for line in self.log.read_bytes().splitlines()
            if line.startswith(b"INPUT=")
        )

    def field(self, value):
        return self.run(
            "display-message", "-p", "-t", "=test:.", "#{" + value + "}"
        ).stdout.strip()


@pytest.fixture
def backend(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for key in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
    ):
        monkeypatch.setenv(key, str(home))
    for key in ("TMUX", "TMUX_PANE", "DISPLAY", "WAYLAND_DISPLAY", "MOSH_NO_TERM_INIT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent-test-bus")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("MOSH_PREDICTION_DISPLAY", "never")
    monkeypatch.setenv("MOSH_SERVER_NETWORK_TMOUT", "30")
    monkeypatch.setenv("TMUX_SIMPLE_ROOT", str(ROOT))
    tmp_path.chmod(0o700)
    instance = Backend(tmp_path)
    try:
        yield instance
    finally:
        instance.run("kill-server", check=False)
