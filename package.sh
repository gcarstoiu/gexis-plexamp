#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Build dist/plexamp-<version>.tar.gz with plugin.json at its top level: the
# adapter's code, its run script, Plexamp's pin and its mark - Plexamp itself
# is downloaded on the player. The same files give the same bytes, so a listed
# archive's sha256 can be checked against the tag it was built from.
set -eu
cd "$(dirname "$0")"
mkdir -p dist
version=$(python3 -c 'import json; print(json.load(open("plugin.json"))["version"])')
grep -q "^version = \"$version\"" pyproject.toml || { echo "ERROR: pyproject.toml is not $version" >&2; exit 1; }
out="dist/plexamp-$version.tar.gz"
files=$(git ls-files plugin.json run plexamp.pin mark.png LICENSE 'src/gexis_plexamp/*.py')
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --mode=u+w,go-w \
	-cf - $files | gzip -n -9 > "$out"
echo "$out $(stat -c %s "$out") bytes, sha256 $(sha256sum "$out" | cut -d' ' -f1)"
