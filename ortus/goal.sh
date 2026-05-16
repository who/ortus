#!/bin/bash
# goal.sh - Autonomous task execution via long-lived `claude -p "/goal CONDITION"` session
#
# Usage: ./ortus/goal.sh [--fast] [--idle-sleep N] [--tasks N] [--iterations N] [--docker] [-c|--condition STR] [--dry-run]
#
# Options:
#   --fast               Fast mode (2.5x faster output, premium pricing)
#   --idle-sleep N       Seconds to sleep when no work available (default: 60)
#   --tasks N            Stop after N tasks completed (default: unlimited)
#   --iterations N       Stop after N loop iterations (default: unlimited)
#   --docker             Tier 2 isolation: route claude through docker sandbox
#   -c, --condition STR  Custom completion condition (default: canonical from PRD Appendix A)
#   --dry-run            Print parsed flag state and exit 0 (for testing)
#   -h, --help           Show this help and exit
#
# yr7d.1 scope: flag parsing scaffold only. Subsequent E2 tasks fill in
# condition string (yr7d.2), flock guard (yr7d.3), sandbox/cache sourcing
# (yr7d.4), claude -p invocation (yr7d.5), and the template/ mirror (yr7d.6).

set -e

IDLE_SLEEP=60
FAST_MODE=""
MAX_TASKS=0
MAX_ITERS=0
USE_DOCKER=""
CONDITION=""
DRY_RUN=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --fast) FAST_MODE="--fast"; shift ;;
    --idle-sleep) IDLE_SLEEP="$2"; shift 2 ;;
    --tasks) MAX_TASKS="$2"; shift 2 ;;
    --iterations) MAX_ITERS="$2"; shift 2 ;;
    --docker) USE_DOCKER=1; shift ;;
    -c|--condition) CONDITION="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,/^[^#]/{/^#/{s/^# \?//;p;}}' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; echo "Run '$0 -h' for usage." >&2; exit 2 ;;
  esac
done

if [ -n "$DRY_RUN" ]; then
  echo "FAST_MODE=$FAST_MODE"
  echo "IDLE_SLEEP=$IDLE_SLEEP"
  echo "MAX_TASKS=$MAX_TASKS"
  echo "MAX_ITERS=$MAX_ITERS"
  echo "USE_DOCKER=$USE_DOCKER"
  echo "CONDITION=$CONDITION"
  exit 0
fi

echo "goal.sh: flag parsing scaffold only — orchestrator implementation pending (see ortus-yr7d epic)" >&2
exit 0
