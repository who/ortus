# ortus/lib/sandbox.sh — sourceable sandbox helpers
#
# Defines:
#   sandbox_smoke_test       — Tier 1 (native OS sandbox) prerequisite check
#   docker_precondition_check — Tier 2 (--docker) prerequisite check
#
# Sourced by ortus/ralph.sh (and, in Phase 2, ortus/goal.sh) so the canonical
# definitions live in one place and ralph.sh/goal.sh invariant parity (FR-022)
# is structurally assertable rather than copy-paste.
#
# Functions depend on a `log` helper defined by the sourcing script — source
# this module after `log()` is defined.

# Sandbox smoke test — fails fast if OS sandbox prerequisites are
# missing, before any iteration runs claude with --dangerously-skip-permissions.
# This check is intentionally NOT skippable via env
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

# Docker precondition check — when --docker
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
