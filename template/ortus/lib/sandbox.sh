# ortus/lib/sandbox.sh — sourceable sandbox helpers
#
# Defines:
#   sandbox_smoke_test       — Tier 1 (native OS sandbox) prerequisite check
#   docker_precondition_check — Tier 2 (--docker) prerequisite check
#   bd_preflight             — bd-reachable-under-the-loop's-posture gate (FR-006)
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

# --- bd exemption preflight (FR-006 / PRD Risk 2) ----------------------------
#
# bd keeps its issue database in an embedded Dolt store under .beads/, inside
# the workspace. What that actually requires of a sandbox was measured, not
# assumed (ortus-863z, codex-cli 0.144.6, bwrap on WSL2):
#
#   * `sandbox_mode = "workspace-write"` with the repo root as the workspace is
#     SUFFICIENT. Under `bwrap --unshare-net` with everything outside the repo
#     read-only, both `bd ready --json` and `bd create` succeed. Nothing bd
#     touches on either path lives outside the workspace, so widening
#     writable_roots would be over-granting.
#   * `network_access` is therefore NOT needed by bd itself. It IS needed by
#     `bd dolt push`, which the session-close protocol runs whenever a git
#     remote is configured. So network_access = false is a hard failure for a
#     project with a remote and a note for a local-only one — the loop would
#     otherwise strand every closed issue locally.
#
# That is the empirical answer to Q4: grant workspace-write, do not widen the
# write scope, and leave the per-domain allowlist to the outer sandbox (Codex's
# [sandbox_workspace_write] has no per-domain filter — network_access is
# all-or-nothing).
#
# Two checks, because they prove different things. The declared-posture check
# reads the config Codex WILL enforce; the live check runs bd here and now.
# The preflight itself runs on the host, outside the inner sandbox, so a
# passing live run alone would say nothing about what Codex permits.
#
# Returns non-zero instead of exiting: the caller decides, and the shape is
# testable without spawning a subshell just to catch an exit.
bd_preflight() {
  log "bd preflight..."

  if ! command -v bd >/dev/null 2>&1; then
    log "ERROR: bd preflight failed: 'bd' not found on PATH"
    log "  Every task the loop closes is tracked in beads; without bd the loop"
    log "  would spin without ever recording work. Install: https://github.com/steveyegge/beads"
    return 1
  fi

  if [ "${ORTUS_BACKEND:-claude}" = "codex" ]; then
    _bd_preflight_codex_config || return 1
  fi

  # bd invoked directly, never wrapped: under Claude Code the sandbox
  # exemption (`sandbox.excludedCommands: ["bd", "bd *"]`) only fires when the
  # harness sees `bd` as the directly-invoked command, and a wrapped bd
  # (`bd ... | jq`, `xargs bd`, `bash -c "bd ..."`) runs as a sandboxed child
  # of the wrapper and hangs on dolt. Command substitution captures stdout
  # without interposing a wrapper process, so it is safe where a pipe is not.
  local out status
  out=$(bd ready --json 2>&1)
  status=$?
  if [ "$status" -ne 0 ]; then
    log "ERROR: bd preflight failed: 'bd ready --json' exited $status"
    log "  Output: ${out:-<empty>}"
    log "  bd could not reach its embedded Dolt store under the loop's posture."
    log "  Check that .beads/ is inside the workspace and writable, and that no"
    log "  stale .beads/dolt.flock is held by a dead process."
    return 1
  fi

  # Exit 0 with unparseable output means bd answered but not with the issue
  # list the loop reads — a degraded bd is as broken as an absent one here.
  case "${out#"${out%%[![:space:]]*}"}" in
    '['*) ;;
    *)
      log "ERROR: bd preflight failed: 'bd ready --json' exited 0 but did not print a JSON array"
      log "  Output: ${out:-<empty>}"
      return 1
      ;;
  esac

  log "bd preflight: ok (bd ready --json returned a JSON array)"
  return 0
}

# Read one scalar from a TOML file. Deliberately a line scan rather than a
# parser: the file we validate is the one Ortus generates, whose keys are
# unique across the whole file, and a preflight that needed a TOML library
# would be one more thing to install before the loop can start.
_bd_toml_scalar() {
  local file="$1" key="$2"
  sed -n -E "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*\"?([^\"#]*)\"?.*/\1/p" "$file" \
    | tail -1 | tr -d '[:space:]'
}

# Assert the posture Codex WILL enforce would let bd work, before we trust the
# live run. Failures here are the loud, specific ones the loop refuses on.
_bd_preflight_codex_config() {
  local config="${CODEX_HOME:-$PWD/.codex}/config.toml"

  if [ ! -f "$config" ]; then
    log "ERROR: bd preflight failed: no Codex config at $config"
    log "  Ortus exports CODEX_HOME=\$PWD/.codex so Codex reads the project's"
    log "  reviewed posture, not the operator's global ~/.codex/config.toml."
    log "  Regenerate the project from the template, or copy .codex/config.toml in."
    return 1
  fi

  local mode
  mode=$(_bd_toml_scalar "$config" sandbox_mode)
  case "$mode" in
    workspace-write|danger-full-access) ;;
    read-only)
      log "ERROR: bd preflight failed: sandbox_mode = \"read-only\" in $config"
      log "  bd writes its embedded Dolt store under .beads/; a read-only sandbox"
      log "  lets the agent read the queue and silently fail to close anything."
      log "  Set sandbox_mode = \"workspace-write\"."
      return 1
      ;;
    '')
      log "ERROR: bd preflight failed: no sandbox_mode set in $config"
      log "  Set sandbox_mode = \"workspace-write\" — bd needs to write .beads/."
      return 1
      ;;
    *)
      log "ERROR: bd preflight failed: unknown sandbox_mode \"$mode\" in $config"
      log "  Valid: workspace-write (what bd needs), danger-full-access, read-only."
      return 1
      ;;
  esac

  # network_access = false does not stop bd from reading or writing locally,
  # but it does stop `bd dolt push` — so it only matters when there is a remote
  # to push to. Failing unconditionally would block the local-only projects
  # that are correctly configured; staying silent would let a remote-backed
  # loop close 40 issues and strand every one of them.
  local net
  net=$(_bd_toml_scalar "$config" network_access)
  if [ "$net" = "false" ]; then
    if [ -n "$(git remote 2>/dev/null)" ]; then
      log "ERROR: bd preflight failed: network_access = false in $config"
      log "  This project has a git remote, so the session-close protocol runs"
      log "  'bd dolt push' — which needs the network. With it off the loop would"
      log "  close issues and strand every one of them locally."
      log "  Set [sandbox_workspace_write].network_access = true."
      return 1
    fi
    log "bd preflight: network_access = false; ok for this local-only project"
    log "  (bd's embedded Dolt store is local; only 'bd dolt push' needs the network)"
  fi

  return 0
}
