#!/bin/bash
# goal.sh - Autonomous task execution via long-lived `claude -p "/goal CONDITION"` session
#
# Usage: ./ortus/goal.sh [--fast] [--idle-sleep N] [--tasks N] [--iterations N] [--docker] [-c|--condition STR] [--dry-run|--dry-run-condition]
#
# Options:
#   --fast                Fast mode (2.5x faster output, premium pricing)
#   --idle-sleep N        Seconds to sleep when no work available (default: 60)
#   --tasks N             Stop after N tasks completed (default: unlimited)
#   --iterations N        Stop after N loop iterations (default: unlimited)
#   --docker              Tier 2 isolation: route claude through docker sandbox
#   --backend NAME        Agent backend: claude|codex. Overrides $ORTUS_BACKEND,
#                         which in turn overrides the generated default.
#   -c, --condition STR   Custom completion condition (default: canonical from PRD Appendix A)
#                         Scoped-run examples (drive the queue until a specific milestone):
#                           -c 'all children of bd-auth-epic are closed'
#                           -c 'reports/goal-vs-ralph-2026-05-16.md exists and contains M1 PASS and M3 PASS'
#   --dry-run             Print parsed flag state and exit 0 (for testing)
#   --dry-run-condition   Print built /goal condition and exit 0 (for testing)
#   -h, --help            Show this help and exit
#
# yr7d.1 scope: flag parsing scaffold. yr7d.3 wires the flock guard and
# cleanup_children trap. yr7d.4 wires sandbox/cache sourcing + smoke test
# + docker precondition check. yr7d.2 wires the canonical-condition builder
# (loads prompts/conditions/queue-zero.txt; substitutes <NTASKS>/<NITERS>;
# drops the early-stop clauses for any flag not set; FR-004 4000-char cap).
# yr7d.5 wires the long-lived `claude -p "/goal CONDITION"` invocation with
# stream-json output teed to logs/goal-<timestamp>.log, --fast pass-through,
# and --docker routing via `docker sandbox run claude --name ortus-goal --`.
# yr7d.6 mirrors canonical -> template/.

set -e

IDLE_SLEEP=60
FAST_MODE=""
MAX_TASKS=0
MAX_ITERS=0
USE_DOCKER=""
BACKEND_FLAG=""
CONDITION=""
DRY_RUN=""
DRY_RUN_CONDITION=""
PRINT_CMD=""

while [[ $# -gt 0 ]]; do
  case $1 in
    --fast) FAST_MODE="--fast"; shift ;;
    --idle-sleep) IDLE_SLEEP="$2"; shift 2 ;;
    --tasks) MAX_TASKS="$2"; shift 2 ;;
    --iterations) MAX_ITERS="$2"; shift 2 ;;
    --docker) USE_DOCKER=1; shift ;;
    --backend) BACKEND_FLAG="$2"; shift 2 ;;
    -c|--condition) CONDITION="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --dry-run-condition) DRY_RUN_CONDITION=1; shift ;;
    --print-cmd) PRINT_CMD=1; shift ;;
    -h|--help) sed -n '2,/^[^#]/{/^#/{s/^# \?//;p;}}' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; echo "Run '$0 -h' for usage." >&2; exit 2 ;;
  esac
done

# Resolve the backend once, here, and export it so every later consumer
# (backend.sh's own functions, the sourced libs, any child process) reads one
# already-decided value instead of re-running the precedence rules (NFR-002).
# Sourced before the dry-run exits so --dry-run reports the resolved backend.
source "$(dirname "${BASH_SOURCE[0]}")/lib/backend.sh"
ORTUS_BACKEND="$(resolve_backend "$BACKEND_FLAG")" || exit 1
export ORTUS_BACKEND

build_condition() {
  # Emit the /goal condition on stdout.
  # If -c CONDITION was supplied, echo it verbatim. Otherwise load the
  # canonical Appendix A text from prompts/conditions/queue-zero.txt, drop
  # the early-stop clauses for any flag not set, substitute <NTASKS> /
  # <NITERS>, and enforce the FR-004 4000-char ceiling.
  local condition_file body has_tasks has_iters len

  if [ -n "$CONDITION" ]; then
    printf '%s\n' "$CONDITION"
    return 0
  fi

  condition_file="$(dirname "${BASH_SOURCE[0]}")/prompts/conditions/queue-zero.txt"
  if [ ! -f "$condition_file" ]; then
    echo "goal.sh: canonical condition file missing: $condition_file" >&2
    exit 1
  fi

  body="$(cat "$condition_file")"
  # A half-mirrored project (template populated before yr7d.X landed the
  # canonical text) must fail loudly rather than launch a no-op evaluator.
  if [[ "$body" == "TODO PLACEHOLDER"* ]]; then
    echo "goal.sh: canonical condition is still a TODO placeholder: $condition_file" >&2
    exit 1
  fi

  has_tasks=""
  has_iters=""
  [ "${MAX_TASKS:-0}" -gt 0 ] && has_tasks=1
  [ "${MAX_ITERS:-0}" -gt 0 ] && has_iters=1

  if [ -z "$has_tasks" ] && [ -z "$has_iters" ]; then
    # Drop the whole early-stop block and collapse the now-adjacent blank lines.
    body="$(printf '%s\n' "$body" \
      | sed '/^You may stop early if EITHER:$/,/^(b) you have used .* turns since this goal was set\.$/d' \
      | awk 'BEGIN{prev=0} /^$/{if(prev)next; prev=1; print; next} {prev=0; print}')"
  elif [ -n "$has_tasks" ] && [ -z "$has_iters" ]; then
    # Drop (b); flip (a)'s trailing ", OR" → "." since it no longer chains.
    body="$(printf '%s\n' "$body" \
      | sed '/^(b) you have used .* turns since this goal was set\.$/d' \
      | sed 's/issues in this session (count only `bd close` calls that returned success), OR$/issues in this session (count only `bd close` calls that returned success)./')"
  elif [ -z "$has_tasks" ] && [ -n "$has_iters" ]; then
    # Drop (a); (b) is already terminated with "." so no rewrite needed.
    body="$(printf '%s\n' "$body" | sed '/^(a) you have closed .* issues in this session .*OR$/d')"
  fi
  # Both flags set: leave the block intact for verbatim Appendix A output.

  body="${body//<NTASKS>/$MAX_TASKS}"
  body="${body//<NITERS>/$MAX_ITERS}"

  len=${#body}
  if [ "$len" -gt 4000 ]; then
    echo "goal.sh: built condition is $len chars; FR-004 ceiling is 4000." >&2
    exit 1
  fi

  printf '%s\n' "$body"
}

check_hooks_enabled() {
  # Fail-fast precheck: /goal is implemented as a managed Stop hook. If any
  # Claude Code settings layer sets disableAllHooks=true, the /goal directive
  # silently degrades into a hookless `claude -p` run with no termination
  # contract — the worst UX outcome. Detect and refuse to launch.
  #
  # Scope: only checks the standard settings.json layers (user, project,
  # platform-managed) and only the disableAllHooks key. allowManagedHooksOnly
  # is a separate ambiguous concern (PRD); not checked here.
  local layers=(
    "$HOME/.claude/settings.json"
    ".claude/settings.json"
  )
  case "$(uname -s)" in
    Linux)  layers+=("/etc/claude/managed-settings.json") ;;
    Darwin) layers+=("/Library/Application Support/ClaudeCode/managed-settings.json") ;;
  esac

  if ! command -v jq >/dev/null 2>&1; then
    log "WARN: jq not on PATH; skipping hook-disabled precheck. /goal may silently degrade if hooks are disabled."
    return 0
  fi

  local f disabled
  for f in "${layers[@]}"; do
    [ -f "$f" ] || continue
    disabled=$(jq -r '.disableAllHooks // false' "$f" 2>/dev/null || echo false)
    if [ "$disabled" = "true" ]; then
      log "ERROR: disableAllHooks=true in $f"
      log "  /goal is implemented as a managed Stop hook and requires hooks to be enabled."
      log "  With hooks disabled, /goal silently does nothing — the session would run as a"
      log "  normal claude -p invocation with no termination contract."
      log ""
      log "  To fix: remove or set disableAllHooks=false in $f, OR run the legacy ralph.sh"
      log "  shim if available (./ortus/ralph.sh — see Q4 / dcr4.4 for status)."
      log ""
      log "  Docs: https://code.claude.com/docs/en/goal"
      exit 1
    fi
  done
  log "Hook precheck: enabled (no disableAllHooks=true found in any settings layer)"
}

if [ -n "$DRY_RUN" ]; then
  echo "FAST_MODE=$FAST_MODE"
  echo "IDLE_SLEEP=$IDLE_SLEEP"
  echo "MAX_TASKS=$MAX_TASKS"
  echo "MAX_ITERS=$MAX_ITERS"
  echo "USE_DOCKER=$USE_DOCKER"
  echo "ORTUS_BACKEND=$ORTUS_BACKEND"
  echo "CONDITION=$CONDITION"
  exit 0
fi

if [ -n "$DRY_RUN_CONDITION" ]; then
  build_condition
  exit 0
fi

# --print-cmd: testability-only debug flag. Build the exact claude argv that
# would be executed and print it to stdout in shell-quoted form (%q so the
# multi-line condition stays on one logical line — keeps the rg regex in
# AC test (a) simple). Exits before the flock dance so tests don't contend
# on .beads/ralph.flock (mirrors the --dry-run / --dry-run-condition
# isolation pattern). Deliberately omitted from -h (per yr7d.5 design).
if [ -n "$PRINT_CMD" ]; then
  # Built by the adapter, not inline, so what this prints cannot drift from
  # what a real launch would run — and so `--backend codex --print-cmd`
  # shows the Codex argv (FR-004) rather than a Claude one.
  if [ -n "$USE_DOCKER" ] && [ "$ORTUS_BACKEND" = "claude" ]; then
    CLAUDE_CMD=(docker sandbox run claude --name ortus-goal --)
  fi
  backend_argv goal "/goal $(build_condition)" || exit 1
  printf '%q ' "${BACKEND_ARGV[@]}"
  printf '\n'
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

# LOG_FILE + log() tee'd to host stream and on-disk log (FR-010). Defined
# ahead of sandbox.sh source because that module's functions call log().
# Log filename uses the same ISO-8601-compact YYYYMMDD-HHMMSS pattern as
# ralph.sh:95 so tail.sh's glob picks up both prefixes once yr7d.8 widens it.
mkdir -p logs
LOG_FILE="logs/goal-$(date '+%Y%m%d-%H%M%S').log"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "=== goal.sh Started ==="
if [ -n "$FAST_MODE" ]; then
  # Codex has no fast-output tier; --fast is a documented no-op there
  # (FR-004). Say so at launch rather than letting the operator believe a
  # flag they passed took effect.
  if [ "$ORTUS_BACKEND" = "codex" ]; then
    log "Fast mode: ignored ($FAST_MODE is Claude-only; no-op under the codex backend)"
  else
    log "Fast mode: enabled (2.5x faster output, premium pricing)"
  fi
fi
log "Log file: $LOG_FILE"
log "Watch live:"
log "  Human-readable: ./ortus/tail.sh         (auto-follows all logs)"
log "  Raw output:     tail -f $LOG_FILE"

# Capture initial ready backlog so the watcher knows session scope at start
# (mirrors ralph.sh's INITIAL_READY capture). goal.sh's long-lived /goal
# session has no per-task shell hook, so READY_REMAINING is reported once
# at the end rather than per-task. Guarded against every failure shape:
# bd missing (pipe degenerates, jq exits 0 on empty stdin), bd lock
# contention (stderr suppressed), jq missing (|| echo 0 fires). The regex
# re-check pins the variable to a non-negative integer for downstream math.
INITIAL_READY=$(bd ready --json 2>/dev/null | jq 'length' 2>/dev/null || echo 0)
[[ "$INITIAL_READY" =~ ^[0-9]+$ ]] || INITIAL_READY=0
log "Initial ready backlog: ${INITIAL_READY} ready remaining"

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

# Past this point the outer sandbox is enforced AND verified — neither branch
# above returns on failure, they exit. Publish that fact so the backend
# adapter can pick the codex inner posture from it (FR-010): a relaxed inner
# sandbox is only defensible behind an enforced outer one. An operator whose
# wrapper provides no outer sandbox sets ORTUS_OUTER_SANDBOX=off in the
# environment and the adapter falls back to a real inner sandbox — that
# opt-out changes the inner posture only; it never skips the gate above.
export ORTUS_OUTER_SANDBOX="${ORTUS_OUTER_SANDBOX:-enforced}"

# Hook-disabled precheck (ortus-sooj). Runs after sandbox/docker checks and
# before any claude spawn. /goal is implemented as a managed Stop hook; if
# disableAllHooks=true is set anywhere in the settings layer stack, the
# directive silently degrades. Refuse to launch in that case.
check_hooks_enabled

# bd exemption preflight (FR-006). The single most likely source of a silently
# broken loop is a sandbox that lets bd read the queue but not write it: the
# session then burns a full run and closes nothing. Runs after the posture is
# settled above (bd_preflight reads ORTUS_BACKEND and, under codex, the config
# CODEX_HOME points at) and before any agent spawn. Not skippable via env var,
# for the same reason the sandbox smoke test isn't — a skippable gate is just a
# slower way to reach the failure it was added to prevent.
backend_env
bd_preflight || {
  log "ERROR: refusing to start — see the bd preflight diagnostic above."
  exit 1
}

# Cache helpers (project-local .cache/ subdirs + XDG/per-tool cache env
# exports) live in ortus/lib/cache.sh so canonical/template parity (FR-022)
# is structural rather than copy-paste. No log() dependency.
source "$(dirname "${BASH_SOURCE[0]}")/lib/cache.sh"

# Invocation routing — when --docker is set, route the inner claude
# session through `docker sandbox run claude --name ortus-goal --` so it
# runs inside Docker's bundled-image sandbox. No Dockerfile; bind-mount
# defaults map host cwd -> /workspace; logs remain tee'd to the host
# LOG_FILE so tail.sh works in both modes. The --name is ortus-goal
# (not ortus-ralph) so docker can tell the two orchestrators apart.
# The wrapper is claude-specific, so it is only applied under that backend —
# same guard the --print-cmd path uses, so the printed argv and the executed
# one cannot disagree.
if [ -n "$USE_DOCKER" ] && [ "$ORTUS_BACKEND" = "claude" ]; then
  CLAUDE_CMD=(docker sandbox run claude --name ortus-goal --)
fi

# Build the /goal prompt: the literal directive name followed by the
# canonical (or -c-supplied) condition body from build_condition.
prompt="/goal $(build_condition)"

# The argv itself comes from the adapter (FR-003): goal.sh no longer knows
# which binary or which flags implement "run a /goal session", only that it
# wants the `goal` role. Stream flags, the permission posture and the
# --fast pass-through all live in lib/backend.sh.
backend_argv goal "$prompt" || exit 1

log "Invoking ${BACKEND_ARGV[0]} '/goal ...' (long-lived session; /goal evaluator owns termination)"
log "Press Ctrl+C to abort"

# pipefail ensures the agent's exit code propagates through the tee pipe
# (tee normally only fails if its output file is unwritable, which the
# mkdir + log() warm-up above would have already surfaced). The `||
# exit_code=$?` clause absorbs the non-zero exit so `set -e` doesn't kill
# the script before we log the end banner — we want goal.sh's own exit
# code to mirror the agent's, not abort silently mid-pipeline.
set -o pipefail
exit_code=0
"${BACKEND_ARGV[@]}" 2>&1 | tee -a "$LOG_FILE" >/dev/null || exit_code=$?

# Session-end progress bookend (analog of ralph.sh's per-iteration line).
# Derive drained = INITIAL_READY - READY_REMAINING; do not clamp — a model
# that files more follow-ups than it drains will surface a negative number
# or pct over/under 100%, which is accurate signal. Same regex re-check as
# the startup capture so bd-missing collapses to '?' rather than empty.
READY_REMAINING=$(bd ready --json 2>/dev/null | jq 'length' 2>/dev/null || echo '?')
[[ "$READY_REMAINING" =~ ^[0-9]+$ ]] || READY_REMAINING='?'
if [ "$INITIAL_READY" -gt 0 ] && [ "$READY_REMAINING" != '?' ]; then
  drained=$(( INITIAL_READY - READY_REMAINING ))
  pct=$(( drained * 100 / INITIAL_READY ))
  log "Session ended. Drained ${drained} of ${INITIAL_READY} (${pct}%) | ${READY_REMAINING} ready remaining"
else
  log "Session ended. Initial ready: ${INITIAL_READY}; ready remaining: ${READY_REMAINING}"
fi

log "=== goal.sh Ended (exit $exit_code) ==="
exit $exit_code
