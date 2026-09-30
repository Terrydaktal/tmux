#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
names=(tmux-simple)
if [[ $# -eq 1 && $1 == --as-tmux ]]; then
    names+=(tmux)
elif [[ $# -ne 0 ]]; then
    echo 'Usage: scripts/link.sh [--as-tmux]' >&2
    exit 2
fi
[[ -x $root/build/runtime/tmux ]] || {
    echo 'Run scripts/build.sh first.' >&2
    exit 1
}
# Check every destination before installing either name.
for name in "${names[@]}"; do
    target="$HOME/.local/bin/$name"
    if [[ -e $target || -L $target ]] && [[ ! -L $target || $(readlink -- "$target") != "$root/tmux-simple" ]]; then
        echo "Refusing to replace existing path: $target" >&2
        exit 1
    fi
done
mkdir -p -- "$HOME/.local/bin"
for name in "${names[@]}"; do
    target="$HOME/.local/bin/$name"
    [[ -L $target ]] || ln -s -- "$root/tmux-simple" "$target"
    echo "Linked: $target"
done
