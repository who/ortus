# ortus/lib/backend.sh — sourceable backend adapter (claude only)
#
# One place that knows how to turn a role ("what we want the agent to do")
# into a concrete CLI argv. Launchers call these functions instead of
# hard-coding `claude ...` inline, so adding a second backend later is a
# change to this file rather than a change to every launcher.
#
# Public API:
#   backend_argv <role> [prompt] [prd_path]
#       Fills the global array BACKEND_ARGV with the full command line.
#       Roles: goal, prd-decompose, idea-expand.
#   backend_env
#       Exports the env a backend needs before it is invoked. For codex that
#       is CODEX_HOME=$PWD/.codex; for claude it is a no-op. Always returns 0.
#   backend_stream_flags
#       Fills the global array BACKEND_STREAM_FLAGS with the flags that make
#       the backend emit a machine-readable event stream (what tail.sh decodes).
#   backend_available
#       0 if the backend CLI is on PATH, 1 otherwise. Silent.
#   backend_preflight
#       0 if the backend can actually be invoked; otherwise prints a
#       diagnostic to stderr and returns 1.
#
# Arrays, not strings: every function publishes a named global array. A
# string would have to be re-split by the caller, and prompts contain
# spaces, quotes and newlines — re-splitting them silently corrupts the
# invocation.
#
# Env inputs:
#   ORTUS_BACKEND  backend name; only "claude" is implemented here.
#   ORTUS_BACKEND_DEFAULT
#                  the copier-generated default; final fallback (FR-002).
#   FAST_MODE      optional extra flag appended to the goal argv.
#   CLAUDE_CMD     optional array; the launcher's command prefix (e.g. the
#                  docker sandbox wrapper). Defaults to (claude).
#
# No dependency on log() — this module emits no diagnostics of its own.

# The backends the CLI surface accepts. Kept as one list so the validator and
# the error message can never disagree about what is spellable.
BACKEND_CHOICES="claude codex"

# The copier-generated default — the final fallback in FR-002's precedence
# chain. The template renders this from the `agent_cli` answer (ortus-1xvv);
# until that question lands the rendered value is "claude" either way. An
# already-exported value wins so a generated project can carry its own
# default without editing this file.
ORTUS_BACKEND_DEFAULT="${ORTUS_BACKEND_DEFAULT:-claude}"

# Resolve the backend name and echo it. Precedence: flag > ORTUS_BACKEND >
# generated default (FR-002). This is the ONLY implementation of that
# precedence — launchers pass their --backend value in and use what comes
# back, they never re-derive it (NFR-002).
#
# Validation lives here rather than in each launcher's flag parser so an
# invalid value fails identically whether it arrived by flag or by env.
resolve_backend() {
    local flag="${1:-}"
    local name="${flag:-${ORTUS_BACKEND:-$ORTUS_BACKEND_DEFAULT}}"

    case " $BACKEND_CHOICES " in
        *" $name "*) ;;
        *)
            echo "ERROR: unknown backend '$name' (valid: ${BACKEND_CHOICES// /, })" >&2
            return 1
            ;;
    esac

    printf '%s\n' "$name"
}

# The backend for the current process. Launchers export ORTUS_BACKEND after
# resolving their flag, so everything downstream of that export agrees.
backend_name() {
    resolve_backend ""
}

# Guard: fail loudly rather than silently emitting a Claude argv for a
# backend we do not implement yet.
_backend_require_claude() {
    local name
    # An unresolvable backend has already printed its own diagnostic; don't
    # follow it with a second, vaguer one about the empty string.
    name=$(backend_name) || return 1
    if [ "$name" != "claude" ]; then
        echo "ERROR: backend '$name' is not implemented (valid: claude)" >&2
        return 1
    fi
}

# Point Codex at the project's own config directory. Codex reads
# $CODEX_HOME/config.toml and defaults to ~/.codex — without this export a run
# would silently pick up the operator's global posture instead of the
# generated .codex/config.toml, which is exactly the drift the in-repo config
# exists to prevent (FR-005).
#
# Respects an explicit CODEX_HOME so an operator can point a one-off run
# elsewhere; the default is project-local.
backend_env() {
    [ "$(backend_name)" = "codex" ] || return 0
    CODEX_HOME="${CODEX_HOME:-$PWD/.codex}"
    export CODEX_HOME
    return 0
}

backend_stream_flags() {
    _backend_require_claude || return 1
    BACKEND_STREAM_FLAGS=(--output-format stream-json --verbose)
}

backend_argv() {
    # Before the guard: when the codex branch lands (FR-004) it inherits the
    # project-local CODEX_HOME without having to remember to ask for it.
    backend_env
    _backend_require_claude || return 1

    local role="${1:-}"
    local prompt="${2:-}"
    local prd_path="${3:-${ORTUS_PRD_PATH:-}}"

    # The launcher may wrap the CLI (e.g. `docker sandbox run claude --name
    # ortus-goal --`). Honour an already-set CLAUDE_CMD array; otherwise the
    # bare binary.
    local -a cmd
    # ${CLAUDE_CMD[*]:-} rather than ${#CLAUDE_CMD[@]} so an unset CLAUDE_CMD
    # is not a fatal unbound-variable error under the launchers' `set -u`.
    if [ -n "${CLAUDE_CMD[*]:-}" ]; then
        cmd=("${CLAUDE_CMD[@]}")
    else
        cmd=(claude)
    fi

    case "$role" in
        goal)
            backend_stream_flags
            BACKEND_ARGV=("${cmd[@]}" -p "$prompt" "${BACKEND_STREAM_FLAGS[@]}" --dangerously-skip-permissions)
            # FAST_MODE is a single opt-in flag, empty when unset — appending
            # it unquoted would inject an empty argv element.
            [ -n "${FAST_MODE:-}" ] && BACKEND_ARGV+=("$FAST_MODE")
            ;;
        prd-decompose)
            # The tool allowlist is scoped to the one PRD being decomposed
            # plus bd, so a decompose run cannot wander the filesystem.
            BACKEND_ARGV=("${cmd[@]}" --allowedTools "Read($prd_path),Bash(bd:*)" --dangerously-skip-permissions -p "$prompt")
            ;;
        idea-expand)
            # One-shot completion whose stdout the caller captures; no loop,
            # no stream decoding.
            BACKEND_ARGV=("${cmd[@]}" --print "$prompt")
            ;;
        *)
            echo "ERROR: unknown backend role '$role' (valid: goal, prd-decompose, idea-expand)" >&2
            return 1
            ;;
    esac
    return 0
}

backend_available() {
    _backend_require_claude 2>/dev/null || return 1
    command -v claude >/dev/null 2>&1
}

backend_preflight() {
    local name
    name=$(backend_name) || return 1
    if [ "$name" != "claude" ]; then
        echo "ERROR: backend '$name' is not implemented (valid: claude)" >&2
        return 1
    fi
    if ! command -v claude >/dev/null 2>&1; then
        echo "ERROR: 'claude' CLI not found on PATH." >&2
        echo "  Install it: https://code.claude.com/docs/en/quickstart" >&2
        return 1
    fi
    return 0
}
