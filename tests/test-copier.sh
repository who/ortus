#!/bin/bash
# test-copier.sh - Validate copier template generation
#
# Usage: ./tests/test-copier.sh
#
# This test verifies:
#   1. copier copy --defaults succeeds with proper --data syntax
#   2. Generated project has expected files
#   3. Catches --data flag syntax errors (ortus-ib1)
#
# Exit codes:
#   0 - Template generation successful
#   1 - Template generation failed

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORTUS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_DIR="/tmp/ortus-copier-test-$$"

# Cleanup on exit
cleanup() {
  if [ -d "$TEST_DIR" ]; then
    rm -rf "$TEST_DIR"
  fi
}
trap cleanup EXIT

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

log_info() {
  echo -e "${GREEN}[INFO]${NC} $1"
}

log_error() {
  echo -e "${RED}[ERROR]${NC} $1"
}

# ============================================================================
# Test: copier copy with --data flags
# ============================================================================

log_info "Testing copier template generation..."
log_info "Test directory: $TEST_DIR"

mkdir -p "$TEST_DIR"

# Run copier with all --data flags on separate lines (like test scripts use)
# This validates that the shell properly handles line continuations
if ! copier copy --defaults --trust --skip-tasks \
  --data project_name=validation-test \
  --data project_description="Validation test project" \
  --data author_name="Test User" \
  --data author_email="test@example.com" \
  --data github_username=testuser \
  --data language=python \
  --data package_manager=uv \
  --data framework=none \
  --data linter=ruff \
  --data license=MIT \
  --data project_type=other \
  "$ORTUS_DIR" "$TEST_DIR/project" 2>&1; then
  log_error "Copier template generation failed"
  log_error "This may indicate malformed --data flags or template errors"
  exit 1
fi

log_info "Template generation succeeded"

# ============================================================================
# Verify expected files
# ============================================================================

log_info "Verifying generated files..."

EXPECTED_FILES=(
  "ralph.sh"
  "interview.sh"
  "idea.sh"
  "tail.sh"
  "PROMPT.md"
  "CLAUDE.md"
  "AGENTS.md"
  ".beads/config.yaml"
  "prompts/INTERVIEW-PROMPT.md"
  "prd/PRD-PROMPT.md"
)

MISSING=0
for file in "${EXPECTED_FILES[@]}"; do
  if [ ! -f "$TEST_DIR/project/$file" ]; then
    log_error "Missing expected file: $file"
    MISSING=$((MISSING + 1))
  fi
done

if [ $MISSING -gt 0 ]; then
  log_error "$MISSING expected files missing"
  exit 1
fi

log_info "All expected files present"

# ============================================================================
# Summary
# ============================================================================

echo ""
log_info "Copier validation test PASSED"
log_info "- Template generation: OK"
log_info "- Expected files: OK"
echo ""

exit 0
