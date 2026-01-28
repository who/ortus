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

    echo "Read $prd_path . Decompose the provided PRD Markdown into a Beads issue graph using bd. For each work item, create an issue with: title, description (scope/context), acceptance_criteria (REQUIRED: must include testable conditions that define 'done' AND specific testing instructions for verification), design notes (technical approach), priority (0-4, 0=critical), type (epic/feature/task/bug/chore), labels, and estimated_minutes. CRITICAL: Every task MUST have acceptance_criteria with: (1) testable conditions - what must be true when done, (2) testing instructions - how to verify each condition. Structure hierarchically: epics for major features, decomposed into tasks via parent-child dependencies; use blocks for execution order constraints and related for shared context. Output all bd create and bd dep add commands to construct the complete graph with proper dependencies reflecting the PRD's requirements and sequence. Where atomically possible, run the bd tasks in parallel with 10 sub-agents.  When done, tell the user to type /exit to continue." | claude --allowedTools "Read($prd_path),Bash(bd:*)" --dangerously-skip-permissions 

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
    description=$(claude --print "You are helping a developer capture a feature idea. Up-sample this brief idea into a 2-3 sentence feature description. Be concise and specific about what the feature should do. Output ONLY the description text, nothing else.

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
