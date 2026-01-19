#!/bin/bash
# ralph.sh - Run Claude agent loop for beads tasks
#
# Usage: ./ralph.sh [iterations]
#
# Options:
#   iterations  Max iterations before giving up (default: 10)
#
# Logs are written to logs/ralph-<timestamp>.log
# Watch live with:
#   Human-readable: ./tail.sh                         (auto-follows all logs)
#   Raw output:     tail -f logs/ralph-<timestamp>.log
#
# Exit codes:
#   0 - Task completed successfully
#   1 - Error occurred
#   2 - No ready work available

set -e

ITERATIONS=${1:-10}

# Setup logging
mkdir -p logs
LOG_FILE="logs/ralph-$(date '+%Y%m%d-%H%M%S').log"

log() {
  echo "$@" | tee -a "$LOG_FILE"
}

# Check if there's any ready work before starting
ready_count=$(bd ready --assignee ralph --json 2>/dev/null | jq -r 'length' 2>/dev/null || echo "0")
if [ "$ready_count" = "0" ]; then
  log "No ready work found (bd ready returned 0 items)"
  exit 2
fi

log "Found $ready_count ready task(s). Starting loop..."
log "Log file: $LOG_FILE"
log "Watch live:"
log "  Human-readable: ./tail.sh              (auto-follows all logs)"
log "  Raw output:     tail -f $LOG_FILE"

for ((i=1; i<=$ITERATIONS; i++)); do
  log ""
  log "=== Iteration $i/$ITERATIONS ==="
  log "$(date '+%Y-%m-%dT%H:%M:%S%z')"
  log "--------------------------------"

  result=$(claude -p "$(cat PROMPT.md)" --output-format stream-json --verbose --dangerously-skip-permissions 2>&1 | tee -a "$LOG_FILE") || true

  if [[ "$result" == *"<promise>COMPLETE</promise>"* ]] || [[ "$result" == *"COMPLETE"* ]]; then
    log ""
    log "Task completed after $i iteration(s)."
    exit 0
  fi

  if [[ "$result" == *"<promise>BLOCKED</promise>"* ]] || [[ "$result" == *"BLOCKED"* ]]; then
    log ""
    log "Task blocked. Check activity.md for details."
    exit 1
  fi

  log ""
  log "--- End of iteration $i ---"
done

log ""
log "Reached max iterations ($ITERATIONS) without completion signal."
exit 1
