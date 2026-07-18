#!/usr/bin/env bash
# idea.sh - Quick feature creation for Ralph workflow
#
# Usage: ./ortus/idea.sh                           # Interactive menu
#        ./ortus/idea.sh "Your idea description"   # Create from idea
#        ./ortus/idea.sh --prd <path>              # Process existing PRD
#        ./ortus/idea.sh --backend claude|codex    # Override the agent backend
#
# Creates a feature bead. After creating the idea:
#   ./ortus/interview.sh   # Interactive interview → PRD → task creation
#   ./ortus/ralph.sh       # Implements the tasks

set -euo pipefail

# Resolve ortus directory for prompt file access (must be done before cd)
ORTUS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Backend resolution (FR-002). Stripped off the front of the argv before the
# positional handling below, so `--backend codex --prd foo.md` and the bare
# `--prd foo.md` reach the same code path. resolve_backend in lib/backend.sh
# owns the flag > env > generated-default precedence; idea.sh only supplies
# the flag value and exports the answer (NFR-002).
source "$ORTUS_DIR/lib/backend.sh"
BACKEND_FLAG=""
if [[ "${1:-}" == "--backend" ]]; then
    if [[ -z "${2:-}" ]]; then
        echo "Error: --backend requires a value (claude|codex)" >&2
        exit 1
    fi
    BACKEND_FLAG="$2"
    shift 2
fi
ORTUS_BACKEND="$(resolve_backend "$BACKEND_FLAG")" || exit 1
export ORTUS_BACKEND

# Handle PRD intake flow
handle_prd() {
    local prd_path="${1:-}"

    # If no path provided as argument, prompt for it
    if [[ -z "$prd_path" ]]; then
        echo "Sweet! What's the path to your PRD?"
        read -r -p "> " prd_path

        if [[ -z "$prd_path" ]]; then
            echo "No path provided. Exiting."
            exit 1
        fi
    fi

    # Convert to absolute path for reliable access after cd
    prd_path=$(realpath "$prd_path" 2>/dev/null || echo "$prd_path")

    if [[ ! -f "$prd_path" ]]; then
        echo "Hmm, I can't find a file at '$prd_path'. Double-check the path?"
        exit 1
    fi

    # Find the project directory containing the PRD
    prd_dir=$(dirname "$prd_path")
    project_dir=$(cd "$prd_dir" && git rev-parse --show-toplevel 2>/dev/null || echo "$prd_dir")

    echo "Processing your PRD in $project_dir..."

    # Save current directory and switch to project
    original_dir=$(pwd)
    cd "$project_dir"

    prompt_file="$ORTUS_DIR/prompts/prd-decompose-prompt.md"
    if [[ ! -f "$prompt_file" ]]; then
        echo "ERROR: PRD decomposition prompt not found at $prompt_file" >&2
        exit 1
    fi
    prompt_template="$(cat "$prompt_file")"
    prompt_body="${prompt_template//\$prd_path/$prd_path}"

    # /goal CONDITION — sets the termination condition so the user no longer
    # needs to type /exit (FR-019). Q5 placed condition strings in
    # ortus/prompts/conditions/*.txt so make parity / diff can detect drift.
    condition_file="$ORTUS_DIR/prompts/conditions/prd-decomposed.txt"
    if [[ ! -f "$condition_file" ]]; then
        echo "ERROR: /goal condition file not found at $condition_file" >&2
        exit 1
    fi
    condition_template="$(cat "$condition_file")"
    if [[ "$condition_template" == "TODO PLACEHOLDER"* ]]; then
        echo "ERROR: condition file at $condition_file is still a placeholder; refusing to launch /goal with TODO PLACEHOLDER text" >&2
        exit 1
    fi
    condition="${condition_template//\{\{PRD_PATH\}\}/$prd_path}"

    full_prompt="/goal ${condition}

${prompt_body}"

    backend_argv prd-decompose "$full_prompt" "$prd_path" || exit 1
    "${BACKEND_ARGV[@]}"

    # Return to original directory
    cd "$original_dir"

    echo ""
    echo "Next steps:"
    echo "cd $(basename "$project_dir") && ./ortus/ralph.sh"
}

# Handle idea intake flow
handle_idea() {
    local idea="${1:-}"

    if [[ -z "$idea" ]]; then
        echo "What's your idea?"
        read -r -p "> " idea
        if [[ -z "$idea" ]]; then
            echo "No idea provided. Exiting."
            exit 1
        fi
    fi

    echo "Expanding your idea..."
    prompt_file="$ORTUS_DIR/prompts/idea-expand-prompt.md"
    if [[ ! -f "$prompt_file" ]]; then
        echo "ERROR: Idea expansion prompt not found at $prompt_file" >&2
        exit 1
    fi
    prompt_template="$(cat "$prompt_file")"
    # stdout is the product here, so the adapter's idea-expand role deliberately
    # carries no stream flags — anything it printed would land in $description.
    backend_argv idea-expand "$prompt_template

Idea: $idea" || exit 1
    description=$("${BACKEND_ARGV[@]}")

    local feature_id
    if [[ -z "$description" ]]; then
        feature_id=$(bd create --title="$idea" --type=feature --json | jq -r '.id')
    else
        feature_id=$(bd create --title="$idea" --type=feature --body="$description" --json | jq -r '.id')
    fi

    echo ""
    echo "Feature created: $feature_id"
    echo "Starting interview to build your PRD..."
    echo ""

    # Kick off interview flow
    ./ortus/interview.sh "$feature_id"
}

# Main flow

# Handle help flag
case "${1:-}" in
    -h|--help)
        head -n 11 "$0" | tail -n +2 | sed 's/^# //' | sed 's/^#//'
        exit 0
        ;;
esac

# Check for --prd flag
if [[ "${1:-}" == "--prd" ]]; then
    if [[ -z "${2:-}" ]]; then
        echo "Error: --prd requires a path argument"
        echo "Usage: ./ortus/idea.sh --prd <path>"
        exit 1
    fi
    handle_prd "$2"
    exit 0
fi

idea="${1:-}"

# If idea provided as argument, skip menu and go straight to idea flow
if [[ -n "$idea" ]]; then
    handle_idea "$idea"
    exit 0
fi

# Interactive menu
echo "Got a PRD already? Don't sweat it if not—we'll build one together."
echo ""
echo "  [1] Yes, I have a PRD"
echo "  [2] Nope, just an idea"
echo ""
read -r -p "Your choice: " choice

case "$choice" in
    1)
        handle_prd
        ;;
    2|"")
        handle_idea
        ;;
    *)
        echo "Invalid choice. Please enter 1 or 2."
        exit 1
        ;;
esac
