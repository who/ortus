#!/bin/bash
# ralph.sh - Autonomous task execution loop
#
# Usage: ./ortus/ralph.sh [--fast] [--idle-sleep N] [--tasks N] [--iterations N] [--docker]
#
# Options:
#   --fast            Fast mode (2.5x faster output, premium pricing)
#   --idle-sleep N    Seconds to sleep when no work available (default: 60)
#   --tasks N         Stop after N tasks completed (default: unlimited)
#   --iterations N    Stop after N loop iterations (default: unlimited)
#   --docker          Tier 2 isolation: route claude through docker sandbox (parsed only; wired in T2.2)
#
# Runs until all ready work is complete. Logs to logs/ralph-<timestamp>.log
# Watch live: ./ortus/tail.sh or tail -f logs/ralph-*.log

set -e

IDLE_SLEEP=60
FAST_MODE=""
MAX_TASKS=0
MAX_ITERATIONS=0
USE_DOCKER=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --fast) FAST_MODE="--fast"; shift ;;
    --idle-sleep) IDLE_SLEEP="$2"; shift 2 ;;
    --tasks) MAX_TASKS="$2"; shift 2 ;;
    --iterations) MAX_ITERATIONS="$2"; shift 2 ;;
    --docker) USE_DOCKER=1; shift ;;
    -h|--help) sed -n '2,/^[^#]/{/^#/{s/^# \?//;p;}}' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# Single-instance guard. Concurrent ralph instances against the same repo
# race each other through bd's auto-start path and pile up orphan dolt
# sql-server processes — under sustained load this can cascade into
# dozens of zombies, exhausting the noms/LOCK and forcing manual recovery.
#
# We re-exec ourselves under flock(1) instead of `exec 9>file; flock -n 9`
# because the latter leaks the lock to children: dolt sql-server and
# `claude -p` inherit the lock FD via fork, and per flock(2) "the lock is
# released when all such [duplicate] descriptors have been closed" — so
# the lock outlives ralph.sh whenever children survive (e.g., after a
# SIGKILL on the wrapper that bypasses our EXIT trap). flock(1) opens
# the lock file in its own process, marks the FD close-on-exec, and
# exec's our script — children of us never see the FD, the lock stays
# scoped to flock(1), and when our script exits flock(1) releases cleanly.
mkdir -p .beads
if [ -z "${RALPH_LOCK_HELD:-}" ]; then
  # Pre-flight: if another ralph already holds the lock, give the user an
  # actionable diagnosis instead of silently exiting (flock -E 0 was kinder
  # to cron-like retries but baffling for interactive runs).
  if ! flock -n -x .beads/ralph.flock true 2>/dev/null; then
    echo "" >&2
    echo "ralph.sh: another instance is already running for this repo." >&2
    echo "  Lock file: .beads/ralph.flock (held)" >&2
    echo "" >&2
    holders="$(pgrep -af 'ortus/ralph' 2>/dev/null | grep -v "^$$ " || true)"
    if [ -n "$holders" ]; then
      echo "  Live ralph processes:" >&2
      printf '    %s\n' "$holders" >&2
      echo "" >&2
    fi
    latest_log="$(ls -1t logs/ralph-*.log 2>/dev/null | head -1 || true)"
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
    echo "    ./ortus/ralph.sh          # restart" >&2
    echo "" >&2
    exit 1
  fi
  export RALPH_LOCK_HELD=1
  # -n: non-blocking; -E 1: exit 1 on conflict (we shouldn't reach this
  # branch if the pre-flight check above passed, but if a TOCTOU race
  # loses to a concurrent ralph, exit 1 surfaces the failure rather than
  # masking it as success).
  exec flock -n -E 1 .beads/ralph.flock "$0" "$@"
  echo "ERROR: failed to re-exec under flock" >&2
  exit 1
fi

mkdir -p logs
LOG_FILE="logs/ralph-$(date '+%Y%m%d-%H%M%S').log"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "=== Ralph Started ==="
if [ -n "$FAST_MODE" ]; then
  log "Fast mode: enabled (2.5x faster output, premium pricing)"
fi
log "Idle sleep: ${IDLE_SLEEP}s"
log "Log file: $LOG_FILE"
log "Watch live:"
log "  Human-readable: ./ortus/tail.sh         (auto-follows all logs)"
log "  Raw output:     tail -f $LOG_FILE"

# bd in embedded mode (1.0.3 default) reads/writes the embedded DB directly
# — no sql-server, no port, no PID, nothing for ralph to orchestrate.
# Earlier versions of this script owned a shared dolt sql-server to work
# around bd's per-call auto-start piling up orphan dolts under sustained
# load; embedded mode eliminates that entire failure class at its source.

cleanup_children() {
  # On graceful EXIT/INT/TERM, kill any direct children that outlived us —
  # typically a forked `claude -p` from a partial iteration. SIGKILL on
  # ralph.sh itself bypasses this trap, so it's defense-in-depth only.
  pkill -KILL -P $$ 2>/dev/null || true
}
trap cleanup_children EXIT INT TERM

# Sandbox helpers (sandbox_smoke_test, docker_precondition_check) live in
# ortus/lib/sandbox.sh so canonical/template parity (FR-022) is structural
# rather than copy-paste. Source after log() is defined — the module's
# functions call log().
source "$(dirname "${BASH_SOURCE[0]}")/lib/sandbox.sh"

if [ -n "$USE_DOCKER" ]; then
  docker_precondition_check
else
  sandbox_smoke_test
fi

# Cache helpers (project-local .cache/ subdirs + XDG/per-tool cache env
# exports) live in ortus/lib/cache.sh so canonical/template parity (FR-022)
# is structural rather than copy-paste.
source "$(dirname "${BASH_SOURCE[0]}")/lib/cache.sh"

# Note: we deliberately DO NOT disable bd's per-call auto-start here.
# `bd config set dolt.auto-start false` (or BEADS_DOLT_AUTO_START=0) breaks
# bd usage outside ralph — any parallel terminal or separate Claude session
# in this repo can no longer file issues until ralph is running. The
# flock guard + ralph-owned dolt lifecycle above are sufficient to
# prevent the orphan-pile-up failure mode; auto-start disable was
# belt-and-suspenders that wasn't worth the parallel-use cost.

# Note: previous versions of this script wrapped `bd` calls in a flock
# helper that serialized concurrent dolt sql-server starts, and prepended
# the wrapper directory to PATH. Both were removed: under the OS sandbox
# the flock-wrapped bd would hang on a sandboxed loopback connection and
# hold the lock project-wide. bd 1.0.3's built-in dolt lifecycle handling
# supersedes the narrow concurrency benefit.

# Claude invocation routing — when --docker is set,
# route the inner claude session through `docker sandbox run claude --name
# ortus-ralph --` so it runs inside Docker's bundled-image sandbox. No
# Dockerfile; bind-mount defaults map host cwd → /workspace; logs
# remain tee'd to the host LOG_FILE so tail.sh works in both modes.
if [ -n "$USE_DOCKER" ]; then
  CLAUDE_CMD=(docker sandbox run claude --name ortus-ralph --)
else
  CLAUDE_CMD=(claude)
fi

tasks_completed=0
iteration=0

# Capture initial ready backlog so per-iteration progress lines can report
# "N of M (pct%)" against a stable denominator. Guarded against every failure
# shape: bd missing (pipe degenerates, jq sees empty stdin and silently exits 0),
# bd lock contention (stderr suppressed), jq missing (|| echo 0 fires). The
# regex re-check catches the bd-missing case the bare || echo 0 can't, so the
# variable is always a non-negative integer and the legacy "Total: N" branch
# stays reachable when bd is unqueryable.
INITIAL_READY=$(bd ready --json 2>/dev/null | jq 'length' 2>/dev/null || echo 0)
[[ "$INITIAL_READY" =~ ^[0-9]+$ ]] || INITIAL_READY=0
log "Initial ready backlog: ${INITIAL_READY} ready remaining"

while true; do
  iteration=$((iteration + 1))
  log ""
  log "--- Starting iteration $iteration ---"

  result=$("${CLAUDE_CMD[@]}" -p "$(cat "$(dirname "$0")/prompts/ralph-prompt.md")" --output-format stream-json --verbose --dangerously-skip-permissions $FAST_MODE 2>&1 | tee -a "$LOG_FILE") || true

  if [[ "$result" == *"<promise>COMPLETE</promise>"* ]]; then
    tasks_completed=$((tasks_completed + 1))
    # Denominator drift is intentional: follow-ups filed mid-run can push
    # READY_REMAINING above INITIAL_READY, and pct may temporarily exceed
    # 100% or decrease — that is accurate signal, not a bug. Do not clamp.
    # Regex re-check catches the bd-missing case (jq exits 0 on empty stdin,
    # so the bare || fallback doesn't fire) and pins READY_REMAINING to '?'.
    READY_REMAINING=$(bd ready --json 2>/dev/null | jq 'length' 2>/dev/null || echo '?')
    [[ "$READY_REMAINING" =~ ^[0-9]+$ ]] || READY_REMAINING='?'
    if [ "$INITIAL_READY" -gt 0 ]; then
      pct=$(( tasks_completed * 100 / INITIAL_READY ))
      log "Task completed. ${tasks_completed} of ${INITIAL_READY} (${pct}%) | ${READY_REMAINING} ready remaining"
    else
      log "Task completed. Total: $tasks_completed"
    fi
    if [ "$MAX_TASKS" -gt 0 ] && [ "$tasks_completed" -ge "$MAX_TASKS" ]; then
      log ""
      log "========================================"
      log "Reached --tasks limit ($MAX_TASKS). Tasks completed: $tasks_completed"
      log "========================================"
      exit 0
    fi
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

  if [ "$MAX_ITERATIONS" -gt 0 ] && [ "$iteration" -ge "$MAX_ITERATIONS" ]; then
    log ""
    log "========================================"
    log "Reached --iterations limit ($MAX_ITERATIONS). Tasks completed: $tasks_completed"
    log "========================================"
    exit 0
  fi
done
