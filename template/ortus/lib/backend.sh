# ortus/lib/backend.sh — sourceable backend adapter (claude, codex)
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
#   ORTUS_BACKEND  backend name; "claude" (all roles) or "codex" (goal role).
#   ORTUS_BACKEND_DEFAULT
#                  the copier-generated default; final fallback (FR-002).
#   FAST_MODE      optional extra flag appended to the goal argv. Claude-only;
#                  a documented no-op under codex (FR-004).
#   ORTUS_OUTER_SANDBOX
#                  whether the enforced outer OS sandbox (bwrap/Seatbelt, or
#                  --docker) is in play: "enforced" (default) or "off". This
#                  is what PICKS the codex inner posture (FR-010).
#   ORTUS_CODEX_POSTURE
#                  explicit override of the picked posture: "bypass" or
#                  "inner". Unset (the norm) means "derive it from
#                  ORTUS_OUTER_SANDBOX". See _backend_codex_posture.
#   ORTUS_CODEX_MODEL
#                  optional model name; appended as `-m <model>` to codex argv.
#   CLAUDE_CMD     optional array; the launcher's command prefix including the
#                  binary (e.g. the docker sandbox wrapper). Defaults to the
#                  bare backend binary.
#
# No dependency on log() — the only thing this module writes is a diagnostic
# on stderr when it refuses, or when it ignores a flag the backend cannot use.

# The backends the CLI surface accepts. Kept as one list so the validator and
# the error message can never disagree about what is spellable.
BACKEND_CHOICES="claude codex"

# The copier-generated default — the final fallback in FR-002's precedence
# chain. A generated project ships lib/backend-default.sh rendered from its
# `agent_cli` answer; this file is not templated (so `make parity` can compare
# it byte-for-byte between ortus/ and template/ortus/), it just reads that one
# if it is there. The Ortus repo itself has no such file and falls through to
# "claude". An already-exported value wins over both, so ORTUS_BACKEND_DEFAULT
# can be set by the environment without editing anything.
_backend_default_file="$(dirname "${BASH_SOURCE[0]}")/backend-default.sh"
# shellcheck source=/dev/null
[ -r "$_backend_default_file" ] && . "$_backend_default_file"
unset _backend_default_file
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
    local name
    name=$(backend_name) || return 1
    case "$name" in
        claude) BACKEND_STREAM_FLAGS=(--output-format stream-json --verbose) ;;
        codex)  BACKEND_STREAM_FLAGS=(--json) ;;
    esac
}

# The launcher may wrap the CLI (e.g. `docker sandbox run claude --name
# ortus-goal --`). Honour an already-set CLAUDE_CMD array — it carries the
# wrapper AND the binary — otherwise the bare binary for this backend.
_backend_cmd() {
    local default_bin="$1"
    # ${CLAUDE_CMD[*]:-} rather than ${#CLAUDE_CMD[@]} so an unset CLAUDE_CMD
    # is not a fatal unbound-variable error under the launchers' `set -u`.
    if [ -n "${CLAUDE_CMD[*]:-}" ]; then
        BACKEND_CMD=("${CLAUDE_CMD[@]}")
    else
        BACKEND_CMD=("$default_bin")
    fi
}

# Sandbox posture for codex roles, published as CODEX_POSTURE_ARGV.
#
# "bypass" mirrors today's Claude posture (--dangerously-skip-permissions
# inside an enforced outer bwrap/Seatbelt sandbox). Say the safety argument
# plainly: relaxing the INNER sandbox is safe ONLY because the OUTER one is
# enforced and smoke-tested by lib/sandbox.sh before anything launches. Take
# the outer layer away and the bypass is a full-privilege agent on the host.
#
# So the outer layer is what picks the posture (FR-010), not a standalone
# preference: outer enforced -> bypass; operator opted out of the outer
# sandbox -> a REAL inner sandbox (--sandbox workspace-write
# --ask-for-approval never) instead of a bypass. Defaulting
# ORTUS_OUTER_SANDBOX to "enforced" is safe because goal.sh's smoke test is
# unconditional and not skippable — an unset value means "goal.sh ran it".
#
# ORTUS_CODEX_POSTURE still overrides the derived value, for the operator who
# knows their wrapper's isolation better than we do. It is deliberately the
# override rather than the primary knob: the common path should require
# stating what the OUTER world looks like, not which flag to emit.
_backend_codex_posture() {
    local posture="${ORTUS_CODEX_POSTURE:-}"

    if [ -z "$posture" ]; then
        case "${ORTUS_OUTER_SANDBOX:-enforced}" in
            enforced) posture="bypass" ;;
            off)      posture="inner" ;;
            *)
                echo "ERROR: unknown outer sandbox state '${ORTUS_OUTER_SANDBOX}' (valid: enforced, off)" >&2
                return 1
                ;;
        esac
    fi

    case "$posture" in
        bypass) CODEX_POSTURE_ARGV=(--sandbox workspace-write --dangerously-bypass-approvals-and-sandbox) ;;
        inner)  CODEX_POSTURE_ARGV=(--sandbox workspace-write --ask-for-approval never) ;;
        *)
            echo "ERROR: unknown codex sandbox posture '${ORTUS_CODEX_POSTURE}' (valid: bypass, inner)" >&2
            return 1
            ;;
    esac
}

_backend_argv_claude() {
    local role="$1" prompt="$2" prd_path="$3"
    local -a cmd
    _backend_cmd claude
    cmd=("${BACKEND_CMD[@]}")

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
    esac
    return 0
}

_backend_argv_codex() {
    local role="$1" prompt="$2"
    local -a cmd

    if [ "$role" != "goal" ]; then
        echo "ERROR: backend 'codex' does not implement role '$role' yet (implemented: goal)" >&2
        return 1
    fi

    _backend_cmd codex
    cmd=("${BACKEND_CMD[@]}")
    backend_stream_flags
    _backend_codex_posture || return 1

    # The prompt is positional under `codex exec`, not behind -p, but it is
    # the SAME string the claude branch passes: the /goal directive text is
    # built once by the launcher and is byte-identical across backends.
    BACKEND_ARGV=("${cmd[@]}" exec "$prompt" "${BACKEND_STREAM_FLAGS[@]}" "${CODEX_POSTURE_ARGV[@]}")

    # Model selection is provisional pending Q5 (ortus-z5tj); until then it is
    # an opt-in env var and the flag is omitted entirely when unset, so the
    # Codex CLI's own default applies.
    [ -n "${ORTUS_CODEX_MODEL:-}" ] && BACKEND_ARGV+=(-m "$ORTUS_CODEX_MODEL")

    # Codex has no fast-output tier. Say so rather than silently dropping a
    # flag the operator explicitly asked for — but do not fail: --fast is a
    # documented no-op here, not an error (FR-004).
    if [ -n "${FAST_MODE:-}" ]; then
        echo "NOTE: $FAST_MODE is a Claude-only flag and is a no-op under the codex backend; ignoring." >&2
    fi
    return 0
}

backend_argv() {
    # Before anything else, so the codex branch inherits the project-local
    # CODEX_HOME without having to remember to ask for it.
    backend_env

    local name
    name=$(backend_name) || return 1

    local role="${1:-}"
    local prompt="${2:-}"
    local prd_path="${3:-${ORTUS_PRD_PATH:-}}"

    # Role validation is shared: an unknown role fails identically whichever
    # backend is active, so the error can never disagree with itself.
    case "$role" in
        goal|prd-decompose|idea-expand) ;;
        *)
            echo "ERROR: unknown backend role '$role' (valid: goal, prd-decompose, idea-expand)" >&2
            return 1
            ;;
    esac

    case "$name" in
        claude) _backend_argv_claude "$role" "$prompt" "$prd_path" ;;
        codex)  _backend_argv_codex "$role" "$prompt" ;;
    esac
}

# The binary each backend name spells. Kept beside the install/login hints so
# a diagnostic can never name one backend's binary and another's fix.
_backend_binary() {
    case "$1" in
        claude) printf 'claude\n' ;;
        codex)  printf 'codex\n' ;;
    esac
}

_backend_install_hint() {
    case "$1" in
        claude) printf 'npm install -g @anthropic-ai/claude-code  (docs: https://code.claude.com/docs/en/quickstart)\n' ;;
        codex)  printf 'npm install -g @openai/codex  (docs: https://developers.openai.com/codex/cli)\n' ;;
    esac
}

_backend_login_hint() {
    case "$1" in
        claude) printf 'claude  # then run /login  (or export ANTHROPIC_API_KEY)\n' ;;
        codex)  printf 'codex login  (or export OPENAI_API_KEY)\n' ;;
    esac
}

backend_available() {
    local name bin
    name=$(backend_name 2>/dev/null) || return 1
    bin=$(_backend_binary "$name")
    command -v "$bin" >/dev/null 2>&1
}

# Is the selected CLI carrying credentials? Neither CLI offers a cheap
# non-interactive "am I logged in" call for every auth mode, so this probes
# for the EVIDENCE of a login rather than performing one. Being wrong in the
# permissive direction is the safe failure: a false pass costs one clear
# error from the CLI itself, while a false fail would refuse to launch a
# perfectly working loop.
_backend_authenticated() {
    local name="$1"

    case "$name" in
        claude)
            # Either auth mode counts: an API key / OAuth token in the
            # environment, or a credentials store written by /login.
            [ -n "${ANTHROPIC_API_KEY:-}" ] && return 0
            [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && return 0
            [ -f "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/.credentials.json" ] && return 0
            # macOS keeps the same credentials in the Keychain, where there is
            # no file to stat.
            if [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
                security find-generic-password -s "Claude Code-credentials" >/dev/null 2>&1 && return 0
            fi
            return 1
            ;;
        codex)
            [ -n "${OPENAI_API_KEY:-}" ] && return 0
            # `codex login status` is non-interactive and exits non-zero when
            # logged out. It reads CODEX_HOME, which backend_preflight has
            # already pointed at the project (backend_env).
            codex login status >/dev/null 2>&1 && return 0
            [ -f "${CODEX_HOME:-$HOME/.codex}/auth.json" ] && return 0
            return 1
            ;;
    esac
    return 0
}

# Fail fast, naming BOTH the backend and the command that fixes it — the
# operator should never have to guess which of two CLIs the message is about.
# Launchers call this before acquiring the flock so a refused launch cannot
# leave a stale lock behind (ortus-3gox).
backend_preflight() {
    local name bin
    name=$(backend_name) || return 1
    backend_env

    bin=$(_backend_binary "$name")
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "ERROR: backend '$name' selected but its CLI '$bin' is not on PATH." >&2
        echo "  Install it: $(_backend_install_hint "$name")" >&2
        return 1
    fi

    if ! _backend_authenticated "$name"; then
        echo "ERROR: backend '$name' CLI '$bin' is not authenticated." >&2
        echo "  Log in: $(_backend_login_hint "$name")" >&2
        return 1
    fi
    return 0
}
