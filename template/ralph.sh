#!/bin/bash
# ralph.sh - Run Claude agent loop for beads tasks
#
# Exit codes:
#   0 - Task completed successfully
#   1 - Error occurred
#   2 - No ready work available

set -e

ITERATIONS=${1:-10}

# Check if there's any ready work before starting
ready_count=$(bd ready --json 2>/dev/null | jq -r 'length' 2>/dev/null || echo "0")
if [ "$ready_count" = "0" ]; then
  echo "No ready work found (bd ready returned 0 items)"
  exit 2
fi

echo "Found $ready_count ready task(s). Starting loop..."

for ((i=1; i<=$ITERATIONS; i++)); do
  echo ""
  echo "=== Iteration $i/$ITERATIONS ==="
  echo "$(date '+%Y-%m-%d %H:%M:%S')"
  echo "--------------------------------"

  result=$(claude -p "$(cat PROMPT.md)" --output-format text 2>&1) || true

  echo "$result"

  if [[ "$result" == *"<promise>COMPLETE</promise>"* ]]; then
    echo ""
    echo "Task completed after $i iteration(s)."
    exit 0
  fi

  if [[ "$result" == *"<promise>BLOCKED</promise>"* ]]; then
    echo ""
    echo "Task blocked. Check activity.md for details."
    exit 1
  fi

  echo ""
  echo "--- End of iteration $i ---"
done

echo ""
echo "Reached max iterations ($ITERATIONS) without completion signal."
exit 1
