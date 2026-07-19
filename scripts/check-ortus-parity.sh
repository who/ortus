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

# ---------------------------------------------------------------------------
# Required backend artifacts (NFR-003).
#
# The walk above already byte-compares lib/backend.sh — but only while the file
# exists on the source side; deleting both copies would pass silently. And the
# Codex config template lives outside ortus/ (template/.codex/), so the walk
# never sees it at all. Assert both explicitly.

require_file() {
    local path="$1" why="$2"
    if [ ! -f "$path" ]; then
        echo "parity: MISSING  $path is required ($why)" >&2
        status=1
        return 1
    fi
}

require_file "$SOURCE/lib/backend.sh" "backend adapter — canonical copy"
require_file "$MIRROR/lib/backend.sh" "backend adapter — template mirror"

# The two backend config templates must both draw their network allowlist from
# the one computed `network_allowlist` copier answer (see copier.yaml). If
# either stops referencing it, the backends have drifted onto separate posture
# logic — the exact regression NFR-003 exists to catch. Rendered-value equality
# is covered by tests/test_codex_config_template.py; this is the cheap,
# dependency-free structural half that runs in CI via `make parity`.
CODEX_CONFIG="template/.codex/config.toml.jinja"
CLAUDE_SETTINGS="template/.claude/settings.json.jinja"

for cfg in "$CODEX_CONFIG" "$CLAUDE_SETTINGS"; do
    require_file "$cfg" "backend config template" || continue
    if ! grep -q 'network_allowlist' "$cfg"; then
        echo "parity: DIVERGED $cfg no longer references the shared network_allowlist answer" >&2
        status=1
    fi
done

if [ "$status" -eq 0 ]; then
    echo "parity: OK — $SOURCE/ and $MIRROR/ in sync"
else
    echo "parity: FAIL — see divergences above; for $SOURCE/ files, mirror into $MIRROR/ (or add a .jinja counterpart)" >&2
fi

exit "$status"
