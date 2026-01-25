#!/bin/bash
# ralph.sh - Task implementation loop for executing assigned work
#
# Usage: ./ralph.sh [--tasks N] [--iterations N] [--idle-sleep N]
#
# Options:
#   --tasks N            Max tasks to complete (default: unlimited, runs until queue empty)
#   --iterations N       Max iterations per task (default: 10)
#   --idle-sleep N       Seconds to sleep when no work available (default: 60)
#
# Examples:
#   ./ralph.sh                    # Run until all tasks complete
#   ./ralph.sh --tasks 1          # Complete exactly 1 task then exit
#   ./ralph.sh --tasks 5          # Complete up to 5 tasks then exit
#   ./ralph.sh --iterations 20    # Allow 20 iterations per task
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
#   0 - Task(s) completed successfully
#   1 - Error occurred
#   2 - No ready work available

set -e

# Defaults
MAX_TASKS=""  # Empty = unlimited (mega mode)
ITERATIONS_PER_TASK=10
IDLE_SLEEP=60
TASK_DELAY=5  # Brief pause between tasks

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --tasks)
      MAX_TASKS="$2"
      shift 2
      ;;
    --iterations)
      ITERATIONS_PER_TASK="$2"
      shift 2
      ;;
    --idle-sleep)
      IDLE_SLEEP="$2"
      shift 2
      ;;
    -h|--help)
      head -n 29 "$0" | tail -n +2 | sed 's/^# //' | sed 's/^#//'
      exit 0
      ;;
    *)
      # Legacy support: first positional arg is iterations
      if [[ "$1" =~ ^[0-9]+$ ]]; then
        ITERATIONS_PER_TASK="$1"
        shift
      else
        echo "Unknown option: $1" >&2
        exit 1
      fi
      ;;
  esac
done

# Setup logging
mkdir -p logs
LOG_FILE="logs/ralph-$(date '+%Y%m%d-%H%M%S').log"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Run a single task to completion
run_single_task() {
  local task_num=$1

  for ((i=1; i<=ITERATIONS_PER_TASK; i++)); do
    log ""
    log "=== Task #$task_num - Iteration $i/$ITERATIONS_PER_TASK ==="
    log "$(date '+%Y-%m-%dT%H:%M:%S%z')"
    log "--------------------------------"

    result=$(claude -p "$(cat prompt.md)" --output-format stream-json --verbose --dangerously-skip-permissions 2>&1 | tee -a "$LOG_FILE") || true

    if [[ "$result" == *"<promise>COMPLETE</promise>"* ]] || [[ "$result" == *"COMPLETE"* ]]; then
      log ""
      log "Task #$task_num completed after $i iteration(s)."
      return 0
    fi

    if [[ "$result" == *"<promise>BLOCKED</promise>"* ]] || [[ "$result" == *"BLOCKED"* ]]; then
      log ""
      log "Task blocked. Check activity.md for details."
      return 1
    fi

    log ""
    log "--- End of iteration $i ---"
  done

  log ""
  log "Reached max iterations ($ITERATIONS_PER_TASK) for task #$task_num without completion signal."
  return 1
}

# =============================================================================
# Main Execution
# =============================================================================

log "=== Ralph Started ==="
if [ -z "$MAX_TASKS" ]; then
  log "Task limit: Unlimited (run until queue empty)"
else
  log "Task limit: $MAX_TASKS task(s)"
fi
log "Iterations per task: $ITERATIONS_PER_TASK"
log "Idle sleep: ${IDLE_SLEEP}s"
log "Log file: $LOG_FILE"
log "Watch live:"
log "  Human-readable: ./tail.sh              (auto-follows all logs)"
log "  Raw output:     tail -f $LOG_FILE"
log ""

tasks_completed=0

while true; do
  # Check if we've hit our task limit
  if [ -n "$MAX_TASKS" ] && [ "$tasks_completed" -ge "$MAX_TASKS" ]; then
    log ""
    log "========================================"
    log "Completed $tasks_completed task(s). Limit reached."
    log "========================================"
    exit 0
  fi

  log ""
  log "--- Checking for ready tasks ---"

  # Check for ready tasks (type=task only)
  ready_count=$(bd ready --assignee ralph --json 2>/dev/null | jq -r '[.[] | select(.issue_type == "task")] | length' 2>/dev/null || echo "0")

  if [ "$ready_count" != "0" ]; then
    log "Found $ready_count ready task(s)"

    log ""
    log "========================================"
    log "Starting task #$((tasks_completed + 1))..."
    log "========================================"

    if run_single_task $((tasks_completed + 1)); then
      tasks_completed=$((tasks_completed + 1))
      log ""
      log "Pausing ${TASK_DELAY}s before checking for more work..."
      sleep "$TASK_DELAY"
    else
      # Task failed or blocked
      log ""
      log "========================================"
      log "Task failed. Tasks completed before failure: $tasks_completed"
      log "========================================"
      exit 1
    fi
  else
    log "No ready tasks found"

    if [ "$tasks_completed" -gt 0 ]; then
      # We completed some work, queue is now empty
      log ""
      log "========================================"
      log "No more ready work. Tasks completed: $tasks_completed"
      log "========================================"
      exit 0
    elif [ -z "$MAX_TASKS" ]; then
      # Continuous mode with no work - sleep and retry
      log ""
      log "No work found. Sleeping ${IDLE_SLEEP}s before retry... (Ctrl+C to stop)"
      sleep "$IDLE_SLEEP"
    else
      # Limited mode with no work - exit
      exit 2
    fi
  fi
done
