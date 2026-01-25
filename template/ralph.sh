#!/bin/bash
# ralph.sh - Autonomous task execution loop
#
# Usage: ./ralph.sh [--idle-sleep N]
#
# Options:
#   --idle-sleep N   Seconds to sleep when no work available (default: 60)
#
# Runs until all ready work is complete. Each Claude invocation handles one task.
#
# Workflow:
#   1. ./idea.sh "my idea"   - Creates a feature
#   2. ./interview.sh        - Conducts interview, generates PRD, creates tasks
#   3. ./ralph.sh            - Implements tasks (can run in background)
#
# Logs are written to logs/ralph-<timestamp>.log
# Watch live with:
#   Human-readable: ./tail.sh                         (auto-follows all logs)
#   Raw output:     tail -f logs/ralph-<timestamp>.log
#
# Exit codes:
#   0 - All ready work completed successfully
#   1 - Error occurred

set -e

IDLE_SLEEP=60

while [[ $# -gt 0 ]]; do
  case $1 in
    --idle-sleep) IDLE_SLEEP="$2"; shift 2 ;;
    -h|--help) head -n 20 "$0" | tail -n +2 | sed 's/^# //' | sed 's/^#//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

mkdir -p logs
LOG_FILE="logs/ralph-$(date '+%Y%m%d-%H%M%S').log"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "=== Ralph Started ==="
log "Idle sleep: ${IDLE_SLEEP}s"
log "Log file: $LOG_FILE"
log "Watch live:"
log "  Human-readable: ./tail.sh              (auto-follows all logs)"
log "  Raw output:     tail -f $LOG_FILE"

tasks_completed=0

while true; do
  log ""
  log "--- Starting iteration ---"

  result=$(claude -p "$(cat prompt.md)" --output-format stream-json --verbose --dangerously-skip-permissions 2>&1 | tee -a "$LOG_FILE") || true

  if [[ "$result" == *"<promise>COMPLETE</promise>"* ]] || [[ "$result" == *"COMPLETE"* ]]; then
    tasks_completed=$((tasks_completed + 1))
    log "Task completed. Total: $tasks_completed"
  elif [[ "$result" == *"<promise>BLOCKED</promise>"* ]] || [[ "$result" == *"BLOCKED"* ]]; then
    log "Task blocked. Check beads comments for details."
  else
    # No signal = no work available or error
    if [ "$tasks_completed" -gt 0 ]; then
      log ""
      log "========================================"
      log "No more ready work. Tasks completed: $tasks_completed"
      log "========================================"
      exit 0
    else
      log "No work found. Sleeping ${IDLE_SLEEP}s... (Ctrl+C to stop)"
      sleep "$IDLE_SLEEP"
    fi
  fi
done
