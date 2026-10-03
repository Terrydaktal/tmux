import base64
import errno
import fcntl
import json
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Emulator:
    def __init__(self, kind="termux", cols=80, rows=24):
        if kind == "termux":
            command = [
                "java",
                "-cp",
                str(ROOT / "build/termux/classes"),
                "com.termux.terminal.Oracle",
                str(cols),
                str(rows),
            ]
        else:
            command = [
                "xvfb-run",
                "-a",
                "/usr/bin/python",
                str(ROOT / "tests/oracles/vte.py"),
                str(cols),
                str(rows),
            ]
        env = os.environ.copy()
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/nonexistent-tmux-simple-test-bus"
        env["GSETTINGS_BACKEND"] = "memory"
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            self.state = self.command("STATE")
        except BaseException:
            self.close()
            raise

    def command(self, command):
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()
        if not select.select([self.proc.stdout], [], [], 5)[0]:
            raise AssertionError("terminal oracle did not reply")
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(self.proc.stderr.read())
        result = json.loads(line)
        for key in ("text", "screen", "replies"):
            result[key] = base64.b64decode(result[key])
        self.state = result
        return result

    def feed(self, data):
        return self.command("FEED " + base64.b64encode(data).decode())

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdout.close()
        self.proc.stderr.close()


class Attachment:
    def __init__(
        self, backend, name="test", cols=80, rows=24, kind="termux", argv=None
    ):
        self.term = Emulator(kind, cols, rows)
        self.wire = bytearray()
        pid, master = pty.fork()
        if pid == 0:
            os.chdir(backend.socket.parent)
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            args = argv or backend.prefix + ["attach-session", "-t", "=" + name]
            os.execvpe(args[0], args, backend.env)
        self.pid, self.master = pid, master
        self.exited = False

    def send(self, data):
        os.write(self.master, data)

    def resize(self, cols, rows):
        self.term.command(f"SIZE {cols} {rows}")
        fcntl.ioctl(
            self.master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
        )

    def pump(self, duration=0.05):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if not select.select(
                [self.master], [], [], min(0.02, max(0, deadline - time.monotonic()))
            )[0]:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    self.exited = True
                    break
                raise
            if not data:
                self.exited = True
                break
            self.wire.extend(data)
            state = self.term.feed(data)
            if state["replies"]:
                self.send(state["replies"])
        return self.term.state

    def until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.pump()
            if predicate(state):
                return state
            if self.exited:
                break
        raise AssertionError(
            f"terminal condition not reached; state={self.term.state!r}, tail={bytes(self.wire[-1000:])!r}"
        )

    def close(self):
        try:
            os.kill(self.pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                break
            time.sleep(0.02)
        else:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        os.close(self.master)
        self.term.close()
