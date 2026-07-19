#!/usr/bin/env bash
# Watch log files in logs/ and format JSON output nicely
# Tails files that are being actively updated

# Resolve logs dir relative to script location (works from any directory)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Defaults
LOGS_DIR="$PROJECT_ROOT/logs"
SHOW_TOOLS="${SHOW_TOOLS:-false}"
SHOW_SYSTEM="${SHOW_SYSTEM:-false}"
ASSISTANT_ONLY="${ASSISTANT_ONLY:-false}"
# FR-007: which decoder to run. Codex emits `codex exec --json` JSON Lines;
# claude emits stream-json. Normally resolved per log file from the
# `# ortus-backend:` marker goal.sh stamps at log creation (ortus-36w3);
# --codex / ORTUS_BACKEND force one decoder and skip detection entirely.
CODEX_MODE=false
BACKEND_EXPLICIT=false
case "${ORTUS_BACKEND:-}" in
    codex) CODEX_MODE=true; BACKEND_EXPLICIT=true ;;
    claude) BACKEND_EXPLICIT=true ;;
esac
DECODE_FILE=""
CODEX_DECODE_FAILED=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -a|--assistant)
            # Show only assistant messages
            ASSISTANT_ONLY=true
            shift
            ;;
        -v|--verbose)
            SHOW_TOOLS=true
            SHOW_SYSTEM=true
            shift
            ;;
        -t|--tools)
            SHOW_TOOLS=true
            shift
            ;;
        -s|--system)
            SHOW_SYSTEM=true
            shift
            ;;
        --codex)
            CODEX_MODE=true
            BACKEND_EXPLICIT=true
            shift
            ;;
        --decode)
            DECODE_FILE="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: tail.sh [OPTIONS] [LOGS_DIR]"
            echo ""
            echo "Options:"
            echo "  -a, --assistant  Show assistant messages only"
            echo "  -v, --verbose    Show all output (tools + system)"
            echo "  -t, --tools      Show tool calls"
            echo "  -s, --system     Show system messages"
            echo "      --codex      Decode codex exec --json events (default: claude stream-json)"
            echo "      --decode F   Format file F to stdout and exit (no follow)"
            echo "  -h, --help       Show this help"
            exit 0
            ;;
        -*)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
        *)
            LOGS_DIR="$1"
            shift
            ;;
    esac
done

# Color support detection
# Respects NO_COLOR (https://no-color.org/) and checks terminal capabilities
setup_colors() {
    if [[ -n "${NO_COLOR:-}" ]] || [[ ! -t 1 ]]; then
        # No colors: NO_COLOR set or not a terminal
        BLUE='' GREEN='' YELLOW='' CYAN='' MAGENTA='' RED='' DIM='' BOLD='' RESET=''
    elif command -v tput &>/dev/null && [[ $(tput colors 2>/dev/null || echo 0) -ge 8 ]]; then
        # Use tput for terminal-aware colors
        BOLD=$(tput bold)
        DIM=$(tput dim 2>/dev/null || echo '')
        RESET=$(tput sgr0)
        # Use basic ANSI colors (0-7) which have consistent meaning across themes
        # These adapt to the terminal's color scheme
        RED=$(tput setaf 1)
        GREEN=$(tput setaf 2)
        YELLOW=$(tput setaf 3)
        BLUE=$(tput setaf 4)
        MAGENTA=$(tput setaf 5)
        CYAN=$(tput setaf 6)
    else
        # Fallback to basic ANSI codes
        BOLD='\033[1m'
        DIM='\033[2m'
        RESET='\033[0m'
        RED='\033[31m'
        GREEN='\033[32m'
        YELLOW='\033[33m'
        BLUE='\033[34m'
        MAGENTA='\033[35m'
        CYAN='\033[36m'
    fi
}

setup_colors

# Track tailed files using a temp file (works across subshells)
TAILED_LIST=$(mktemp)
trap "rm -f '$TAILED_LIST'; kill 0 2>/dev/null" EXIT INT TERM

is_tailed() {
    grep -qxF "$1" "$TAILED_LIST" 2>/dev/null
}

mark_tailed() {
    echo "$1" >> "$TAILED_LIST"
}

# --- Decoder selection (ortus-36w3) ----------------------------------------
# Logs are self-describing: goal.sh writes `# ortus-backend: <name>` as the
# first line of every log it creates. Detection reads that marker and nothing
# else — sniffing the JSON payload would guess, and a wrong guess renders
# garbage. A markerless or unknown-backend log is a hard error, not a
# best-effort render; --codex / ORTUS_BACKEND remain the manual override for
# raw logs captured outside goal.sh.
read_backend_marker() {
    head -n 20 "$1" 2>/dev/null \
        | sed -n 's/^# ortus-backend: \([a-z][a-z0-9_-]*\)$/\1/p' \
        | head -n 1
}

# Echoes "true"/"false" for CODEX_MODE, or fails with a diagnostic on stderr.
resolve_codex_mode() {
    local file="$1" backend
    if [ "$BACKEND_EXPLICIT" = "true" ]; then
        echo "$CODEX_MODE"
        return 0
    fi
    backend="$(read_backend_marker "$file")"
    case "$backend" in
        codex)  echo "true" ;;
        claude) echo "false" ;;
        "")
            echo "$(basename "$file"): no '# ortus-backend:' marker — cannot pick a decoder." >&2
            echo "  Logs created by goal.sh carry one. For a raw log, pass --codex or set ORTUS_BACKEND=claude|codex." >&2
            return 1
            ;;
        *)
            echo "$(basename "$file"): unknown backend marker '$backend' — cannot pick a decoder." >&2
            echo "  Pass --codex or set ORTUS_BACKEND=claude|codex to decode it anyway." >&2
            return 1
            ;;
    esac
}

start_tail() {
    local file="$1" mode
    if ! is_tailed "$file"; then
        mark_tailed "$file"
        if ! mode="$(resolve_codex_mode "$file")"; then
            return 1
        fi
        echo -e "${BOLD}${MAGENTA}=== TAILING: $(basename "$file") ===${RESET}"
        (
            CODEX_MODE="$mode"
            tail -f "$file" 2>/dev/null | while IFS= read -r line; do
                decode_line "$line"
            done
        ) &
    fi
}

decode_line() {
    if [ "$CODEX_MODE" = "true" ]; then
        format_codex_line "$1"
    else
        format_line "$1"
    fi
}

# --- Codex `codex exec --json` decoder (FR-007) ----------------------------
#
# Event vocabulary pinned by the Q2 spike (ortus-l75g); fixtures live at
# tests/fixtures/codex-exec-events*.jsonl. Every field is read by typed path
# (.type, .item.type, .usage.*) — never by grepping free text — and the render
# is byte-identical to the Python decoder in src/ortus/commands/tail.py, which
# the golden-render test in tests/test_codex_tail_decoder.py enforces.
CODEX_JQ='
def trunc($n): tostring | if length <= $n then . else .[0:$n] + "..." end;
def w($codes; $text): ($codes | join("")) as $c |
    if $c == "" then $text else $c + $text + $reset end;
def item_lines($item; $started):
    ($item.type) as $t |
    if $t == "agent_message" then
        if $started or (($item.text // "") == "") then []
        else ["", w([$bold,$green]; "<<< ASSISTANT"), w([$green]; $item.text)] end
    elif $t == "reasoning" then
        if $started or $show_system != "true" or (($item.text // "") == "") then []
        else [w([$dim]; "  (thinking) " + ($item.text | trunc(200)))] end
    elif $t == "command_execution" then
        if $show_tools != "true" then []
        elif $started then
            [w([$yellow]; "  [TOOL] command_execution"),
             w([$dim]; "  " + (($item.command // "") | trunc(200)))]
        else
            (($item.aggregated_output // "") | sub("\n+$"; "")) as $body |
            (if ($item.status == "failed")
                 or (($item.exit_code != null) and ($item.exit_code != 0))
             then w([$red]; "  [RESULT] command_execution: ERROR (exit "
                            + ($item.exit_code | tostring) + ")")
             else w([$cyan]; "  [RESULT] command_execution: "
                             + ($item.status // "?")) end) as $header |
            if $body == "" then [$header]
            else [$header, w([$dim]; "  " + ($body | trunc(200)))] end
        end
    elif $t == "todo_list" then
        if $show_system != "true" then []
        else
            ($item.items // []) as $entries |
            [w([$dim]; "  [TODO] "
                       + ([$entries[] | select(.completed)] | length | tostring)
                       + "/" + ($entries | length | tostring))]
            + [$entries[] | w([$dim]; "    ["
                              + (if .completed then "x" else " " end) + "] "
                              + (.text // ""))]
        end
    elif $t == "error" then
        if $started then [] else [w([$red]; "  [ERROR] " + ($item.message // ""))] end
    elif $show_system == "true" then [w([$dim]; "[SYS] item." + ($t | tostring))]
    else [] end;
if type != "object" then error("event is not a JSON object")
elif ((.type // "") == "") then error("event has no `type` field")
else
    (.type) as $k |
    if $k == "thread.started" then
        ["", w([$bold,$magenta]; "=== NEW SESSION ==="),
         w([$magenta]; (.thread_id // "?" | tostring))]
    elif $k == "turn.completed" then
        (.usage // {}) as $u |
        [w([$cyan]; "  [USAGE] input=" + (($u.input_tokens // 0) | tostring)
                    + " cached=" + (($u.cached_input_tokens // 0) | tostring)
                    + " output=" + (($u.output_tokens // 0) | tostring)
                    + " reasoning=" + (($u.reasoning_output_tokens // 0) | tostring))]
    elif $k == "turn.failed" then
        [w([$red]; "  [TURN FAILED] " + ((.error // {}).message // ""))]
    elif $k == "error" then [w([$red]; "  [ERROR] " + (.message // ""))]
    elif ($k == "item.started" or $k == "item.completed") then
        (if (.item | type) == "object"
         then item_lines(.item; $k == "item.started") else [] end)
    elif $show_system == "true" then [w([$dim]; "[SYS] " + ($k | tostring))]
    else [] end
end | .[]
'

format_codex_line() {
    local line="$1"
    [ -z "$line" ] && return 0

    if [[ "$line" != "{"* ]]; then
        format_plain_line "$line"
        return 0
    fi

    # jq is silent on stderr for a successful decode, so folding stderr into
    # the capture is safe and keeps this allocation-free per line.
    local rendered
    if rendered=$(printf '%s' "$line" | jq -r \
            --arg bold "$BOLD" --arg dim "$DIM" --arg reset "$RESET" \
            --arg red "$RED" --arg green "$GREEN" --arg yellow "$YELLOW" \
            --arg magenta "$MAGENTA" --arg cyan "$CYAN" \
            --arg show_tools "$SHOW_TOOLS" --arg show_system "$SHOW_SYSTEM" \
            "$CODEX_JQ" 2>&1); then
        [ -n "$rendered" ] && printf '%s\n' "$rendered"
        return 0
    fi

    # Loud, non-silent failure: a truncated write or a schema change must be
    # visible to the operator, not swallowed into an eerily quiet log.
    local reason
    reason=$(printf '%s' "$rendered" | tr '\n' ' ')
    CODEX_DECODE_FAILED=true
    local diagnostic="${BOLD}${RED}!!! CODEX DECODE ERROR: ${reason%% }: ${line:0:200}${RESET}"
    printf '%s\n' "$diagnostic"
    printf '%s\n' "$diagnostic" >&2
    return 0
}

format_line() {
    local line="$1"

    # Token usage on the session-end result event. The Codex branch renders
    # this class from turn.completed; the M3 parity check (ortus-iwac) is what
    # surfaced its absence here. Read by typed path, and emitted unconditionally
    # so the class survives without -t, exactly as the Codex [USAGE] line does.
    if [[ "$line" == "{"* ]]; then
        local usage
        usage=$(printf '%s' "$line" | jq -r '
            select(.type == "result" and (.usage | type) == "object")
            | "  [USAGE] input=\(.usage.input_tokens // 0)"
              + " cached=\(.usage.cache_read_input_tokens // 0)"
              + " output=\(.usage.output_tokens // 0)"
        ' 2>/dev/null)
        [ -n "$usage" ] && echo -e "${CYAN}${usage}${RESET}"
    fi

    # Try to parse as JSON
    parsed=$(echo "$line" | jq -r '
        if .type == "user" then
            "USER|\(.message.content // .message // "?")"
        elif .type == "assistant" then
            .message.content[]? |
            if .type == "text" then
                "ASSISTANT|\(.text // empty)"
            elif .type == "tool_use" then
                "TOOL_CALL|\(.name)|\(.input | tostring | .[0:200])"
            else
                empty
            end
        elif .type == "result" then
            "RESULT|\(.tool)|\(.subtype // "ok")|\((.result // .error // "") | tostring | .[0:300])"
        elif .type == "system" and .subtype == "init" then
            "INIT|Session started: \(.session_id)"
        elif .type == "system" then
            "SYSTEM|\(.subtype // "info")"
        else
            empty
        end
    ' 2>/dev/null)

    if [ -n "$parsed" ]; then
        type=$(echo "$parsed" | cut -d'|' -f1)
        content=$(echo "$parsed" | cut -d'|' -f2-)

        case "$type" in
            USER)
                if [ "$ASSISTANT_ONLY" != "true" ]; then
                    echo -e "\n${BOLD}${BLUE}>>> USER${RESET}"
                    echo -e "${BLUE}$content${RESET}"
                fi
                ;;
            ASSISTANT)
                echo -e "\n${BOLD}${GREEN}<<< ASSISTANT${RESET}"
                echo -e "${GREEN}$content${RESET}"
                ;;
            TOOL_CALL)
                if [ "$SHOW_TOOLS" = "true" ]; then
                    tool_name=$(echo "$content" | cut -d'|' -f1)
                    tool_input=$(echo "$content" | cut -d'|' -f2-)
                    echo -e "${YELLOW}  [TOOL] ${tool_name}${RESET}"
                    echo -e "${DIM}  ${tool_input}${RESET}"
                fi
                ;;
            RESULT)
                if [ "$SHOW_TOOLS" = "true" ]; then
                    tool=$(echo "$content" | cut -d'|' -f1)
                    subtype=$(echo "$content" | cut -d'|' -f2)
                    result=$(echo "$content" | cut -d'|' -f3-)
                    if [ "$subtype" = "error" ]; then
                        echo -e "${RED}  [RESULT] ${tool}: ERROR${RESET}"
                    else
                        echo -e "${CYAN}  [RESULT] ${tool}: ${subtype}${RESET}"
                    fi
                    echo -e "${DIM}  ${result:0:200}...${RESET}"
                fi
                ;;
            INIT)
                echo -e "\n${BOLD}${MAGENTA}=== NEW SESSION ===${RESET}"
                echo -e "${MAGENTA}$content${RESET}"
                ;;
            SYSTEM)
                if [ "$SHOW_SYSTEM" = "true" ]; then
                    echo -e "${DIM}[SYS] $content${RESET}"
                fi
                ;;
        esac
    else
        format_plain_line "$line"
    fi
}

# Non-JSON line (plain text banners from goal.sh). Shared by both decoders —
# the Codex branch renders plain lines identically to the Claude branch.
format_plain_line() {
    local line="$1"
    # The decoder-selection marker (ortus-36w3) is plumbing, not run output.
    if [[ "$line" == "# ortus-backend: "* ]]; then
        return 0
    fi
    if [[ "$line" == "==="* ]]; then
        echo -e "\n${BOLD}${CYAN}$line${RESET}"
    elif [[ "$line" == "Processing:"* ]] || [[ "$line" == "Found"* ]]; then
        echo -e "${CYAN}$line${RESET}"
    elif [[ "$line" == *"error"* ]] || [[ "$line" == *"Error"* ]] || [[ "$line" == *"ERROR"* ]]; then
        echo -e "${RED}$line${RESET}"
    elif [[ "$line" == *"success"* ]] || [[ "$line" == *"Success"* ]] || [[ "$line" == *"completed"* ]]; then
        echo -e "${GREEN}$line${RESET}"
    elif [ -n "$line" ]; then
        echo -e "${DIM}$line${RESET}"
    fi
}

# One-shot decode of a single file (no follow). Used by the golden-render
# tests and handy for post-mortem reading of a finished run's log.
if [ -n "$DECODE_FILE" ]; then
    # Decode mode never backgrounds a tail, so drop the watcher's
    # kill-the-process-group trap — it would mask the real exit status.
    trap 'rm -f "$TAILED_LIST"' EXIT INT TERM
    if [ ! -f "$DECODE_FILE" ]; then
        echo "No such file: $DECODE_FILE" >&2
        exit 1
    fi
    CODEX_MODE="$(resolve_codex_mode "$DECODE_FILE")" || exit 1
    while IFS= read -r line || [ -n "$line" ]; do
        decode_line "$line"
    done < "$DECODE_FILE"
    [ "$CODEX_DECODE_FAILED" = "true" ] && exit 65
    exit 0
fi

echo -e "${BOLD}Watching logs in: ${LOGS_DIR}${RESET}"
echo -e "${DIM}Set SHOW_TOOLS=true to see tool calls${RESET}"
echo -e "${DIM}Set SHOW_SYSTEM=true to see system messages${RESET}"
echo -e "${DIM}Set NO_COLOR=1 to disable colors${RESET}"
echo ""

# Watch for file modifications
if command -v inotifywait &> /dev/null; then
    echo -e "${GREEN}Watching for file updates...${RESET}"
    echo ""

    inotifywait -m -q -e modify -e create "${LOGS_DIR}" 2>/dev/null | while read -r dir event file; do
        if [[ "$file" == ralph-*.log || "$file" == goal-*.log ]]; then
            start_tail "${LOGS_DIR}/${file}"
        fi
    done
else
    echo -e "${YELLOW}Note: Install inotify-tools for instant detection${RESET}"
    echo -e "${YELLOW}Polling every 2s for recently modified files...${RESET}"
    echo ""

    while true; do
        # Find files modified in the last 60 seconds
        for f in "${LOGS_DIR}"/ralph-*.log "${LOGS_DIR}"/goal-*.log; do
            if [ -f "$f" ]; then
                # Check if modified in last 60 seconds
                if [ "$(find "$f" -mmin -1 2>/dev/null)" ]; then
                    start_tail "$f"
                fi
            fi
        done
        sleep 2
    done
fi
