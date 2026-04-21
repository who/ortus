#!/usr/bin/env bash
# Check parity between ortus/ (working copy) and template/ortus/ (distributable mirror).
#
# For every regular file under ortus/, verify either:
#   (a) template/ortus/<path> exists and is byte-identical, OR
#   (b) template/ortus/<path>.jinja exists (templated counterpart — may legitimately differ).
#
# Exits 0 on parity, 1 on divergence (with a clear message naming each divergent file).

set -u

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$repo_root"

SOURCE="ortus"
MIRROR="template/ortus"

if [ ! -d "$SOURCE" ] || [ ! -d "$MIRROR" ]; then
    echo "parity: $SOURCE/ or $MIRROR/ missing — nothing to check" >&2
    exit 0
fi

status=0

while IFS= read -r -d '' f; do
    rel="${f#$SOURCE/}"

    # Source-side .jinja files would be unusual; skip defensively so they never
    # generate a false "MISSING mirror" (their mirror would have a double .jinja).
    case "$rel" in *.jinja) continue ;; esac

    dest="$MIRROR/$rel"
    dest_jinja="$dest.jinja"

    if [ -f "$dest_jinja" ]; then
        # Templated counterpart exists; parity check not applicable.
        continue
    fi

    if [ ! -f "$dest" ]; then
        echo "parity: MISSING  $f has no counterpart at $dest (or $dest_jinja)" >&2
        status=1
        continue
    fi

    if ! cmp -s "$f" "$dest"; then
        echo "parity: DIVERGED $f differs from $dest" >&2
        status=1
    fi
done < <(find "$SOURCE" -type f -print0)

if [ "$status" -eq 0 ]; then
    echo "parity: OK — $SOURCE/ and $MIRROR/ in sync"
else
    echo "parity: FAIL — mirror $SOURCE/ into $MIRROR/ (or add a .jinja counterpart)" >&2
fi

exit "$status"
