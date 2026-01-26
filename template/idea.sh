#!/usr/bin/env bash
# idea.sh - Quick feature creation for Ralph workflow
#
# Usage: ./idea.sh "Your idea description"
#        ./idea.sh                           # Prompts for idea
#
# Creates a feature bead. After creating the idea:
#   ./interview.sh   # Interactive interview → PRD → task creation
#   ./ralph.sh       # Implements the tasks

set -euo pipefail

# Handle PRD intake flow
handle_prd() {
    echo "Paste your PRD (press Ctrl+D when done):"
    prd_content=$(cat)
    if [[ -z "$prd_content" ]]; then
        echo "No PRD provided. Exiting."
        exit 1
    fi

    echo "Processing your PRD..."
    # Extract title from PRD
    title=$(claude --print "Extract a concise feature title (5-8 words max) from this PRD. Output ONLY the title, nothing else.

PRD:
$prd_content")

    if [[ -z "$title" ]]; then
        title="Feature from PRD"
    fi

    bd create --title="$title" --type=feature --body="$prd_content"
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

    if [[ -z "$description" ]]; then
        bd create --title="$idea" --type=feature
    else
        bd create --title="$idea" --type=feature --body="$description"
    fi
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
