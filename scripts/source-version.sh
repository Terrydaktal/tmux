#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
base=$(cat "$root/FORK-VERSION")
if [[ $# -gt 1 || ($# == 1 && $1 != --commit) || ! $base =~ ^([0-9][[:alnum:].]*-fork)(0|[1-9][0-9]{0,8})\.([0-9])\.([0-9])$ ]]; then
	printf 'Invalid fork base version or unexpected arguments.\n' >&2
	exit 1
fi
prefix=${BASH_REMATCH[1]}
base_revision=$((BASH_REMATCH[2] * 100 + BASH_REMATCH[3] * 10 + BASH_REMATCH[4]))
if [[ -e "$root/.git" ]]; then
	anchor=$(cat "$root/FORK-BASE")
	[[ $anchor =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]] || {
		printf 'Invalid FORK-BASE commit.\n' >&2
		exit 1
	}
	commit=$(git -C "$root" rev-parse --verify HEAD)
	git -C "$root" merge-base --is-ancestor "$anchor" "$commit" || {
		printf 'FORK-BASE is not an available ancestor; use the full fork history.\n' >&2
		exit 1
	}
	count=$(git -C "$root" rev-list --first-parent --count "$anchor..$commit")
	[[ $count =~ ^[0-9]{1,9}$ ]] || exit 1
	# The stable upstream anchor counts as revision one before fork commits.
	revision=$((base_revision + count + 1))
	version="$prefix$((revision / 100)).$((revision / 10 % 10)).$((revision % 10))+g${commit:0:12}"
	if [[ -n $(git -C "$root" status --porcelain=v1 --untracked-files=normal) ]]; then
		version+=.dirty
	fi
	[[ $commit == "$(git -C "$root" rev-parse --verify HEAD)" ]] || {
		printf 'HEAD changed while reading source identity; retry the build.\n' >&2
		exit 1
	}
elif [[ -r "$root/SOURCE-VERSION" ]]; then
	version=$(cat "$root/SOURCE-VERSION")
	commit=$(cat "$root/SOURCE-COMMIT")
else
	printf 'Missing source provenance: build from Git or a fresh source bundle.\n' >&2
	exit 1
fi
if [[ ! $version =~ ^([0-9][[:alnum:].]*-fork)(0|[1-9][0-9]{0,8})\.([0-9])\.([0-9])\+g[0-9a-f]{12}(\.dirty)?$ || ${#version} -gt 50 ]]; then
	printf 'Invalid or oversized SOURCE-VERSION.\n' >&2
	exit 1
fi
source_prefix=${BASH_REMATCH[1]}
source_revision=$((BASH_REMATCH[2] * 100 + BASH_REMATCH[3] * 10 + BASH_REMATCH[4]))
[[ $source_prefix == "$prefix" && $source_revision -ge $base_revision ]] || {
	printf 'Source provenance disagrees with FORK-VERSION.\n' >&2
	exit 1
}
[[ $commit =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ && $version == *"+g${commit:0:12}"* ]] || {
	printf 'Source commit provenance disagrees with SOURCE-VERSION.\n' >&2
	exit 1
}
if [[ ${1:-} == --commit ]]; then
	printf '%s\n' "$commit"
else
	printf '%s\n' "$version"
fi
