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

bd create --title="$idea" --type=feature --assignee=lisa
