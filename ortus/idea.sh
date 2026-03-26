#!/usr/bin/env bash
# idea.sh - Quick feature creation for Ralph workflow
#
# Usage: ./ortus/idea.sh                           # Interactive menu
#        ./ortus/idea.sh "Your idea description"   # Create from idea
#        ./ortus/idea.sh --script <path>           # Process existing video script
#
# Creates a feature bead. After creating the idea:
#   ./ortus/setup-video-beads.sh  # Set up video generation tasks from script
#   ./ortus/ralph.sh              # Implements the tasks

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Handle script intake flow
handle_script() {
    local script_path="${1:-}"

    # If no path provided as argument, prompt for it
    if [[ -z "$script_path" ]]; then
        echo "Sweet! What's the path to your video script?"
        read -r -p "> " script_path

        if [[ -z "$script_path" ]]; then
            echo "No path provided. Exiting."
            exit 1
        fi
    fi

    # Convert to absolute path for reliable access after cd
    script_path=$(realpath "$script_path" 2>/dev/null || echo "$script_path")

    if [[ ! -f "$script_path" ]]; then
        echo "Hmm, I can't find a file at '$script_path'. Double-check the path?"
        exit 1
    fi

    # Find the project directory containing the script
    script_dir=$(dirname "$script_path")
    project_dir=$(cd "$script_dir" && git rev-parse --show-toplevel 2>/dev/null || echo "$script_dir")

    echo "Processing your video script in $project_dir..."

    # Save current directory and switch to project
    original_dir=$(pwd)
    cd "$project_dir"

    # Copy script to SCRIPT.md if it isn't already there
    if [[ "$(realpath "$script_path")" != "$(realpath "$project_dir/SCRIPT.md" 2>/dev/null || echo "")" ]]; then
        cp "$script_path" "$project_dir/SCRIPT.md"
    fi

    # Run setup-video-beads.sh to create the bead hierarchy
    "$SCRIPT_DIR/setup-video-beads.sh"

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
    echo "Describe your video concept and we'll set up the generation tasks."
    echo ""
}

# Main flow

# Check for --script flag
if [[ "${1:-}" == "--script" ]]; then
    if [[ -z "${2:-}" ]]; then
        echo "Error: --script requires a path argument"
        echo "Usage: ./ortus/idea.sh --script <path>"
        exit 1
    fi
    handle_script "$2"
    exit 0
fi

idea="${1:-}"

# If idea provided as argument, skip menu and go straight to idea flow
if [[ -n "$idea" ]]; then
    handle_idea "$idea"
    exit 0
fi

# Interactive menu
echo "Got a video script already? Don't sweat it if not—we'll help you get started."
echo ""
echo "  [1] Yes, I have a script"
echo "  [2] Nope, just an idea"
echo ""
read -r -p "Your choice: " choice

case "$choice" in
    1)
        handle_script
        ;;
    2|"")
        handle_idea
        ;;
    *)
        echo "Invalid choice. Please enter 1 or 2."
        exit 1
        ;;
esac
