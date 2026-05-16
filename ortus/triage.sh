#!/usr/bin/env bash
# triage.sh - Interactive claude harness for draining the human-decision bd queue.
#
# Walks the `human`-labeled bd queue and presents each issue to the operator
# via AskUserQuestion: defer / close / revise AC / dismiss / skip. The chosen
# disposition is applied through `bd` commands by the claude session itself.
#
# Read-only outside bd; this script never edits code. The claude session is
# launched with allowedTools restricted to AskUserQuestion + Bash(bd:*) + Read.
#
# Usage: ./ortus/triage.sh
#        ./ortus/triage.sh -h | --help
#
# Exit codes:
#   0   Graceful completion or empty queue
#   1   Internal error (missing prompt, missing deps, claude exit != 0/130)
#   130 Operator Ctrl+C mid-session

set -uo pipefail

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "triage.sh: unknown option: $1" >&2
            exit 1
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

PROMPT_FILE="$SCRIPT_DIR/prompts/triage-prompt.md"
if [[ ! -f "$PROMPT_FILE" ]]; then
    echo "triage.sh: prompt template not found at $PROMPT_FILE" >&2
    exit 1
fi

for cmd in bd jq claude; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "triage.sh: required dependency '$cmd' not found on PATH" >&2
        exit 1
    fi
done

# Pre-flight: if the human queue is already empty, exit cleanly without
# spinning up a claude session. AC (4) allows either the wrapper or claude
# to handle this; doing it here is cheaper and avoids a noisy claude launch.
queue_json=$(bd list --label=human --json 2>/dev/null || printf '[]')
if ! printf '%s' "$queue_json" | jq empty >/dev/null 2>&1; then
    queue_json='[]'
fi
open_count=$(printf '%s' "$queue_json" | jq '[.[] | select(.status != "closed")] | length')
if [[ "$open_count" == "0" ]]; then
    echo "No human-queue items. Exiting."
    exit 0
fi

prompt_body=$(cat "$PROMPT_FILE")

# Bootstrap directive: ensure claude's first move is a bd read, not narration.
initial='Your FIRST action must be to run a bd command (e.g. bd list --label=human --json) to discover the human queue. Do not output prose before that first tool call.'

full_prompt="${prompt_body}

---

${initial}"

# Launch claude. allowedTools is intentionally minimal:
#   AskUserQuestion  - the operator-choice surface
#   Bash(bd:*)       - read and mutate bd issues
#   Read             - inspect in-repo context (PRDs etc.) when explaining trade-offs
# No Edit, no Write, no generic Bash. Triage decisions never modify code; if
# the decision implies code work, claude routes via "Dismiss" back to the
# ralph/goal loops, which is the seam for code-changing work.
claude --allowedTools "AskUserQuestion,Bash(bd:*),Read" -p "$full_prompt"
ec=$?

if [[ $ec -eq 0 ]]; then
    exit 0
elif [[ $ec -eq 130 ]]; then
    echo ""
    echo "triage.sh: interrupted; bd writes that already completed remain committed." >&2
    exit 130
else
    echo "triage.sh: claude session exited with code $ec" >&2
    exit 1
fi
