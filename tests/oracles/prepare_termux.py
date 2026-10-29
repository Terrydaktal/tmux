"""Fetch a pinned upstream emulator; compile a headless Android-free test host."""

import hashlib
import json
import re
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMIT = "5b657c6adf4304e5198951ce815fe0205dcac29c"  # Termux v0.118.3
FILES = [
    "TerminalEmulator",
    "TerminalBuffer",
    "TerminalRow",
    "TerminalColors",
    "TerminalColorScheme",
    "TextStyle",
    "WcWidth",
    "KeyHandler",
    "TerminalOutput",
    "TerminalSessionClient",
    "Logger",
]


def main():
    dest = ROOT / "build/termux/src/com/termux/terminal"
    dest.mkdir(parents=True, exist_ok=True)
    sources = []
    for name in FILES:
        url = f"https://raw.githubusercontent.com/termux/termux-app/{COMMIT}/terminal-emulator/src/main/java/com/termux/terminal/{name}.java"
        path = dest / f"{name}.java"
        # Always resolve content from the immutable revision when preparing.
        data = urllib.request.urlopen(url, timeout=30).read()
        path.write_bytes(data)
        sources.append({"url": url, "sha256": hashlib.sha256(data).hexdigest()})
    license_url = (
        f"https://raw.githubusercontent.com/termux/termux-app/{COMMIT}/LICENSE.md"
    )
    (ROOT / "build/termux/LICENSE.md").write_bytes(
        urllib.request.urlopen(license_url, timeout=30).read()
    )
    names = re.findall(
        r"import static android.view.KeyEvent.(\w+);",
        (dest / "KeyHandler.java").read_text(),
    )
    stub = ROOT / "build/termux/src/android/view/KeyEvent.java"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        "package android.view;\npublic final class KeyEvent {\n"
        + "\n".join(
            f"public static final int {name} = {i};" for i, name in enumerate(names, 1)
        )
        + "\n}\n"
    )
    classes = ROOT / "build/termux/classes"
    classes.mkdir(parents=True, exist_ok=True)
    files = sorted((ROOT / "build/termux/src").rglob("*.java")) + sorted(
        (ROOT / "tests/oracles/java").rglob("*.java")
    )
    subprocess.run(
        ["javac", "-encoding", "UTF-8", "-d", str(classes), *map(str, files)],
        check=True,
    )
    (ROOT / "build/termux/provenance.json").write_text(
        json.dumps({"commit": COMMIT, "sources": sources}, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
