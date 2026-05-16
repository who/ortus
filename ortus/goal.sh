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
# yr7d.1 scope: flag parsing scaffold. yr7d.4 wires sandbox/cache sourcing
# + smoke test + docker precondition check. Subsequent E2 tasks fill in
# condition string (yr7d.2), flock guard (yr7d.3), claude -p invocation
# (yr7d.5), and the template/ mirror (yr7d.6).

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

# Minimal log() until yr7d.5 lands LOG_FILE + tee'd variant. Defined ahead of
# the sandbox.sh source because that module's functions call log().
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2; }

# Sandbox helpers (sandbox_smoke_test, docker_precondition_check) live in
# ortus/lib/sandbox.sh so canonical/template parity (FR-022) is structural
# rather than copy-paste. Source after log() is defined.
source "$(dirname "${BASH_SOURCE[0]}")/lib/sandbox.sh"

# Tier 1 (native sandbox) vs Tier 2 (--docker): mirror ralph.sh's
# mutually-exclusive dispatch. NFR-001 forbids skip env vars; both branches
# fail fast with friendly install hints when prerequisites are missing.
if [ -n "$USE_DOCKER" ]; then
  docker_precondition_check
else
  sandbox_smoke_test
fi

# Cache helpers (project-local .cache/ subdirs + XDG/per-tool cache env
# exports) live in ortus/lib/cache.sh so canonical/template parity (FR-022)
# is structural rather than copy-paste. No log() dependency.
source "$(dirname "${BASH_SOURCE[0]}")/lib/cache.sh"

echo "goal.sh: orchestrator pending — yr7d.3 (flock guard) and yr7d.5 (claude -p loop) wire the remainder" >&2
exit 0
