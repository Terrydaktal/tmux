#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
archive="$root/build/downloads/mosh-1.4.0.tar.gz"
sha=872e4b134e5df29c8933dff12350785054d2fd2839b5ae6b5587b14db1465ddd
mkdir -p "$root/build/downloads" "$root/build/runtime"
for command in curl tar patch make pkg-config protoc; do
    command -v "$command" >/dev/null || {
        printf 'Missing build prerequisite: %s\n' "$command" >&2
        exit 1
    }
done
if [[ ! -f "$archive" ]]; then
    partial=$(mktemp "$archive.XXXXXX")
    trap 'rm -f -- "$partial"' EXIT
    curl --fail --location --retry 2 --max-time 120 \
        https://github.com/mobile-shell/mosh/releases/download/mosh-1.4.0/mosh-1.4.0.tar.gz -o "$partial"
    printf '%s  %s\n' "$sha" "$partial" | sha256sum --check --status
    mv -- "$partial" "$archive"
    trap - EXIT
fi
printf '%s  %s\n' "$sha" "$archive" | sha256sum --check --status
work=$(mktemp -d "$root/build/mosh-release.XXXXXX")
trap 'echo "Mosh build logs: $work/{configure,compile}.log" >&2' ERR
tar -xzf "$archive" --strip-components=1 -C "$work"
patch --batch --forward -d "$work" -p1 <"$root/mosh/last-received.patch"
cp -- "$root/mosh/server-status.h" "$work/src/frontend/server-status.h"
(
    cd -- "$work"
    ./configure CXXFLAGS="${CXXFLAGS:--O2 -g -std=c++17}" --with-utempter >configure.log 2>&1
    make -j "${JOBS:-4}" >compile.log 2>&1
)
names=(mosh mosh-client mosh-server)
targets=("$work/scripts/mosh" "$work/src/frontend/mosh-client" "$work/src/frontend/mosh-server")
for name in "${names[@]}"; do
    target="$root/build/runtime/$name"
    if [[ -e "$target" && ! -L "$target" ]]; then
        echo "Refusing to replace non-symlink: $target" >&2
        exit 1
    fi
done
sha256sum "$archive" "$root/mosh/last-received.patch" "$root/mosh/server-status.h" \
    "${targets[@]}" >"$work/BUILD-SHA256SUMS"
for index in "${!names[@]}"; do
    target="$root/build/runtime/${names[index]}"
    ln -sfn -- "${targets[index]}" "$target"
    printf 'Built: %s\n' "$target"
done
printf 'Build logs: %s\n' "$work"
