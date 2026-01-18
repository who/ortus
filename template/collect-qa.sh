#!/bin/bash
# collect-qa.sh - Check if Q&A subtasks are complete and collect answers
#
# Usage: ./collect-qa.sh <idea-id>
#
# This script checks if all Q&A subtasks blocking an idea are closed,
# and if so, collects the answers into a structured format for PRD generation.
#
# Exit codes:
#   0 - Success, all Q&A complete, answers collected
#   1 - Error occurred
#   2 - Q&A incomplete (some subtasks still open)

set -e

IDEA_ID="$1"

if [ -z "$IDEA_ID" ]; then
    echo "Usage: ./collect-qa.sh <idea-id>"
    echo ""
    echo "Example:"
    echo "  ./collect-qa.sh myproject-abc123"
    exit 1
fi

# Get idea details with dependencies
idea_json=$(bd show "$IDEA_ID" --json 2>/dev/null) || {
    echo "Error: Could not read idea '$IDEA_ID'. Does it exist?"
    exit 1
}

# Extract basic info
idea_title=$(echo "$idea_json" | jq -r '.[0].title // "Untitled"')
idea_description=$(echo "$idea_json" | jq -r '.[0].description // "No description"')

echo "Checking Q&A status for: $idea_title"
echo ""

# Get dependencies (issues that block this idea)
# These are the Q&A subtasks
deps=$(echo "$idea_json" | jq -r '.[0].dependencies // []')
dep_count=$(echo "$deps" | jq -r 'length')

if [ "$dep_count" = "0" ]; then
    echo "No Q&A subtasks found for this idea."
    echo "Run ./generate-qa.sh $IDEA_ID first to generate questions."
    exit 2
fi

echo "Found $dep_count Q&A subtask(s)"
echo ""

# Check status of each dependency
open_count=0
closed_count=0
qa_pairs=""

for i in $(seq 0 $((dep_count - 1))); do
    dep_id=$(echo "$deps" | jq -r ".[$i].id")
    dep_title=$(echo "$deps" | jq -r ".[$i].title")
    dep_status=$(echo "$deps" | jq -r ".[$i].status")
    dep_description=$(echo "$deps" | jq -r ".[$i].description // \"\"")

    # Check if this looks like a Q&A subtask (title starts with "Q:")
    if [[ "$dep_title" != Q:* ]]; then
        continue
    fi

    echo "  [$dep_status] $dep_title"

    if [ "$dep_status" = "closed" ]; then
        closed_count=$((closed_count + 1))

        # Extract the question (from title, remove "Q: " prefix)
        question="${dep_title#Q: }"

        # Try to extract answer from description
        # Look for "## Your Answer" section
        answer=$(echo "$dep_description" | awk '/^## Your Answer/,0' | tail -n +2 | sed '/^$/d' | head -20)

        # If no answer in description, try to get from comments
        if [ -z "$answer" ] || [ "$answer" = "[Write your answer here]" ]; then
            # Get comments for this subtask
            comments_json=$(bd comments "$dep_id" --json 2>/dev/null || echo "[]")
            answer=$(echo "$comments_json" | jq -r '.[].content // empty' | tail -1)
        fi

        # If still no answer, note it
        if [ -z "$answer" ] || [ "$answer" = "[Write your answer here]" ]; then
            answer="[No answer provided]"
        fi

        # Append to Q&A pairs
        qa_pairs="${qa_pairs}
Q: $question
A: $answer
"
    else
        open_count=$((open_count + 1))
    fi
done

echo ""

# Check if all Q&A is complete
if [ "$open_count" -gt 0 ]; then
    echo "Q&A incomplete: $open_count question(s) still open, $closed_count closed"
    echo "Waiting for human to answer remaining questions and close subtasks."
    exit 2
fi

echo "All Q&A complete! ($closed_count questions answered)"
echo ""

# Output the collected Q&A
output_file="prd/.qa-answers-$(echo "$IDEA_ID" | tr '[:upper:]' '[:lower:]').md"

cat > "$output_file" << EOF
# Q&A Answers for: $idea_title

**Idea ID**: $IDEA_ID
**Collected**: $(date '+%Y-%m-%d %H:%M:%S')

## Original Idea Description

$idea_description

## Discovery Q&A
$qa_pairs
EOF

echo "Answers collected to: $output_file"
echo ""
echo "Ready for PRD generation. Run:"
echo "  ./generate-prd-from-qa.sh $IDEA_ID"
