#!/usr/bin/env bash
# Check structural parity between ortus/ralph.sh and ortus/goal.sh (FR-022).
#
# Both orchestrators must agree on the load-bearing invariants that justify
# running them side-by-side during the Phase 2 migration window:
#   1. flock path literal (shared lock = mutual exclusion)
#   2. sandbox_smoke_test invocation (NFR-001: no skip)
#   3. docker_precondition_check invocation (NFR-001: fail fast on missing docker)
#   4. lib/cache.sh source line (identical project-local .cache/ layout)
#   5. lib/sandbox.sh source line (precondition for 2 and 3)
#
# Exits 0 on parity, 1 on drift. Failure messages name the divergent invariant
# verbatim ("flock", "smoke", "docker", "cache", "sandbox") so the AC tests can
# grep them.

set -u

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$repo_root"

RALPH="ortus/ralph.sh"
GOAL="ortus/goal.sh"

if [ ! -f "$RALPH" ] || [ ! -f "$GOAL" ]; then
    echo "structural-parity: $RALPH or $GOAL missing — nothing to check" >&2
    exit 0
fi

# Phase 5 (dcr4): once ralph.sh has been reduced to the deprecation shim
# (<=10 lines, execs goal.sh), structural parity is no longer meaningful —
# there is only one real orchestrator. Detect the shim and skip with an OK
# message so `make parity` stays green. Reverting ralph.sh to its pre-shim
# body automatically re-engages the structural check (the shim heuristic
# stops matching), preserving the Phase 5 reversibility property.
if [ "$(wc -l < "$RALPH")" -le 10 ] && grep -qE '^exec ".*goal\.sh"' "$RALPH"; then
    echo "structural-parity: SKIP — $RALPH is the Phase 5 deprecation shim (execs $GOAL); structural invariants now live only in $GOAL"
    exit 0
fi

status=0

# compare_invariant <name> <ralph-tokens> <goal-tokens>
# Tokens are newline-separated, sort -u'd. Diverging sets fail with a
# diff-style message that includes <name> verbatim so external tests can
# grep for the invariant they perturbed.
compare_invariant() {
    local name="$1"
    local ralph_tokens="$2"
    local goal_tokens="$3"
    if [ -z "$ralph_tokens" ] && [ -z "$goal_tokens" ]; then
        echo "structural-parity: MISSING  invariant '$name' not found in either script" >&2
        status=1
        return
    fi
    if [ "$ralph_tokens" != "$goal_tokens" ]; then
        echo "structural-parity: DIVERGED invariant '$name' differs between $RALPH and $GOAL" >&2
        echo "  $RALPH:" >&2
        printf '%s\n' "${ralph_tokens:-(none)}" | sed 's/^/    /' >&2
        echo "  $GOAL:" >&2
        printf '%s\n' "${goal_tokens:-(none)}" | sed 's/^/    /' >&2
        status=1
    fi
}

# Invariant 1: flock path. Today both scripts use .beads/ralph.flock so the
# two orchestrators mutually exclude each other; any drift here splits the
# lock and reintroduces the orphan-dolt failure mode (see ralph.sh:36-49).
ralph_flock=$(grep -oE '\.beads/[A-Za-z0-9_.-]+\.flock' "$RALPH" | sort -u)
goal_flock=$(grep -oE '\.beads/[A-Za-z0-9_.-]+\.flock' "$GOAL" | sort -u)
compare_invariant "flock path" "$ralph_flock" "$goal_flock"

# Invariant 2: sandbox_smoke_test. Both scripts must call the function from
# lib/sandbox.sh on the non-docker branch (NFR-001 forbids skip env vars).
# Match the bare call (leading whitespace tolerated) so a one-shot grep
# perturbation of the call site fails the check.
ralph_smoke=$(grep -E '^[[:space:]]*sandbox_smoke_test([[:space:]]|$)' "$RALPH" | sed -E 's/^[[:space:]]+//' | sort -u)
goal_smoke=$(grep -E '^[[:space:]]*sandbox_smoke_test([[:space:]]|$)' "$GOAL" | sed -E 's/^[[:space:]]+//' | sort -u)
compare_invariant "smoke test call (sandbox_smoke_test)" "$ralph_smoke" "$goal_smoke"

# Invariant 3: docker_precondition_check. Both scripts must fail fast on
# missing docker when --docker is set (NFR-001 likewise).
ralph_docker=$(grep -E '^[[:space:]]*docker_precondition_check([[:space:]]|$)' "$RALPH" | sed -E 's/^[[:space:]]+//' | sort -u)
goal_docker=$(grep -E '^[[:space:]]*docker_precondition_check([[:space:]]|$)' "$GOAL" | sed -E 's/^[[:space:]]+//' | sort -u)
compare_invariant "docker check call (docker_precondition_check)" "$ralph_docker" "$goal_docker"

# Invariant 4: lib/cache.sh source. Both scripts must source the canonical
# cache-setup module so generated projects get identical .cache/ layout.
ralph_cache=$(grep -oE 'lib/cache\.sh' "$RALPH" | sort -u)
goal_cache=$(grep -oE 'lib/cache\.sh' "$GOAL" | sort -u)
compare_invariant "cache source (lib/cache.sh)" "$ralph_cache" "$goal_cache"

# Invariant 5: lib/sandbox.sh source. Precondition for invariants 2 and 3 —
# without this source line the smoke/docker calls would be undefined.
ralph_sandbox=$(grep -oE 'lib/sandbox\.sh' "$RALPH" | sort -u)
goal_sandbox=$(grep -oE 'lib/sandbox\.sh' "$GOAL" | sort -u)
compare_invariant "sandbox source (lib/sandbox.sh)" "$ralph_sandbox" "$goal_sandbox"

if [ "$status" -eq 0 ]; then
    echo "structural-parity: OK — $RALPH and $GOAL agree on flock, smoke, docker, cache, sandbox"
else
    echo "structural-parity: FAIL — see divergences above" >&2
fi

exit "$status"
