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
# Usage: ./ortus/triage.sh [args...]   # forwarded verbatim to `ortus triage`
#        ./ortus/triage.sh -h | --help
#
# Exit codes:
#   *   Whatever `ortus triage` exits with, forwarded verbatim
#   1   `ortus` CLI not installed

set -uo pipefail

case "${1:-}" in
    -h|--help)
        sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
esac

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

exec ortus triage "$@"
