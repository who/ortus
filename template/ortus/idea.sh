#!/usr/bin/env bash
# idea.sh - Quick feature creation for Ralph workflow
#
# Usage: ./ortus/idea.sh "Your idea description"
#        ./ortus/idea.sh                           # Prompts for idea
#
# Creates a feature bead. After creating the idea:
#   ./ortus/interview.sh   # Interactive interview → PRD → task creation
#   ./ortus/ralph.sh       # Implements the tasks

set -euo pipefail

# Handle PRD intake flow
handle_prd() {
    echo "Sweet! What's the path to your PRD?"
    read -r -p "> " prd_path

    if [[ -z "$prd_path" ]]; then
        echo "No path provided. Exiting."
        exit 1
    fi

    if [[ ! -f "$prd_path" ]]; then
        echo "Hmm, I can't find a file at '$prd_path'. Double-check the path?"
        exit 1
    fi

    echo "Processing your PRD..."
    echo "Read $prd_path. Use bd to create an epic and decompose into tasks with dependencies. Each task must have acceptance criteria." | claude --allowedTools "Bash(bd *)"

    echo ""
    echo "Done! Your PRD has been decomposed into an epic with tasks."
    echo "Next steps:"
    echo "  bd ready       # See what's ready to work on"
    echo "  ./ortus/ralph.sh     # Start implementing tasks"
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
