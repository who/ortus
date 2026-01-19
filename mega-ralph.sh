#!/bin/bash
# mega-ralph.sh - Continuous loop that keeps pulling work until queue is empty
#
# Usage: ./mega-ralph.sh [iterations_per_task] [idle_sleep]
#   iterations_per_task: Max iterations for ralph.sh per task (default: 10)
#   idle_sleep: Seconds to sleep when no work available (default: 60)
#
# Logs are written to logs/ralph-<timestamp>.log (one per task)
# Watch live with: tail -f logs/ralph-*.log
#
# Exit codes from ralph.sh:
#   0 - Task completed, check for more work
#   1 - Error, stop the loop
#   2 - No ready work, sleep and retry

ITERATIONS_PER_TASK=${1:-10}
IDLE_SLEEP=${2:-60}
TASK_DELAY=5  # Brief pause between tasks

echo "=== Mega Ralph Started ==="
echo "Iterations per task: $ITERATIONS_PER_TASK"
echo "Idle sleep: ${IDLE_SLEEP}s"
echo "Task delay: ${TASK_DELAY}s"
echo "Watch logs: tail -f logs/ralph-*.log"
echo ""

tasks_completed=0

while true; do
  echo ""
  echo "========================================"
  echo "$(date '+%Y-%m-%dT%H:%M:%S%z') - Checking for work..."
  echo "========================================"

  ./ralph.sh "$ITERATIONS_PER_TASK"
  exit_code=$?

  case $exit_code in
    0)
      tasks_completed=$((tasks_completed + 1))
      echo ""
      echo "Task #$tasks_completed completed. Checking for more work in ${TASK_DELAY}s..."
      sleep "$TASK_DELAY"
      ;;
    2)
      echo ""
      echo "No ready work. Tasks completed this session: $tasks_completed"
      echo "Sleeping ${IDLE_SLEEP}s before retry... (Ctrl+C to stop)"
      sleep "$IDLE_SLEEP"
      ;;
    *)
      echo ""
      echo "========================================"
      echo "Ralph exited with code $exit_code. Stopping."
      echo "Tasks completed this session: $tasks_completed"
      echo "========================================"
      exit $exit_code
      ;;
  esac
done
