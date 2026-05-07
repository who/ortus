#!/usr/bin/env bash
# idea.sh - Quick feature creation for Ralph workflow
#
# Usage: ./ortus/idea.sh                           # Interactive menu
#        ./ortus/idea.sh "Your idea description"   # Create from idea
#        ./ortus/idea.sh --prd <path>              # Process existing PRD
#
# Creates a feature bead. After creating the idea:
#   ./ortus/interview.sh   # Interactive interview → PRD → task creation
#   ./ortus/ralph.sh       # Implements the tasks

set -euo pipefail

# Resolve ortus directory for prompt file access (must be done before cd)
ORTUS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
    prompt="${prompt_template//\$prd_path/$prd_path}"
    echo "$prompt" | claude --allowedTools "Read($prd_path),Bash(bd:*)" --dangerously-skip-permissions

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
    description=$(claude --print "$prompt_template

Idea: $idea")

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
        head -n 10 "$0" | tail -n +2 | sed 's/^# //' | sed 's/^#//'
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
