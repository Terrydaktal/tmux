#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ $# -ne 0 ]]; then
	echo 'Usage: scripts/link.sh' >&2
	exit 2
fi
config_root=$(dirname -- "$(readlink -f -- "$root/tmux-simple.conf")")
sources=(
	"$root/build/runtime/tmux"
	"$root/scripts/clipboard.sh"
	"$root/scripts/attach-environment.sh"
	"$config_root/integration.conf"
)
targets=(
	"$HOME/.local/bin/tmux"
	"$HOME/.local/libexec/tmux/clipboard.sh"
	"$HOME/.local/libexec/tmux/attach-environment.sh"
	"$HOME/.local/libexec/tmux/integration.conf"
)
for index in "${!targets[@]}"; do
	source_path=${sources[index]}
	target=${targets[index]}
	[[ -f $source_path ]] || {
		echo "Missing source: $source_path" >&2
		exit 1
	}
	if [[ -e $target || -L $target ]]; then
		if [[ ! -L $target ]] || [[ $(readlink -m -- "$target") != $(readlink -m -- "$source_path") ]]; then
			echo "Refusing to replace existing path: $target" >&2
			exit 1
		fi
	fi
done
for index in "${!targets[@]}"; do
	target=${targets[index]}
	mkdir -p -- "$(dirname -- "$target")"
	ln -sfn -- "${sources[index]}" "$target"
	echo "Linked: $target"
done
