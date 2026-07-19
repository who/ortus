#!/usr/bin/env bash
# triage.sh — deprecation shim. All work delegates to the `ortus triage` verb.
#
# Background: ortus-sr0b established that AskUserQuestion is an interactive-only
# surface — under `claude -p` the calls silently return is_error and the agent
# exits, which this wrapper used to read as success. The operator saw
# "triage complete" while the human queue was never drained (ortus-4md0).
#
# The Python verb (src/ortus/commands/triage.py) was rebuilt on the
# envelope-driven two-phase flow that fixes this: claude gathers context into
# logs/triage-envelopes.jsonl under -p, then the wrapper — which owns a real
# stdin — prompts the operator and applies dispositions via bd. Rather than
# port 260 lines of operator I/O back into bash for a tree that Phase 5
# deletes wholesale (docs/sunset-notes.md), this script retires in favour of
# the verb, mirroring the ralph.sh -> goal.sh shim.
#
# Backend support: the operator loop is backend-agnostic by construction, but
# the context phase is still a `claude -p` session (src/ortus/core/claude.py),
# so under --backend codex this shim refuses up front instead of silently
# running Claude behind a codex-selected project (NFR-005). Refusing costs one
# message and never blocks (NFR-004).
#
# Usage: ./ortus/triage.sh [--backend claude|codex] [args...]
#        Remaining args are forwarded verbatim to `ortus triage`
#        ./ortus/triage.sh -h | --help
#
# Exit codes:
#   *   Whatever `ortus triage` exits with, forwarded verbatim
#   1   `ortus` CLI not installed
#   3   Selected backend cannot run this flow

set -uo pipefail

BACKEND_FLAG=""
ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help)
            sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        --backend)
            BACKEND_FLAG="${2:-}"
            shift 2
            ;;
        --backend=*)
            BACKEND_FLAG="${1#--backend=}"
            shift
            ;;
        *)
            ARGS+=("$1")
            shift
            ;;
    esac
done

# shellcheck source=lib/backend.sh
. "$(dirname "$0")/lib/backend.sh"
BACKEND=$(resolve_backend "$BACKEND_FLAG") || exit 1
export ORTUS_BACKEND="$BACKEND"

if [ "$BACKEND" != "claude" ]; then
    cat >&2 <<EOF
triage.sh: triage is not available under the '$BACKEND' backend.

The operator prompt loop is backend-agnostic, but the context-gathering phase
still runs as a \`claude -p\` session. Running it under a codex-selected
project would quietly use a different agent than the one you asked for, so
this script stops here instead.

Run it on Claude:  ./ortus/triage.sh --backend claude
EOF
    exit 3
fi

echo '[triage.sh] deprecated; delegating to `ortus triage`. See README.' >&2

if ! command -v ortus >/dev/null 2>&1; then
    cat >&2 <<'EOF'
triage.sh: the `ortus` CLI is not on PATH.

The bash triage harness has been retired: its interactive prompt flow was
unreachable under `claude -p`, so it silently drained nothing while
reporting success. Install the CLI:

    uv tool install ortus

then re-run `ortus triage` (or this script, which forwards to it).
EOF
    exit 1
fi

exec ortus triage ${ARGS[@]+"${ARGS[@]}"}
