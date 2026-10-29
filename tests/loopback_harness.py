"""Compatibility-fork Mosh on loopback with outages, roaming and private PTYs."""

import ctypes
import os
import re
import select
import signal
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from terminal_harness import Attachment

LIBC = ctypes.CDLL(None, use_errno=True)
RUNTIME = Path(__file__).resolve().parents[1] / "build/runtime"


class Link:
    """Two loopback-only sockets model loss, duplicates, outages and roaming."""

    def __init__(self, server_port, lossy=False):
        self.server = ("127.0.0.1", server_port)
        self.downstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.downstream.bind(("127.0.0.1", 0))
        self.upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.upstream.bind(("127.0.0.1", 0))
        self.port = self.downstream.getsockname()[1]
        self.client = None
        self.lossy, self.paused, self.roam = lossy, False, False
        self.packets = 0
        self.last_client_packet = None

    def pump(self):
        if self.roam:
            self.upstream.close()
            self.upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.upstream.bind(("127.0.0.1", 0))
            self.roam = False
        for source in select.select([self.downstream, self.upstream], [], [], 0)[0]:
            data, address = source.recvfrom(65536)
            if source is self.downstream:
                self.client = address
                target, destination = self.upstream, self.server
            elif self.client:
                target, destination = self.downstream, self.client
            else:
                continue
            self.packets += 1
            if self.paused or (self.lossy and self.packets % 7 == 0):
                continue
            if source is self.downstream:
                self.last_client_packet = data
            target.sendto(data, destination)
            if self.lossy and self.packets % 11 == 0:
                target.sendto(data, destination)

    def close(self):
        self.downstream.close()
        self.upstream.close()


class MoshAttachment(Attachment):
    def __init__(self, link, *args, **kwargs):
        self.link = link
        super().__init__(*args, **kwargs)

    def pump(self, duration=0.05):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self.link.pump()
            super().pump(min(0.005, max(0, deadline - time.monotonic())))
        return self.term.state

    def resize(self, cols, rows):
        # VTE settles geometry asynchronously; pause only this test client.
        os.kill(self.pid, signal.SIGSTOP)
        os.waitpid(self.pid, os.WUNTRACED)
        try:
            super().pump(0.02)
            super().resize(cols, rows)
        finally:
            os.kill(self.pid, signal.SIGCONT)


class Session:
    def __init__(
        self,
        root,
        kind="termux",
        lossy=False,
        *,
        program,
        client=str(RUNTIME / "mosh-client"),
        server=str(RUNTIME / "mosh-server"),
    ):
        self.link, self.attachment, self.pidfd = None, None, None
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        self.env = os.environ.copy()
        args = [server, "new", "-i", "127.0.0.1", "-p", str(port), "-c", "256", "--"]
        args += program
        result = subprocess.run(
            args,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=self.env,
            timeout=5,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        handshake = re.search(rb"MOSH CONNECT (\d+) ([A-Za-z0-9/+]{22})", result.stdout)
        assert handshake, "server did not provide a handshake"
        pid = int(re.search(rb"detached, pid = (\d+)", result.stderr)[1])
        self.pidfd = LIBC.pidfd_open(pid, 0)
        if self.pidfd < 0:
            raise OSError(ctypes.get_errno(), "pidfd_open for owned test server failed")
        self.env["MOSH_KEY"] = handshake[2].decode()
        self.link = Link(port, lossy)
        backend = SimpleNamespace(socket=root / "unused.sock", env=self.env)
        try:
            self.attachment = MoshAttachment(
                self.link,
                backend,
                kind=kind,
                cols=80,
                rows=24,
                argv=[client, "127.0.0.1", str(self.link.port)],
            )
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.attachment:
            self.attachment.close()
        if self.pidfd is not None:
            LIBC.pidfd_send_signal(self.pidfd, signal.SIGTERM, None, 0)
            if not select.select([self.pidfd], [], [], 0.3)[0]:
                LIBC.pidfd_send_signal(self.pidfd, signal.SIGKILL, None, 0)
            os.close(self.pidfd)
        if self.link:
            self.link.close()
