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

# Sandbox smoke test (FR-004) — fails fast if OS sandbox prerequisites are
# missing, before any iteration runs claude with --dangerously-skip-permissions.
# Per ortus-hhq9 decision, this check is intentionally NOT skippable via env
# var: skippability re-introduces the silent-degradation failure mode that
# sandbox hardening is designed to eliminate. For unsandboxed CI runners, use
# the --docker mode (Phase 2) which provides container-level isolation instead.
sandbox_smoke_test() {
  log "Sandbox smoke test..."
  local platform
  platform=$(uname -s)
  case "$platform" in
    Linux)
      if ! command -v bwrap >/dev/null 2>&1; then
        log "ERROR: Sandbox prerequisite missing: bubblewrap (bwrap)"
        log "  Install on Debian/Ubuntu/WSL2: sudo apt-get install bubblewrap socat"
        log "  Note: WSL1 is unsupported (requires WSL2's Linux kernel)"
        exit 1
      fi
      ;;
    Darwin)
      if ! command -v sandbox-exec >/dev/null 2>&1; then
        log "ERROR: Sandbox prerequisite missing: Seatbelt (sandbox-exec)"
        log "  Seatbelt is built into macOS; absence indicates a system-level issue"
        exit 1
      fi
      ;;
    *)
      log "ERROR: Unsupported platform '$platform' for native sandbox"
      log "  Supported: Linux/WSL2 (bubblewrap+socat), macOS (Seatbelt built-in)"
      exit 1
      ;;
  esac
  log "Sandbox smoke test: ok ($platform)"
}

# Docker precondition check (ortus-heot decision, ortus-lfft.7) — when --docker
# is set, fail fast with a friendly install hint if Docker or its bundled-image
# `docker sandbox` subcommand is unavailable. Mirrors the detect-and-message
# pattern from sandbox_smoke_test() so Tier 2 (--docker) and Tier 1 (native)
# share the same friendly-error tone.
docker_precondition_check() {
  log "Docker precondition check..."
  if ! command -v docker >/dev/null 2>&1; then
    log "ERROR: --docker requires Docker, but 'docker' was not found on PATH"
    local platform
    platform=$(uname -s)
    case "$platform" in
      Darwin)
        log "  Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
        log "  Or via Homebrew: brew install --cask docker"
        ;;
      Linux)
        log "  Install Docker Engine: https://docs.docker.com/engine/install/"
        ;;
      *)
        log "  Install Docker for your platform: https://docs.docker.com/get-docker/"
        ;;
    esac
    exit 1
  fi
  if ! docker sandbox --help >/dev/null 2>&1; then
    log "ERROR: --docker requires the bundled-image 'docker sandbox' subcommand, which is unavailable"
    log "  Update Docker Desktop to a version with the bundled-image rollout"
    log "  See: https://docs.docker.com/desktop/release-notes/"
    exit 1
  fi
  log "Docker precondition check: ok"
}

if [ -n "$USE_DOCKER" ]; then
  docker_precondition_check
else
  sandbox_smoke_test
fi

# Cache relocation (ortus-zj9v) — the OS sandbox profile mounts ~/.cache
# read-only, which blocks package-manager writes (uv/pip/npm/cargo). Point
# XDG and per-tool cache dirs into a project-local .cache/ inside the
# sandbox-writable filesystem. Bounded, cleanable, and matches the
# minimal-writable-surface stance from FR-001.
mkdir -p .cache/uv .cache/pip .cache/npm .cache/cargo .cache/go-mod .cache/go-build
export XDG_CACHE_HOME="$PWD/.cache"
export UV_CACHE_DIR="$PWD/.cache/uv"
export PIP_CACHE_DIR="$PWD/.cache/pip"
export npm_config_cache="$PWD/.cache/npm"
export CARGO_HOME="$PWD/.cache/cargo"
export GOMODCACHE="$PWD/.cache/go-mod"
export GOCACHE="$PWD/.cache/go-build"

# Claude invocation routing (FR-006, ortus-lfft.2) — when --docker is set,
# route the inner claude session through `docker sandbox run claude --name
# ortus-ralph --` so it runs inside Docker's bundled-image sandbox. No
# Dockerfile (NFR-003); bind-mount defaults map host cwd → /workspace; logs
# remain tee'd to the host LOG_FILE so tail.sh works in both modes.
if [ -n "$USE_DOCKER" ]; then
  CLAUDE_CMD=(docker sandbox run claude --name ortus-ralph --)
else
  CLAUDE_CMD=(claude)
fi

tasks_completed=0
iteration=0

while true; do
  iteration=$((iteration + 1))
  log ""
  log "--- Starting iteration $iteration ---"

  result=$("${CLAUDE_CMD[@]}" -p "$(cat "$(dirname "$0")/prompts/ralph-prompt.md")" --output-format stream-json --verbose --dangerously-skip-permissions $FAST_MODE 2>&1 | tee -a "$LOG_FILE") || true

  if [[ "$result" == *"<promise>COMPLETE</promise>"* ]]; then
    tasks_completed=$((tasks_completed + 1))
    log "Task completed. Total: $tasks_completed"
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
