#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
runtime=${BUILD_RUNTIME:-"$root/build/runtime"}
mkdir -p "$root/build" "$runtime"
work=$(mktemp -d "$root/build/release.XXXXXX")
trap 'echo "Build logs: $work/{autoreconf,configure,compile}.log" >&2' ERR
bash "$root/scripts/source-snapshot.sh" "$work"
BUILD_COUNTER_FILE=${BUILD_COUNTER_FILE:-"$root/build/build-number"} bash "$work/scripts/build-version.sh"
(
	cd -- "$work"
	autoreconf -fi >autoreconf.log 2>&1
	CFLAGS="${CFLAGS:--O2 -g -std=gnu99}" ./configure --prefix="$runtime" >configure.log 2>&1
	make -j "${JOBS:-8}" >compile.log 2>&1
)
link="$runtime/tmux"
if [[ -e "$link" && ! -L "$link" ]]; then
	echo "Refusing to replace non-symlink: $link" >&2
	exit 1
fi
sha256sum "$work/SOURCE-SHA256SUMS" "$work/SOURCE-VERSION" "$work/SOURCE-COMMIT" "$work/BUILD-VERSION" "$work/BUILD-ID" "$work/tmux" >"$work/BUILD-SHA256SUMS"
ln -sfn -- "$work/tmux" "$link"
printf 'Built: %s\nBuild log: %s/compile.log\n' "$work/tmux" "$work"
