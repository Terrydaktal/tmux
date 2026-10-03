"""Application-drawn selections copy without a display inside the persistent pane."""

import base64
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT

PAYLOAD = "private clipboard fixture \u4e2d\u6587\nsecond line".encode()
PROGRAM = """
import os, sys, tty
from pathlib import Path
tty.setraw(0)
sequence = bytes.fromhex(sys.argv[1])
chunk = int(sys.argv[2])
if path := os.environ.get('COPY_INPUT_LOG'):
    Path(path).touch()
os.write(1, b'\\x1b[?1002h\\x1b[?1006h\\x1b[7mSELECTION-READY\\x1b[0m\\r\\n')
while data := os.read(0, 4096):
    if path := os.environ.get('COPY_INPUT_LOG'):
        with Path(path).open('ab') as log:
            log.write(data)
    if data == b'\\x03':
        for offset in range(0, len(sequence), chunk):
            os.write(1, sequence[offset:offset + chunk])
        os.write(1, b'COPY-SENT\\r\\n')
"""


def clipboard_sequence(wrapper, payload=PAYLOAD, terminator=b"\x07"):
    sequence = b"\x1b]52;c;" + base64.b64encode(payload) + terminator
    if wrapper:
        sequence = b"\x1bPtmux;" + sequence.replace(b"\x1b", b"\x1b\x1b") + b"\x1b\\"
    return sequence


def attach_application(backend, sequence, chunk=29, kind="termux"):
    backend.program = lambda: [
        sys.executable,
        "-c",
        PROGRAM,
        sequence.hex(),
        str(chunk),
    ]
    app = backend.attach(kind)
    app.until(lambda state: b"SELECTION-READY" in state["screen"])
    return app


def configure_helper(backend, path):
    backend.run("set-option", "-s", "copy-command", f"cat > {shlex.quote(str(path))}")


@pytest.mark.parametrize("wrapper", [False, True], ids=["pi-osc52", "codex-tmux-osc52"])
@pytest.mark.parametrize("terminator", [b"\x07", b"\x1b\\"], ids=["bel", "st"])
@pytest.mark.parametrize("kind", ["termux", "vte"])
def test_application_copy_reaches_helper_and_phone(
    backend, tmp_path, wrapper, terminator, kind
):
    copied = tmp_path / "copied"
    app = attach_application(
        backend, clipboard_sequence(wrapper, terminator=terminator), kind=kind
    )
    try:
        configure_helper(backend, copied)
        assert (
            backend.run("show-options", "-gv", "allow-passthrough").stdout.strip()
            == b"off"
        )
        app.send(b"\x03")
        app.until(lambda state: copied.exists() and copied.read_bytes() == PAYLOAD)
        assert backend.run("show-buffer").stdout == PAYLOAD
        assert backend.field("pane_in_mode") == b"0"
        if kind == "termux":
            app.until(lambda state: base64.b64decode(state["clipboard"]) == PAYLOAD)
    finally:
        app.close()


def test_application_copy_uses_session_display_not_pane_startup_environment(
    backend, tmp_path
):
    copied = tmp_path / "copied"
    display = tmp_path / "display"
    app = attach_application(backend, clipboard_sequence(True))
    try:
        # The synthetic program started without DISPLAY, as an SSH-started Codex can.
        backend.run(
            "set-environment", "-t", "=test", "DISPLAY", ":private-test-display"
        )
        backend.run(
            "set-option",
            "-s",
            "copy-command",
            f'printf "%s" "$DISPLAY" > {shlex.quote(str(display))}; cat > {shlex.quote(str(copied))}',
        )
        app.send(b"\x03")
        app.until(lambda state: copied.exists() and copied.read_bytes() == PAYLOAD)
        assert display.read_text() == ":private-test-display"
    finally:
        app.close()


@pytest.mark.parametrize("wrapper", [False, True])
@pytest.mark.parametrize("policy", ["off", "external"])
def test_application_copy_respects_disabled_clipboard_policy(
    backend, tmp_path, wrapper, policy
):
    copied = tmp_path / "copied"
    app = attach_application(backend, clipboard_sequence(wrapper))
    try:
        configure_helper(backend, copied)
        backend.run("set-option", "-s", "set-clipboard", policy)
        app.send(b"\x03")
        app.until(lambda state: b"COPY-SENT" in state["screen"])
        app.pump(0.15)
        assert not copied.exists()
        assert backend.run("show-buffer", check=False).returncode != 0
        assert b"\x1b]52;" not in app.wire
    finally:
        app.close()


@pytest.mark.parametrize(
    "sequence",
    [
        b"\x1bPtmux;\x1b\x1b]52;c;?\x07\x1b\\",
        b"\x1bPtmux;\x1b\x1b]52;c;%%%%\x07\x1b\\",
        b"\x1bPtmux;\x1b\x1b]52;c;YQ==\x07\x1b\x1b]2;not-clipboard\x07\x1b\\",
        b"\x1bPtmux;\x1b\x1b]2;not-clipboard\x07\x1b\\",
    ],
)
def test_wrapped_clipboard_does_not_enable_queries_or_general_passthrough(
    backend, tmp_path, sequence
):
    copied = tmp_path / "copied"
    app = attach_application(backend, sequence)
    try:
        configure_helper(backend, copied)
        backend.run("set-option", "-s", "get-clipboard", "both")
        app.send(b"\x03")
        app.until(lambda state: b"COPY-SENT" in state["screen"])
        app.pump(0.15)
        assert not copied.exists()
        assert backend.run("show-buffer", check=False).returncode != 0
        assert b"\x1b]52;" not in app.wire
        assert b"not-clipboard" not in app.wire
    finally:
        app.close()


GUI_DRIVER = r'''
import os, shlex, subprocess, sys, time
from pathlib import Path

binary, terminal, socket, source, sequence, payload, layout = sys.argv[1:]
prefix = [binary, "-N", "-S", socket]
window = None

def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, timeout=3, **kwargs)

try:
    # The server and its program start without the desktop display environment.
    env = os.environ.copy()
    input_log = Path(socket).with_suffix('.input')
    helper_log = Path(socket).with_suffix('.helper-log')
    terminal_log = Path(socket).with_suffix('.terminal-log')
    env['COPY_INPUT_LOG'] = str(input_log)
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"):
        env.pop(name, None)
    run(binary, "-S", socket, "new-session", "-d", "-s", "fixture", "--",
        sys.executable, "-c", source, sequence, "7", env=env)
    command = shlex.join(prefix + ["attach-session", "-t", "=fixture"])
    with terminal_log.open('w') as log:
        window = subprocess.Popen(
            [terminal, "--disable-server", "--title=APPLICATION-COPY-FIXTURE",
             f"--{layout}-menubar", f"--{layout}-toolbar", "--command", command],
            stdout=log, stderr=log, env=os.environ | {'G_MESSAGES_DEBUG': 'all'},
        )
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        state = subprocess.run(prefix + ["show-environment", "-t", "=fixture", "DISPLAY"],
                               capture_output=True, timeout=3).stdout
        if state.startswith(b"DISPLAY="):
            break
        time.sleep(0.03)
    else:
        raise AssertionError("desktop attachment did not restore its private display")
    # Use GTK's real X11 clipboard in the private display; no system xclip install is needed.
    writer = """
import sys
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, GLib, Gtk
clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
clipboard.set_text(sys.stdin.read(), -1)
GLib.timeout_add_seconds(10, Gtk.main_quit)
Gtk.main()
"""
    reader = """
import sys
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, Gtk
text = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text()
sys.stdout.write(text or '')
"""
    run(*prefix, "set-option", "-s", "copy-command",
        shlex.join(["/usr/bin/python", "-c", writer]) + ' 2>' + shlex.quote(str(helper_log)))
    xid = run("xdotool", "search", "--onlyvisible", "--sync", "--name", "APPLICATION-COPY-FIXTURE").stdout.splitlines()[0]
    run("xdotool", "windowfocus", "--sync", xid.decode())
    time.sleep(0.8)
    run("xdotool", "key", "--clearmodifiers", "f")
    time.sleep(0.1)
    run("xdotool", "key", "--clearmodifiers", "ctrl+c")
    expected = bytes.fromhex(payload)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = subprocess.run(["/usr/bin/python", "-c", reader],
                                capture_output=True, timeout=2)
        if result.returncode == 0 and result.stdout == expected:
            break
        time.sleep(0.03)
    else:
        recorded = input_log.read_bytes().hex() if input_log.exists() else 'none'
        error = helper_log.read_text() if helper_log.exists() else 'helper not started'
        buffer = subprocess.run(prefix + ['show-buffer'], capture_output=True, timeout=3)
        focused = run('xdotool', 'getwindowfocus').stdout.decode().strip()
        flags = run(*prefix, 'list-clients', '-F', '#{client_flags}|#{client_key_table}|#{pane_in_mode}|#{pane_mode}').stdout.decode()
        screen = run(*prefix, 'capture-pane', '-p', '-M').stdout.decode()[:2000]
        raise AssertionError(f"private fixture: key bytes={recorded}, helper={error}, "
                             f"buffer_status={buffer.returncode}, clipboard_status={result.returncode}, "
                             f"reader_error={result.stderr.decode()}, xid={xid}, focused={focused}, flags={flags}, "
                             f"screen={screen}, terminal_log={terminal_log.read_text()}")
    assert run(*prefix, "show-buffer").stdout == expected
    print("real-keyboard-and-clipboard-ok")
finally:
    subprocess.run(prefix + ["kill-server"], capture_output=True, timeout=3)
    if window is not None:
        try:
            window.wait(timeout=3)
        except subprocess.TimeoutExpired:
            window.terminate()
            window.wait(timeout=3)
'''


@pytest.mark.parametrize("wrapper", [False, True], ids=["pi-osc52", "codex-tmux-osc52"])
@pytest.mark.parametrize("layout", ["hide", "show"])
def test_real_xfce_keypress_copies_application_selection(backend, wrapper, layout):
    terminal = Path(
        os.environ.get(
            "TEST_XFCE_TERMINAL",
            str(ROOT.parents[1] / "repos/xfce4-terminal/build/terminal/xfce4-terminal"),
        )
    )
    assert terminal.is_file(), "Build the XFCE terminal first"
    config = Path(backend.env["XDG_CONFIG_HOME"]) / "xfce4/terminal/accels.scm"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        '(gtk_accel_path "<Actions>/terminal-window/copy" "<Primary>c")\n'
    )
    env = backend.env | {
        "GDK_BACKEND": "x11",
        "GTK_THEME": "Adwaita",
        "NO_AT_BRIDGE": "1",
        "GIO_USE_VFS": "local",
        "GTK_USE_PORTAL": "0",
    }
    result = subprocess.run(
        [
            "xvfb-run",
            "-a",
            "dbus-run-session",
            "--",
            sys.executable,
            "-c",
            GUI_DRIVER,
            backend.binary,
            str(terminal),
            str(backend.socket),
            PROGRAM,
            clipboard_sequence(wrapper).hex(),
            PAYLOAD.hex(),
            layout,
        ],
        env=env,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout.decode() + result.stderr.decode()
    assert b"real-keyboard-and-clipboard-ok" in result.stdout


def test_application_copy_only_targets_the_interacting_viewer(backend, tmp_path):
    copied = tmp_path / "copied"
    app = attach_application(backend, clipboard_sequence(True))
    second = None
    try:
        second = backend.attach(existing=True)
        second.until(lambda state: b"SELECTION-READY" in state["screen"])
        configure_helper(backend, copied)
        app.send(b"\x03")
        app.until(lambda state: copied.exists() and copied.read_bytes() == PAYLOAD)
        app.until(lambda state: base64.b64decode(state["clipboard"]) == PAYLOAD)
        second.pump(0.15)
        assert not second.term.state["clipboard"]
    finally:
        if second is not None:
            second.close()
        app.close()
