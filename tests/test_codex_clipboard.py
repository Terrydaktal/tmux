"""Exercise the installed Codex selection code with a private saved conversation."""

import base64
import json
import os
from pathlib import Path

from conftest import ROOT


def test_real_codex_transcript_selection_copies_without_a_desktop_environment(
    backend, tmp_path
):
    codex = Path(
        os.environ.get(
            "TEST_CODEX",
            str(ROOT.parents[1] / ".local/bin/codex"),
        )
    )
    assert codex.is_file(), "Set TEST_CODEX to the Codex release being verified"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    fixture = "CLIPBOARD_FIXTURE_159"
    thread = "f5d2e811-beba-41ef-a204-d4e02ac4ed09"
    timestamp = "2026-09-30T10:00:00Z"
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": thread,
                "session_id": thread,
                "timestamp": timestamp,
                "cwd": str(tmp_path),
                "originator": "codex",
                "cli_version": "0.159.1",
                "source": "cli",
                "model_provider": "clipboard-fixture",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": fixture}],
            },
        },
        {
            "type": "event_msg",
            "payload": {"type": "user_message", "message": fixture, "kind": "plain"},
        },
    ]
    rollout = (
        codex_home
        / "sessions/2026/09/30"
        / f"rollout-2026-09-30T10-00-00-{thread}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        "".join(
            json.dumps({"timestamp": timestamp, **record}) + "\n" for record in records
        )
    )
    (codex_home / "config.toml").write_text(
        'model = "gpt-5"\n'
        'model_provider = "clipboard-fixture"\n'
        "check_for_update_on_startup = false\n"
        'approval_policy = "never"\n'
        'sandbox_mode = "read-only"\n'
        "[tui]\n"
        'copy_on_select = "never"\n'
        "[model_providers.clipboard-fixture]\n"
        'name = "Local clipboard fixture"\n'
        'base_url = "http://127.0.0.1:9/v1"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = false\n"
        f"[projects.{json.dumps(str(tmp_path))}]\n"
        'trust_level = "trusted"\n'
    )
    backend.env["CODEX_HOME"] = str(codex_home)
    backend.program = lambda: [str(codex), "--no-daemon", "resume", thread]
    copied = tmp_path / "copied"
    app = backend.attach(cols=100, rows=32)
    try:
        app.until(lambda state: fixture.encode() in state["screen"], timeout=15)
        backend.run("set-option", "-s", "copy-command", f"cat > '{copied}'")
        app.pump(0.3)
        lines = [base64.b64decode(line).decode() for line in app.term.state["lines"]]
        row, line = next(
            (row, line) for row, line in enumerate(lines) if fixture in line
        )
        column = line.index(fixture)
        app.send(f"\x1b[<0;{column + 1};{row + 1}M".encode())
        app.pump(0.1)
        app.send(f"\x1b[<32;{column + len(fixture) + 1};{row + 1}M".encode())
        app.pump(0.1)
        app.send(f"\x1b[<0;{column + len(fixture) + 1};{row + 1}m".encode())
        app.until(lambda state: b"ctrl+c copy" in state["screen"], timeout=3)
        assert not copied.exists(), "The fixture must copy only on Ctrl+C"
        app.send(b"\x03")
        app.until(lambda state: copied.exists(), timeout=8)
        assert copied.read_text().strip() == fixture
    finally:
        app.close()
