#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ $# -ne 1 ]]; then
	echo 'Usage: scripts/source-snapshot.sh DESTINATION' >&2
	exit 2
fi
destination=$1
mkdir -p -- "$destination"
if [[ -e "$root/.git" ]]; then
	git -C "$root" ls-files --cached --others --exclude-standard -z
elif [[ -r "$root/SOURCE-FILES" ]]; then
	cat -- "$root/SOURCE-FILES"
else
	echo 'Build from a Git working tree or a bundle containing SOURCE-FILES.' >&2
	exit 1
fi | while IFS= read -r -d '' file; do
	case "$file" in
	/* | .. | ../* | */../* | */..)
		printf 'Invalid source path: %s\n' "$file" >&2
		exit 1
		;;
	esac
	# The index may still list a deleted file; copy current working-tree contents.
	if [[ -f "$root/$file" || -L "$root/$file" ]]; then
		printf '%s\0' "$file"
	fi
done >"$destination/SOURCE-FILES"
[[ -s "$destination/SOURCE-FILES" ]] || {
	echo 'No repository source files found.' >&2
	exit 1
}
version=$(bash "$root/scripts/source-version.sh")
commit=$(bash "$root/scripts/source-version.sh" --commit)
tar -C "$root" --null -T "$destination/SOURCE-FILES" -cf - | tar -C "$destination" -xf -
while IFS= read -r -d '' file; do
	# Preserve symlinks in the snapshot without hashing external or generated targets.
	[[ -L "$destination/$file" ]] || printf '%s\0' "$file"
done <"$destination/SOURCE-FILES" | (
	cd -- "$destination"
	xargs -0 -r sha256sum --
) >"$destination/SOURCE-SHA256SUMS"
[[ $version == "$(bash "$root/scripts/source-version.sh")" && $commit == "$(bash "$root/scripts/source-version.sh" --commit)" ]] || {
	printf 'Source revision changed during export; retry the snapshot.\n' >&2
	exit 1
}
printf '%s\n' "$version" >"$destination/SOURCE-VERSION"
printf '%s\n' "$commit" >"$destination/SOURCE-COMMIT"
