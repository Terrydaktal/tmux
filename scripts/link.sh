#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $# -ne 0 ]]; then
    echo 'Usage: scripts/link.sh' >&2
    exit 2
fi
sources=(
    "$root/build/runtime/tmux"
    "$root/tmux-mosh"
    "$root/scripts/clipboard.sh"
    "$root/scripts/attach-environment.sh"
    "$root/../config/tmux-simple/integration.conf"
)
targets=(
    "$HOME/.local/bin/tmux"
    "$HOME/.local/bin/tmux-mosh"
    "$HOME/.local/libexec/tmux/clipboard.sh"
    "$HOME/.local/libexec/tmux/attach-environment.sh"
    "$HOME/.local/libexec/tmux/integration.conf"
)
if [[ -x "$root/build/runtime/mosh-server" ]]; then
    for name in mosh mosh-client mosh-server; do
        sources+=("$root/build/runtime/$name")
        targets+=("$HOME/.local/bin/$name")
    done
fi
for index in "${!targets[@]}"; do
    source_path=${sources[index]}
    target=${targets[index]}
    [[ -f $source_path ]] || {
        echo "Missing source: $source_path" >&2
        exit 1
    }
    if [[ -e $target || -L $target ]]; then
        if [[ ! -L $target ]] || {
            [[ $(readlink -m -- "$target") != $(readlink -m -- "$source_path") ]] &&
                [[ $target != "$HOME/.local/bin/tmux" || $(readlink -m -- "$target") != "$root/tmux-simple" ]]
        }; then
            echo "Refusing to replace existing path: $target" >&2
            exit 1
        fi
    fi
done
legacy="$HOME/.local/bin/tmux-simple"
if [[ -e $legacy || -L $legacy ]] &&
    [[ ! -L $legacy || $(readlink -m -- "$legacy") != "$root/tmux-simple" ]]; then
    echo "Refusing to remove unrelated path: $legacy" >&2
    exit 1
fi
for index in "${!targets[@]}"; do
    target=${targets[index]}
    mkdir -p -- "$(dirname -- "$target")"
    ln -sfn -- "${sources[index]}" "$target"
    echo "Linked: $target"
done
[[ ! -L $legacy ]] || rm -- "$legacy"
