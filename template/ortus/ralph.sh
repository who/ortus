#!/bin/bash
# ralph.sh - Autonomous task execution loop
#
# Usage: ./ortus/ralph.sh [--idle-sleep N]
#
# Runs until all ready work is complete. Logs to logs/ralph-<timestamp>.log
# Watch live: ./ortus/tail.sh or tail -f logs/ralph-*.log

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
log "  Human-readable: ./ortus/tail.sh         (auto-follows all logs)"
log "  Raw output:     tail -f $LOG_FILE"

tasks_completed=0

while true; do
  log ""
  log "--- Starting iteration ---"

  result=$(claude -p "$(cat "$(dirname "$0")/prompts/ralph-prompt.md")" --output-format stream-json --verbose --dangerously-skip-permissions 2>&1 | tee -a "$LOG_FILE") || true

  if [[ "$result" == *"<promise>COMPLETE</promise>"* ]]; then
    tasks_completed=$((tasks_completed + 1))
    log "Task completed. Total: $tasks_completed"
  elif [[ "$result" == *"<promise>EMPTY</promise>"* ]]; then
    # Explicit empty queue signal - stop gracefully
    log ""
    log "========================================"
    log "Queue empty. Tasks completed: $tasks_completed"
    log "========================================"
    exit 0
  elif [[ "$result" == *"<promise>BLOCKED</promise>"* ]]; then
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
