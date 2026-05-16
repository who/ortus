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
# yr7d.1 scope: flag parsing scaffold. yr7d.3 wires the flock guard and
# cleanup_children trap. yr7d.4 wires sandbox/cache sourcing + smoke test
# + docker precondition check. Subsequent E2 tasks fill in condition string
# (yr7d.2), claude -p invocation (yr7d.5), and the template/ mirror (yr7d.6).

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

# Single-instance guard (FR-005). Concurrent orchestrator instances (ralph.sh
# OR goal.sh) against the same repo race each other through bd's auto-start
# path and pile up orphan dolt sql-server processes — under sustained load
# this can cascade into dozens of zombies, exhausting the noms/LOCK and
# forcing manual recovery. goal.sh shares ralph.sh's lock file so that the
# two orchestrators mutually exclude each other during the migration window.
#
# We re-exec ourselves under flock(1) instead of `exec 9>file; flock -n 9`
# because the latter leaks the lock to children: dolt sql-server and
# `claude -p` inherit the lock FD via fork, and per flock(2) "the lock is
# released when all such [duplicate] descriptors have been closed" — so
# the lock outlives goal.sh whenever children survive (e.g., after a
# SIGKILL on the wrapper that bypasses our EXIT trap). flock(1) opens
# the lock file in its own process, marks the FD close-on-exec, and
# exec's our script — children of us never see the FD, the lock stays
# scoped to flock(1), and when our script exits flock(1) releases cleanly.
mkdir -p .beads
if [ -z "${GOAL_LOCK_HELD:-}" ]; then
  # Pre-flight: if another orchestrator (ralph.sh or goal.sh) already holds
  # the lock, give the user an actionable diagnosis instead of silently
  # exiting (flock -E 0 was kinder to cron-like retries but baffling for
  # interactive runs).
  if ! flock -n -x .beads/ralph.flock true 2>/dev/null; then
    echo "" >&2
    echo "goal.sh: another orchestrator instance is already running for this repo." >&2
    echo "  Lock file: .beads/ralph.flock (held)" >&2
    echo "  Note: goal.sh and ralph.sh share this lock — only one orchestrator can run at a time." >&2
    echo "" >&2
    holders="$(pgrep -af 'ortus/(ralph|goal)' 2>/dev/null | grep -v "^$$ " || true)"
    if [ -n "$holders" ]; then
      echo "  Live ralph/goal processes:" >&2
      printf '    %s\n' "$holders" >&2
      echo "" >&2
    fi
    latest_log="$(ls -1t logs/ralph-*.log logs/goal-*.log 2>/dev/null | head -1 || true)"
    echo "  To watch the running session:" >&2
    if [ -n "$latest_log" ]; then
      echo "    tail -f $latest_log" >&2
    fi
    echo "    ./ortus/tail.sh" >&2
    echo "" >&2
    echo "  To stop the running session and start fresh:" >&2
    if [ -n "$holders" ]; then
      head_pid="$(echo "$holders" | awk 'NR==1{print $1}')"
      echo "    kill -KILL -$head_pid    # negative PID = whole process group" >&2
    else
      echo "    kill -KILL -<wrapper-pid>    # see lsof .beads/ralph.flock" >&2
    fi
    echo "    ./ortus/goal.sh          # restart" >&2
    echo "" >&2
    exit 1
  fi
  export GOAL_LOCK_HELD=1
  # -n: non-blocking; -E 1: exit 1 on conflict (we shouldn't reach this
  # branch if the pre-flight check above passed, but if a TOCTOU race
  # loses to a concurrent orchestrator, exit 1 surfaces the failure rather
  # than masking it as success).
  exec flock -n -E 1 .beads/ralph.flock "$0" "$@"
  echo "ERROR: failed to re-exec under flock" >&2
  exit 1
fi

# Minimal log() until yr7d.5 lands LOG_FILE + tee'd variant. Defined ahead of
# the sandbox.sh source because that module's functions call log().
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2; }

cleanup_children() {
  # On graceful EXIT/INT/TERM, kill any direct children that outlived us —
  # typically a forked `claude -p` from a partial iteration. SIGKILL on
  # goal.sh itself bypasses this trap, so it's defense-in-depth only.
  pkill -KILL -P $$ 2>/dev/null || true
}
trap cleanup_children EXIT INT TERM

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

echo "goal.sh: orchestrator pending — yr7d.5 (claude -p loop) wires the remainder" >&2
exit 0
