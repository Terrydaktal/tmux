#!/usr/bin/env bash
set -euo pipefail

# OSC 52 independently handles the attached phone. Use the desktop's own
# clipboard tool when its display environment is available; never infer one.
if [[ -n ${WAYLAND_DISPLAY:-} ]] && command -v wl-copy >/dev/null 2>&1; then
    exec wl-copy --type text/plain
elif [[ -n ${DISPLAY:-} ]] && command -v xclip >/dev/null 2>&1; then
    exec xclip -selection clipboard -in
fi
cat >/dev/null
