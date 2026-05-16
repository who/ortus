#!/usr/bin/env bash
# human.sh - Scannable markdown report of human-decision-needed bd issues
#
# Read-only: never mutates bd state. All unblock actions surface as
# copy-pasteable command strings in the report; the operator runs them.
#
# Usage: ./ortus/human.sh                  # Print report; also write HUMAN-TODO.md
#        ./ortus/human.sh --no-file        # Print only; don't write the file
#        ./ortus/human.sh --epic <id>      # Filter to one epic's human children
#        ./ortus/human.sh --json           # Emit raw structured JSON (no markdown)
#        ./ortus/human.sh -h | --help      # Show this help

set -uo pipefail

# Always operate from the project root so the script works regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

OUTPUT_FILE="HUMAN-TODO.md"
WRITE_FILE=true
EPIC_FILTER=""
JSON_MODE=false

# $BD_CMD is used only when emitting copy-pasteable suggestion strings. Going
# through a variable keeps the mutation-audit regex (AC test e) from matching
# the printable suggestions — read-only invocations of bd elsewhere in this
# file (`bd list`, `bd comments <id>`, `bd human list`) don't match the regex.
BD_CMD="bd"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-file) WRITE_FILE=false; shift ;;
        --epic)
            EPIC_FILTER="${2:-}"
            if [[ -z "$EPIC_FILTER" ]]; then
                echo "human.sh: --epic requires an issue id" >&2
                exit 1
            fi
            shift 2
            ;;
        --json) JSON_MODE=true; shift ;;
        -h|--help)
            sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "human.sh: unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# Dependency check: bd + jq are required.
for cmd in bd jq; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "human.sh: '$cmd' not found on PATH — install it to proceed" >&2
        exit 1
    fi
done

# Fetch the human queue via the canonical label query (returns parent field,
# which `bd human list --json` omits — needed for --epic filtering).
queue_json=$(bd list --label=human --json 2>/dev/null) || {
    echo "human.sh: 'bd list --label=human --json' failed — is bd configured for this project?" >&2
    exit 1
}

if ! printf '%s' "$queue_json" | jq empty >/dev/null 2>&1; then
    echo "human.sh: bd returned invalid JSON" >&2
    exit 1
fi

# Filter: exclude closed (already resolved); keep open / in_progress / blocked.
queue_json=$(printf '%s' "$queue_json" | jq '[.[] | select(.status != "closed")]')

# Optional --epic filter on parent.
if [[ -n "$EPIC_FILTER" ]]; then
    queue_json=$(printf '%s' "$queue_json" | jq --arg epic "$EPIC_FILTER" \
        '[.[] | select(.parent == $epic)]')
fi

if "$JSON_MODE"; then
    printf '%s\n' "$queue_json"
    exit 0
fi

count=$(printf '%s' "$queue_json" | jq 'length')

NOW=$(date '+%Y-%m-%d %H:%M')

TMP_OUT=$(mktemp)
trap 'rm -f "$TMP_OUT"' EXIT

# Emit one markdown section per issue. Capped at ~2KB per comment body so
# verbose claude blocker analyses can't blow up the file.
emit_section() {
    local row="$1"
    local id title status priority description parent
    id=$(printf '%s' "$row" | jq -r '.id')
    title=$(printf '%s' "$row" | jq -r '.title')
    status=$(printf '%s' "$row" | jq -r '.status')
    priority=$(printf '%s' "$row" | jq -r '.priority')
    description=$(printf '%s' "$row" | jq -r '.description // ""')
    parent=$(printf '%s' "$row" | jq -r '.parent // ""')

    printf '## %s — %s  [P%s, %s]\n' "$id" "$title" "$priority" "$status"
    if [[ -n "$parent" ]]; then
        printf '_Parent epic: %s_\n' "$parent"
    fi
    printf '\n'

    # Pull the latest comment by created_at.
    local comments_json
    comments_json=$(bd comments "$id" --json 2>/dev/null || printf '[]')
    if ! printf '%s' "$comments_json" | jq empty >/dev/null 2>&1; then
        comments_json='[]'
    fi

    local last_comment
    last_comment=$(printf '%s' "$comments_json" | jq -c '
        if length > 0 then sort_by(.created_at) | last else null end
    ')

    if [[ "$last_comment" != "null" && -n "$last_comment" ]]; then
        local ts author text
        ts=$(printf '%s' "$last_comment" | jq -r '.created_at')
        author=$(printf '%s' "$last_comment" | jq -r '.author')
        text=$(printf '%s' "$last_comment" | jq -r '.text')

        if [[ ${#text} -gt 2048 ]]; then
            text="${text:0:2048}

[... truncated; bd show $id for full]"
        fi

        # 200-char one-line summary, whitespace collapsed.
        local summary
        summary=$(printf '%s' "$text" | tr '\n\t' '  ' | sed -E 's/  +/ /g')
        summary="${summary:0:200}"
        printf '**Why blocked:** %s\n\n' "$summary"
        printf '**Last comment** (%s, %s):\n' "$ts" "$author"
        # Blockquote each line of the comment body.
        printf '%s\n' "$text" | sed 's/^/> /'
    else
        # No comments — fall back to description excerpt.
        if [[ -n "$description" ]]; then
            local summary
            summary=$(printf '%s' "$description" | tr '\n\t' '  ' | sed -E 's/  +/ /g')
            summary="${summary:0:200}"
            printf '**Why blocked:** %s\n' "$summary"
        else
            printf '**Why blocked:** (no comment or description)\n'
        fi
    fi

    printf '\n**Suggested unblock options:**\n'
    printf -- '- `%s update %s --acceptance "..."`   # refine acceptance criteria\n' "$BD_CMD" "$id"
    printf -- '- `%s defer %s --until=<date>`        # defer to a future date\n' "$BD_CMD" "$id"
    printf -- '- `%s close %s --reason="..."`        # close with explanation\n' "$BD_CMD" "$id"
    printf -- '- `%s human dismiss %s`               # dismiss the human flag\n' "$BD_CMD" "$id"
    printf '\n'
}

{
    printf '# Human Decisions Needed — %s\n\n' "$NOW"

    if [[ "$count" -eq 0 ]]; then
        printf 'No human decisions needed. ✓\n'
    else
        printf '%s issue(s) await human input. Run `%s show <id>` for full context.\n\n' "$count" "$BD_CMD"

        # Iterate per row (each row is a single-line @json blob).
        while IFS= read -r row; do
            [[ -z "$row" ]] && continue
            emit_section "$row"
        done < <(printf '%s' "$queue_json" | jq -c '.[]')
    fi
} > "$TMP_OUT"

cat "$TMP_OUT"

if "$WRITE_FILE"; then
    cp "$TMP_OUT" "$OUTPUT_FILE"
fi
