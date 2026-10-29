#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ $# == 0 ]] || exit 2
version=$(bash "$root/scripts/source-version.sh")
counter=${BUILD_COUNTER_FILE:-"$root/build/build-number"}
mkdir -p -- "$(dirname -- "$counter")"
# Reserve each number once, even across concurrent isolated build snapshots.
exec {lock_fd}>"$counter.lock"
flock -x "$lock_fd"
build_id=0
if [[ -e $counter || -L $counter ]]; then
	build_id=$(cat -- "$counter")
fi
if [[ ! $build_id =~ ^(0|[1-9][0-9]{0,11})$ || $build_id -ge 999999999999 ]]; then
	printf 'Invalid or exhausted build counter: %s\n' "$counter" >&2
	exit 1
fi
next=$(mktemp "$counter.XXXXXX")
printf '%s\n' "$((build_id + 1))" >"$next"
mv -f -- "$next" "$counter"
printf '%s\n' "$build_id" >"$root/BUILD-ID"
printf '%s-%s\n' "${version%%+g*}" "$build_id" >"$root/BUILD-VERSION"
cat "$root/BUILD-VERSION"
