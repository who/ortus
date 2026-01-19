#!/bin/bash
# lisa.sh - PRD generation and idea-to-implementation pipeline
#
# Usage: ./lisa.sh [--poll-interval N] [--idle-sleep N]
#
# Options:
#   --poll-interval N  Seconds between state checks (default: 30)
#   --idle-sleep N     Seconds to sleep when no work (default: 60)
#
# The pipeline uses labels to track state:
#   (none)           - New idea, needs interview questions generated
#   prd:interviewing - Interview questions created, waiting for human answers
#   prd:ready        - Interview complete, PRD generated, awaiting approval
#   prd:approved     - Human approved, tasks being created for ralph
#
# Workflow:
#   1. User: bd create --title="My idea" --type=idea --assignee=lisa
#   2. Lisa: Generates interview questions as child beads (blocking)
#   3. Human: Answers questions via comments, closes question beads
#   4. Lisa: When all questions closed, generates PRD (prd/PRD-<name>.md)
#   5. Human: Reviews PRD, adds 'approved' label when ready
#   6. Lisa: Creates implementation tasks with --assignee=ralph
#   7. Lisa: Closes the idea
#
# Logs are written to logs/lisa-<timestamp>.log
# Watch live with:
#   Human-readable: ./tail.sh                         (auto-follows all logs)
#   Raw output:     tail -f logs/lisa-<timestamp>.log
#
# Exit codes:
#   0 - Normal exit (Ctrl+C)
#   1 - Error occurred

set -e

# Defaults
POLL_INTERVAL=30
IDLE_SLEEP=60

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --poll-interval)
      POLL_INTERVAL="$2"
      shift 2
      ;;
    --idle-sleep)
      IDLE_SLEEP="$2"
      shift 2
      ;;
    -h|--help)
      head -n 32 "$0" | tail -n +2 | sed 's/^# //' | sed 's/^#//'
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

# Setup logging
mkdir -p logs
LOG_FILE="logs/lisa-$(date '+%Y%m%d-%H%M%S').log"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# State handlers - to be implemented by subsequent tasks
handle_new_idea() {
  local idea_id="$1"
  local idea_title="$2"
  log "  TODO: Generate interview questions for new idea"
  # Will call generate_interview() and add prd:interviewing label
  return 1  # Not implemented yet
}

handle_interviewing() {
  local idea_id="$1"
  local idea_title="$2"
  log "  TODO: Check if interview is complete"
  # Will check if all question beads are closed
  # If complete: generate PRD and add prd:ready label
  return 1  # Not implemented yet
}

handle_ready() {
  local idea_id="$1"
  local idea_title="$2"
  log "  Waiting for human to add 'approved' label"
  # PRD exists, waiting for human review
  return 0  # Nothing to do, human action required
}

handle_approved() {
  local idea_id="$1"
  local idea_title="$2"
  log "  TODO: Create implementation tasks for ralph"
  # Will create tasks from PRD and close the idea
  return 1  # Not implemented yet
}

# Process a single idea based on its state (labels)
process_idea() {
  local idea_id="$1"
  local idea_title="$2"
  local labels="$3"

  log "Processing: $idea_id - $idea_title"
  log "  Labels: ${labels:-none}"

  # Route based on state labels
  if [[ "$labels" == *"prd:approved"* ]]; then
    handle_approved "$idea_id" "$idea_title"
  elif [[ "$labels" == *"prd:ready"* ]]; then
    handle_ready "$idea_id" "$idea_title"
  elif [[ "$labels" == *"prd:interviewing"* ]]; then
    handle_interviewing "$idea_id" "$idea_title"
  else
    # New idea - no prd: label yet
    handle_new_idea "$idea_id" "$idea_title"
  fi
}

# Main execution
log "=== Lisa Started ==="
log "Poll interval: ${POLL_INTERVAL}s"
log "Idle sleep: ${IDLE_SLEEP}s"
log "Log file: $LOG_FILE"
log "Watch live:"
log "  Human-readable: ./tail.sh              (auto-follows all logs)"
log "  Raw output:     tail -f $LOG_FILE"
log ""

# Main loop
while true; do
  log ""
  log "--- Checking for ideas ---"

  # Get ideas assigned to lisa
  ideas_json=$(bd ready --assignee lisa --json 2>/dev/null || echo "[]")
  idea_count=$(echo "$ideas_json" | jq -r 'length' 2>/dev/null || echo "0")

  if [ "$idea_count" = "0" ] || [ -z "$idea_count" ]; then
    log "No ideas ready. Sleeping ${IDLE_SLEEP}s... (Ctrl+C to stop)"
    sleep "$IDLE_SLEEP"
    continue
  fi

  log "Found $idea_count idea(s) to process"

  # Process each idea
  for i in $(seq 0 $((idea_count - 1))); do
    idea_id=$(echo "$ideas_json" | jq -r ".[$i].id")
    idea_title=$(echo "$ideas_json" | jq -r ".[$i].title")
    idea_labels=$(echo "$ideas_json" | jq -r ".[$i].labels // [] | join(\",\")")

    process_idea "$idea_id" "$idea_title" "$idea_labels" || {
      log "  Handler returned non-zero (feature not implemented or error)"
    }
  done

  log "Sleeping ${POLL_INTERVAL}s before next check..."
  sleep "$POLL_INTERVAL"
done
