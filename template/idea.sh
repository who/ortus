#!/usr/bin/env bash
# idea.sh - Quick feature creation for Lisa workflow
#
# Usage: ./idea.sh "Your idea description"
#        ./idea.sh                           # Prompts for idea
#
# Creates a feature bead assigned to lisa. After creating the idea:
#   ./interview.sh   # Interactive feature interview
#   ./lisa.sh        # Generates PRD from interview answers

set -euo pipefail

idea="${1:-}"

if [[ -z "$idea" ]]; then
    # Prompt mode - ask user for their idea
    echo "What's your idea?"
    read -r -p "> " idea
    if [[ -z "$idea" ]]; then
        echo "No idea provided. Exiting."
        exit 1
    fi
fi

# Use Claude to up-sample the idea into a full description
echo "Expanding your idea..."
description=$(claude --print "You are helping a developer capture a feature idea. Up-sample this brief idea into a 2-3 sentence feature description. Be concise and specific about what the feature should do. Output ONLY the description text, nothing else.

Idea: $idea")

if [[ -z "$description" ]]; then
    # Fallback if Claude fails
    bd create --title="$idea" --type=feature --assignee=lisa
else
    bd create --title="$idea" --type=feature --assignee=lisa --body="$description"
fi
