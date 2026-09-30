import pytest
from conftest import mosh


@pytest.mark.parametrize("via_mosh", [False, True])
def test_active_phone_uses_full_height_after_keyboard_closes(backend, via_mosh):
    desktop = backend.attach(cols=141, rows=65)
    phone = remote = None
    try:
        desktop.until(lambda state: b"READY" in state["screen"])
        pane_pid = backend.field("pane_pid")
        if via_mosh:
            remote = mosh.Session(
                backend.socket.parent,
                "termux",
                program=backend.cli("attach", "test", "--existing"),
                client="/usr/bin/mosh-client",
                server="/usr/bin/mosh-server",
            )
            phone = remote.attachment
        else:
            phone = backend.attach(existing=True, cols=86, rows=95)
        phone.until(lambda state: b"READY" in state["screen"])
        phone.resize(86, 95)
        phone.send(b"phone-active")
        phone.until(lambda state: backend.received().endswith(b"phone-active"))
        phone.until(lambda state: backend.field("pane_height") == b"95")
        assert backend.field("pane_width") == b"86"
        assert backend.field("window-size") == b"latest"

        # Reproduce keyboard open/close, including while viewing retained history.
        phone.send(b"H")
        phone.until(lambda state: b"HISTORY-END" in state["screen"])
        phone.send(b"\x1b[<64;2;8M" * 8)
        phone.until(lambda state: backend.field("pane_in_mode") == b"1")
        for rows in (50, 95, 50, 95):
            before = len(backend.log.read_bytes())
            phone.resize(86, rows)
            phone.until(
                lambda state: backend.field("pane_height") == str(rows).encode()
            )
            phone.until(
                lambda state: (
                    f"SIZE=86x{rows}\n".encode() in backend.log.read_bytes()[before:]
                )
            )
            assert backend.field("pane_in_mode") == b"1"

        desktop.send(b"desktop-active")
        desktop.until(lambda state: backend.field("pane_width") == b"141")
        assert backend.field("pane_height") == b"65"
        phone.send(b"phone-again")
        phone.until(lambda state: backend.field("pane_height") == b"95")
        assert backend.field("pane_width") == b"86"
        assert len(backend.run("list-clients").stdout.splitlines()) == 2
        assert backend.field("pane_pid") == pane_pid
        assert not desktop.exited and not phone.exited
    finally:
        if remote:
            remote.close()
        elif phone:
            phone.close()
        desktop.close()
