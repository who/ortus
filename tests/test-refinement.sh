#!/bin/bash
# test-refinement.sh - Test suite for ralph.sh refinement pipeline (PRD generation)
#
# Usage: ./tests/test-refinement.sh [--dry-run] [--keep]
#
# Options:
#   --dry-run   Set up test project but don't run ralph (for inspection)
#   --keep      Keep the test project after completion (default: cleanup)
#
# This test:
#   1. Generates a test project from the ortus template
#   2. Creates a feature bead assigned to ralph with 'interviewed' label
#   3. Adds interview comments to simulate completed interview
#   4. Runs ralph.sh --refinement-only and verifies PRD generation
#   5. Simulates human approval
#   6. Verifies implementation tasks are created
#
# Exit codes:
#   0 - All tests passed
#   1 - Test setup failed
#   2 - PRD generation failed
#   3 - Task generation failed

set -e

# Defaults
DRY_RUN=false
KEEP_PROJECT=false
TEST_DIR="/tmp/ortus-test-refinement-$$"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORTUS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run)
      DRY_RUN=true
      KEEP_PROJECT=true  # Implied
      shift
      ;;
    --keep)
      KEEP_PROJECT=true
      shift
      ;;
    -h|--help)
      head -n 22 "$0" | tail -n +2 | sed 's/^# //' | sed 's/^#//'
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

# Cleanup function
cleanup() {
  if [ "$KEEP_PROJECT" = false ] && [ -d "$TEST_DIR" ]; then
    echo "Cleaning up test directory: $TEST_DIR"
    rm -rf "$TEST_DIR"
  fi
}

# Set up trap for cleanup on exit (unless --keep)
trap cleanup EXIT

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
  echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
  echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
  echo -e "${RED}[ERROR]${NC} $1"
}

log_step() {
  echo ""
  echo -e "${GREEN}===${NC} $1 ${GREEN}===${NC}"
}

# Count beads by assignee and status
count_beads() {
  local assignee=$1
  local status=$2
  bd list --assignee="$assignee" --status="$status" --json 2>/dev/null | jq -r 'length' 2>/dev/null || echo "0"
}

# Get feature labels
get_labels() {
  local feature_id=$1
  bd show "$feature_id" --json 2>/dev/null | jq -r '.[0].labels // [] | join(",")' 2>/dev/null || echo ""
}

# Wait for ralph to process (with timeout)
wait_for_label() {
  local feature_id=$1
  local expected_label=$2
  local timeout=${3:-60}
  local elapsed=0

  while [ $elapsed -lt $timeout ]; do
    local labels
    labels=$(get_labels "$feature_id")
    if [[ "$labels" == *"$expected_label"* ]]; then
      return 0
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done

  return 1  # Timeout
}

# ============================================================================
# Test Setup
# ============================================================================

log_step "Setting up test environment"

# Create test directory
mkdir -p "$TEST_DIR"
log_info "Test directory: $TEST_DIR"

# Generate project from template
# Note: The template runs idea.sh at the end which calls Claude for up-sampling.
# We use --skip-tasks to avoid this interactive step, then manually initialize.
log_info "Generating project from template (skipping tasks)..."
copier copy --defaults --trust --skip-tasks \
  --data project_name=testproj \
  --data project_description="Test project for ralph.sh refinement" \
  --data author_name="Test User" \
  --data author_email="test@example.com" \
  --data github_username=testuser \
  --data language=python \
  --data package_manager=uv \
  --data framework=none \
  --data linter=ruff \
  --data license=MIT \
  "$ORTUS_DIR" "$TEST_DIR/testproj"

cd "$TEST_DIR/testproj"
log_info "Project generated at: $(pwd)"

# Manually run the initialization tasks (except idea.sh)
log_info "Running manual initialization..."
chmod +x ralph.sh interview.sh idea.sh tail.sh 2>/dev/null || true
git init >/dev/null 2>&1
bd init >/dev/null 2>&1
git add -A >/dev/null 2>&1
git commit -m 'Initial commit from Ortus template' >/dev/null 2>&1
log_info "Initialization complete"

# Verify project structure
if [ ! -f "ralph.sh" ]; then
  log_error "ralph.sh not found in generated project"
  exit 1
fi

log_info "Project structure verified"

# ============================================================================
# Create Test Feature with Interview
# ============================================================================

log_step "Creating test feature with interview"

FEATURE_TITLE="Simple calculator CLI"
FEATURE_DESC="A command-line calculator that can add, subtract, multiply, and divide two numbers. Should handle basic error cases like division by zero."

# Create feature assigned to ralph
FEATURE_OUTPUT=$(bd create --title="$FEATURE_TITLE" --type=feature --assignee=ralph --description="$FEATURE_DESC" 2>&1)
FEATURE_ID=$(echo "$FEATURE_OUTPUT" | grep -oE '[a-z]+-[a-z0-9]+' | head -1)

if [ -z "$FEATURE_ID" ]; then
  log_error "Failed to create feature bead"
  log_error "Output: $FEATURE_OUTPUT"
  exit 1
fi

log_info "Created feature: $FEATURE_ID - $FEATURE_TITLE"

# Verify feature exists
if ! bd show "$FEATURE_ID" >/dev/null 2>&1; then
  log_error "Feature $FEATURE_ID not found after creation"
  exit 1
fi

# Add interview comments to simulate completed interview
log_info "Adding simulated interview answers..."
bd comments add "$FEATURE_ID" "Q: What problem does this solve?
A: Users need a quick way to perform basic math calculations from the command line without opening a full calculator app." >/dev/null 2>&1

bd comments add "$FEATURE_ID" "Q: Who are the primary users?
A: Developers and power users who work primarily in the terminal." >/dev/null 2>&1

bd comments add "$FEATURE_ID" "Q: What operations should be supported?
A: Addition, subtraction, multiplication, division. Division by zero should return an error message." >/dev/null 2>&1

bd comments add "$FEATURE_ID" "Interview Summary:
- Key problem: Quick CLI math calculations
- Target users: Terminal power users
- Scope: Basic 4 operations with error handling
- Success criteria: Correct results, clear error messages" >/dev/null 2>&1

# Add 'interviewed' label to trigger PRD generation
bd label add "$FEATURE_ID" "interviewed" >/dev/null 2>&1 || {
  log_error "Failed to add interviewed label"
  exit 1
}

log_info "Added 'interviewed' label to trigger PRD generation"

# ============================================================================
# Dry Run Mode
# ============================================================================

if [ "$DRY_RUN" = true ]; then
  log_step "DRY RUN MODE - Project setup complete"
  log_info "Test project created at: $TEST_DIR/testproj"
  log_info "Feature created: $FEATURE_ID (with 'interviewed' label)"
  echo ""
  log_info "To run ralph manually:"
  echo "  cd $TEST_DIR/testproj"
  echo "  ./ralph.sh --refinement-only --poll-interval 5 --idle-sleep 10"
  echo ""
  log_info "In another terminal, watch the beads:"
  echo "  cd $TEST_DIR/testproj"
  echo "  watch -n 2 'bd list'"
  exit 0
fi

# ============================================================================
# Test 1: Run ralph to generate PRD
# ============================================================================

log_step "Test 1: Ralph generates PRD document"

# Start ralph in background with refinement-only mode and short intervals
./ralph.sh --refinement-only --poll-interval 5 --idle-sleep 10 &
RALPH_PID=$!

# Give ralph time to pick up the feature and generate PRD
log_info "Started ralph (PID: $RALPH_PID), waiting for PRD generation..."

# Wait for prd:ready label (up to 120 seconds - PRD generation takes time)
if ! wait_for_label "$FEATURE_ID" "prd:ready" 120; then
  log_error "Test 1 FAILED: Timeout waiting for prd:ready label"
  kill $RALPH_PID 2>/dev/null || true
  exit 2
fi

log_info "Feature now has prd:ready label"

# Check PRD file exists
SLUG=$(echo "$FEATURE_TITLE" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g' | sed 's/^-//' | sed 's/-$//')
PRD_FILE="prd/PRD-${SLUG}.md"

if [ ! -f "$PRD_FILE" ]; then
  log_error "Test 1 FAILED: PRD file not found at $PRD_FILE"
  kill $RALPH_PID 2>/dev/null || true
  exit 2
fi

log_info "PRD file created: $PRD_FILE"
log_info "Test 1 PASSED: PRD document generated"

# ============================================================================
# Test 2: Simulate human approval and task generation
# ============================================================================

log_step "Test 2: Approve PRD and generate implementation tasks"

# Add approved label
bd label add "$FEATURE_ID" "approved" >/dev/null 2>&1 || {
  log_error "Failed to add approved label"
  kill $RALPH_PID 2>/dev/null || true
  exit 3
}

log_info "Added 'approved' label to feature"

# Wait for feature to be closed and tasks created (up to 120 seconds)
log_info "Waiting for task generation..."

WAIT_TIME=0
while [ $WAIT_TIME -lt 120 ]; do
  FEATURE_STATUS=$(bd show "$FEATURE_ID" --json 2>/dev/null | jq -r '.[0].status' 2>/dev/null || echo "open")
  if [ "$FEATURE_STATUS" = "closed" ]; then
    break
  fi
  sleep 5
  WAIT_TIME=$((WAIT_TIME + 5))
done

if [ "$FEATURE_STATUS" != "closed" ]; then
  log_error "Test 2 FAILED: Timeout waiting for feature to be closed"
  kill $RALPH_PID 2>/dev/null || true
  exit 3
fi

log_info "Feature closed"

# Stop ralph
kill $RALPH_PID 2>/dev/null || true
wait $RALPH_PID 2>/dev/null || true
log_info "Stopped ralph"

# Check tasks created for ralph
TASK_COUNT=$(bd list --assignee=ralph --type=task --status=open --json 2>/dev/null | jq -r 'length' 2>/dev/null || echo "0")
log_info "Implementation tasks created: $TASK_COUNT"

if [ "$TASK_COUNT" -lt 3 ]; then
  log_error "Test 2 FAILED: Expected at least 3 tasks, got $TASK_COUNT"
  exit 3
fi

log_info "Test 2 PASSED: $TASK_COUNT implementation tasks created"

# ============================================================================
# Summary
# ============================================================================

log_step "All Tests Passed!"
echo ""
log_info "Test 1: Ralph generated PRD document ($PRD_FILE)"
log_info "Test 2: Ralph created $TASK_COUNT implementation tasks"
echo ""
log_info "Final state:"
bd list
echo ""

if [ "$KEEP_PROJECT" = true ]; then
  log_info "Test project preserved at: $TEST_DIR/testproj"
else
  log_info "Test project will be cleaned up"
fi

exit 0
