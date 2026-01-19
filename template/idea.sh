#!/usr/bin/env bash
# idea.sh - Quick feature creation for Lisa workflow
#
# Usage: ./idea.sh "Your idea description"
#
# Creates a feature bead assigned to lisa. After creating the idea:
#   ./interview.sh   # Interactive feature interview
#   ./lisa.sh        # Generates PRD from interview answers

set -euo pipefail

if [[ -z "${1:-}" ]]; then
    echo "Usage: ./idea.sh \"Your idea description\""
    echo ""
    echo "Creates a feature assigned to lisa for the PRD workflow."
    exit 1
fi

bd create --title="$1" --type=feature --assignee=lisa
