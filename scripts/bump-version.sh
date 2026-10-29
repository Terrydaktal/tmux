#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
configure="$root/FORK-VERSION"
part=${1:-patch}
if [[ $# -gt 1 || ! $part =~ ^(patch|minor|major)$ ]]; then
	printf 'Usage: bash scripts/bump-version.sh [patch|minor|major]\n' >&2
	exit 2
fi
version=$(cat "$configure")
if [[ ! $version =~ ^([0-9][[:alnum:].]*-fork)(0|[1-9][0-9]{0,8})\.([0-9])\.([0-9])$ ]]; then
	printf 'Unsupported fork version: %s\n' "$version" >&2
	exit 1
fi
prefix=${BASH_REMATCH[1]}
major=${BASH_REMATCH[2]}
minor=${BASH_REMATCH[3]}
patch=${BASH_REMATCH[4]}
case "$part" in
patch) patch=$((patch + 1)) ;;
minor)
	minor=$((minor + 1))
	patch=0
	;;
major)
	major=$((major + 1))
	minor=0
	patch=0
	;;
esac
minor=$((minor + patch / 10))
patch=$((patch % 10))
major=$((major + minor / 10))
minor=$((minor % 10))
if ((major > 999999999 || minor > 999999999 || patch > 999999999)); then
	printf 'Fork version component is too large.\n' >&2
	exit 1
fi
next="$prefix$major.$minor.$patch"
temporary=$(mktemp "$configure.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT
printf '%s\n' "$next" >"$temporary"
chmod --reference="$configure" "$temporary"
mv -- "$temporary" "$configure"
printf '%s\n' "$next"
