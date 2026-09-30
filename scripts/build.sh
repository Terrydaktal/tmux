#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
version=3.7c
sha=7c60cae9a0e25288e2e24750aafc9e8800fc7fd4555e447e1b29ee4201cfb3bf
archive="$root/build/downloads/tmux-$version.tar.gz"
mkdir -p "$root/build/downloads" "$root/build/runtime"
if [[ ! -f "$archive" ]]; then
    partial=$(mktemp "$archive.XXXXXX")
    trap 'rm -f -- "$partial"' EXIT
    curl --fail --location --retry 2 --max-time 120 \
        "https://github.com/tmux/tmux/releases/download/$version/tmux-$version.tar.gz" -o "$partial"
    printf '%s  %s\n' "$sha" "$partial" | sha256sum --check --status
    mv -- "$partial" "$archive"
    trap - EXIT
fi
printf '%s  %s\n' "$sha" "$archive" | sha256sum --check --status
work=$(mktemp -d "$root/build/release.XXXXXX")
trap 'echo "Build logs: $work/{autoreconf,configure,compile}.log" >&2' ERR
tar -xzf "$archive" --strip-components=1 -C "$work"
for patch_file in "$root"/patches/*.patch; do
    patch --batch --forward -d "$work" -p1 <"$patch_file"
done
(
    cd -- "$work"
    autoreconf -fi >autoreconf.log 2>&1
    CFLAGS="${CFLAGS:--O2 -g -std=gnu99}" ./configure --prefix="$root/build/runtime" >configure.log 2>&1
    make -j "${JOBS:-8}" >compile.log 2>&1
)
link="$root/build/runtime/tmux"
if [[ -e "$link" && ! -L "$link" ]]; then
    echo "Refusing to replace non-symlink: $link" >&2
    exit 1
fi
sha256sum "$archive" "$root"/patches/*.patch "$work/tmux" >"$work/BUILD-SHA256SUMS"
ln -sfn -- "$work/tmux" "$link"
printf 'Built: %s\nBuild log: %s/compile.log\n' "$work/tmux" "$work"
