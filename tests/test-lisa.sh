#!/bin/bash
# test-lisa.sh - Test suite for lisa.sh PRD pipeline
#
# Usage: ./tests/test-lisa.sh [--dry-run] [--keep]
#
# Options:
#   --dry-run   Set up test project but don't run lisa (for inspection)
#   --keep      Keep the test project after completion (default: cleanup)
#
# This test:
#   1. Generates a test project from the ortus template
#   2. Creates an idea bead assigned to lisa
#   3. Runs lisa.sh and verifies it generates interview questions
#   4. Simulates human answering questions
#   5. Verifies PRD generation
#   6. Simulates human approval
#   7. Verifies tasks are created for ralph
#
# Exit codes:
#   0 - All tests passed
#   1 - Test setup failed
#   2 - Interview generation failed
#   3 - PRD generation failed
#   4 - Task generation failed

set -e

# Defaults
DRY_RUN=false
KEEP_PROJECT=false
TEST_DIR="/tmp/ortus-test-lisa-$$"
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
      head -n 24 "$0" | tail -n +2 | sed 's/^# //' | sed 's/^#//'
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

# Get idea labels
get_labels() {
  local idea_id=$1
  bd show "$idea_id" --json 2>/dev/null | jq -r '.[0].labels // [] | join(",")' 2>/dev/null || echo ""
}

# Wait for lisa to process (with timeout)
wait_for_label() {
  local idea_id=$1
  local expected_label=$2
  local timeout=${3:-60}
  local elapsed=0

  while [ $elapsed -lt $timeout ]; do
    local labels
    labels=$(get_labels "$idea_id")
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
  --data project_description="Test project for lisa.sh" \
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
chmod +x ralph.sh lisa.sh interview.sh idea.sh tail.sh 2>/dev/null || true
git init >/dev/null 2>&1
bd init >/dev/null 2>&1
git add -A >/dev/null 2>&1
git commit -m 'Initial commit from Ortus template' >/dev/null 2>&1
log_info "Initialization complete"

# Verify project structure
if [ ! -f "lisa.sh" ]; then
  log_error "lisa.sh not found in generated project"
  exit 1
fi

if [ ! -f "ralph.sh" ]; then
  log_error "ralph.sh not found in generated project"
  exit 1
fi

log_info "Project structure verified"

# ============================================================================
# Create Test Idea
# ============================================================================

log_step "Creating test idea"

IDEA_TITLE="Simple calculator CLI"
IDEA_DESC="A command-line calculator that can add, subtract, multiply, and divide two numbers. Should handle basic error cases like division by zero."

# Use 'feature' type since 'idea' is not a valid beads type
IDEA_OUTPUT=$(bd create --title="$IDEA_TITLE" --type=feature --assignee=lisa --description="$IDEA_DESC" 2>&1)
IDEA_ID=$(echo "$IDEA_OUTPUT" | grep -oE '[a-z]+-[a-z0-9]+' | head -1)

if [ -z "$IDEA_ID" ]; then
  log_error "Failed to create idea bead"
  log_error "Output: $IDEA_OUTPUT"
  exit 1
fi

log_info "Created idea: $IDEA_ID - $IDEA_TITLE"

# Verify idea exists
if ! bd show "$IDEA_ID" >/dev/null 2>&1; then
  log_error "Idea $IDEA_ID not found after creation"
  exit 1
fi

# ============================================================================
# Dry Run Mode
# ============================================================================

if [ "$DRY_RUN" = true ]; then
  log_step "DRY RUN MODE - Project setup complete"
  log_info "Test project created at: $TEST_DIR/testproj"
  log_info "Idea created: $IDEA_ID"
  echo ""
  log_info "To run lisa manually:"
  echo "  cd $TEST_DIR/testproj"
  echo "  ./lisa.sh --poll-interval 5 --idle-sleep 10"
  echo ""
  log_info "In another terminal, watch the beads:"
  echo "  cd $TEST_DIR/testproj"
  echo "  watch -n 2 'bd list'"
  exit 0
fi

# ============================================================================
# Test 1: Run lisa to generate interview questions
# ============================================================================

log_step "Test 1: Lisa generates interview questions"

# Start lisa in background with short intervals
./lisa.sh --poll-interval 5 --idle-sleep 10 &
LISA_PID=$!

# Give lisa time to pick up the idea and generate questions
log_info "Started lisa (PID: $LISA_PID), waiting for interview questions..."

# Wait for prd:interviewing label (up to 90 seconds)
if ! wait_for_label "$IDEA_ID" "prd:interviewing" 90; then
  log_error "Test 1 FAILED: Timeout waiting for prd:interviewing label"
  kill $LISA_PID 2>/dev/null || true
  exit 2
fi

log_info "Idea now has prd:interviewing label"

# Count questions created (assigned to human)
QUESTION_COUNT=$(count_beads "human" "open")
log_info "Questions created: $QUESTION_COUNT"

if [ "$QUESTION_COUNT" -lt 3 ]; then
  log_error "Test 1 FAILED: Expected at least 3 questions, got $QUESTION_COUNT"
  kill $LISA_PID 2>/dev/null || true
  exit 2
fi

log_info "Test 1 PASSED: Lisa generated $QUESTION_COUNT interview questions"

# ============================================================================
# Test 2: Simulate human answering questions
# ============================================================================

log_step "Test 2: Simulate human answering questions"

# Get all question IDs
QUESTION_IDS=$(bd list --assignee=human --status=open --json 2>/dev/null | jq -r '.[].id' 2>/dev/null)

# Answer and close each question
for qid in $QUESTION_IDS; do
  log_info "Answering question: $qid"
  bd comments add "$qid" "Test answer for automated testing" >/dev/null 2>&1 || {
    log_warn "Failed to add comment to $qid"
  }
  bd close "$qid" --reason="Answered for testing" >/dev/null 2>&1 || {
    log_error "Failed to close question $qid"
    kill $LISA_PID 2>/dev/null || true
    exit 2
  }
done

log_info "All questions answered and closed"

# ============================================================================
# Test 3: Wait for PRD generation
# ============================================================================

log_step "Test 3: Lisa generates PRD document"

# Wait for prd:ready label (up to 120 seconds - PRD generation takes time)
log_info "Waiting for PRD generation..."

if ! wait_for_label "$IDEA_ID" "prd:ready" 120; then
  log_error "Test 3 FAILED: Timeout waiting for prd:ready label"
  kill $LISA_PID 2>/dev/null || true
  exit 3
fi

log_info "Idea now has prd:ready label"

# Check PRD file exists
SLUG=$(echo "$IDEA_TITLE" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g' | sed 's/^-//' | sed 's/-$//')
PRD_FILE="prd/PRD-${SLUG}.md"

if [ ! -f "$PRD_FILE" ]; then
  log_error "Test 3 FAILED: PRD file not found at $PRD_FILE"
  kill $LISA_PID 2>/dev/null || true
  exit 3
fi

log_info "PRD file created: $PRD_FILE"
log_info "Test 3 PASSED: PRD document generated"

# ============================================================================
# Test 4: Simulate human approval and task generation
# ============================================================================

log_step "Test 4: Approve PRD and generate tasks for ralph"

# Add approved label
bd label add "$IDEA_ID" "approved" >/dev/null 2>&1 || {
  log_error "Failed to add approved label"
  kill $LISA_PID 2>/dev/null || true
  exit 4
}

log_info "Added 'approved' label to idea"

# Wait for idea to be closed and tasks created (up to 120 seconds)
log_info "Waiting for task generation..."

WAIT_TIME=0
while [ $WAIT_TIME -lt 120 ]; do
  IDEA_STATUS=$(bd show "$IDEA_ID" --json 2>/dev/null | jq -r '.[0].status' 2>/dev/null || echo "open")
  if [ "$IDEA_STATUS" = "closed" ]; then
    break
  fi
  sleep 5
  WAIT_TIME=$((WAIT_TIME + 5))
done

if [ "$IDEA_STATUS" != "closed" ]; then
  log_error "Test 4 FAILED: Timeout waiting for idea to be closed"
  kill $LISA_PID 2>/dev/null || true
  exit 4
fi

log_info "Idea closed"

# Stop lisa
kill $LISA_PID 2>/dev/null || true
wait $LISA_PID 2>/dev/null || true
log_info "Stopped lisa"

# Check tasks created for ralph
RALPH_TASK_COUNT=$(count_beads "ralph" "open")
log_info "Tasks created for ralph: $RALPH_TASK_COUNT"

if [ "$RALPH_TASK_COUNT" -lt 3 ]; then
  log_error "Test 4 FAILED: Expected at least 3 tasks for ralph, got $RALPH_TASK_COUNT"
  exit 4
fi

log_info "Test 4 PASSED: $RALPH_TASK_COUNT tasks created for ralph"

# ============================================================================
# Summary
# ============================================================================

log_step "All Tests Passed!"
echo ""
log_info "Test 1: Lisa generated interview questions ($QUESTION_COUNT questions)"
log_info "Test 2: Human answered all questions"
log_info "Test 3: Lisa generated PRD document ($PRD_FILE)"
log_info "Test 4: Lisa created $RALPH_TASK_COUNT tasks for ralph"
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
