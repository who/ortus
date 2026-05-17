#!/usr/bin/env bash
# Hermetic smoke against the local-dev Python ortus build (ortus-inam).
#
#   ./scripts/smoke-local.sh             # fast tests only (no API spend)
#   ./scripts/smoke-local.sh --slow      # include slow tests (real claude)
#   ./scripts/smoke-local.sh -k test_init    # pytest -k passthrough
#
# Exits 0 if all selected tests pass; non-zero otherwise.

set -euo pipefail

REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"

# Translate --slow into "drop the marker filter"; otherwise gate slow tests off.
PYTEST_ARGS=()
SLOW=0
for arg in "$@"; do
    if [ "$arg" = "--slow" ]; then
        SLOW=1
    else
        PYTEST_ARGS+=("$arg")
    fi
done

if [ "$SLOW" -eq 0 ]; then
    PYTEST_ARGS=("-m" "not slow" "${PYTEST_ARGS[@]}")
fi

exec uv run pytest tests/test_smoke_local.py "${PYTEST_ARGS[@]}"
