#!/bin/bash
# test-ralph.sh - Test suite for ralph.sh with deterministic project
#
# Usage: ./test-ralph.sh [--dry-run] [--keep]
#
# Options:
#   --dry-run   Set up test project but don't run ralph (for inspection)
#   --keep      Keep the test project after completion (default: cleanup)
#
# Exit codes:
#   0 - All tests passed
#   1 - Test setup failed
#   2 - Test 1 failed (--tasks 1 didn't complete exactly 1 task)
#   3 - Test 2 failed (unlimited ralph didn't complete all remaining tasks)

set -e

# Defaults
DRY_RUN=false
KEEP_PROJECT=false
TEST_DIR="/tmp/ortus-test-$$"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
      head -n 14 "$0" | tail -n +2 | sed 's/^# //' | sed 's/^#//'
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

# Count tasks by status
count_tasks() {
  local status=$1
  bd list --status="$status" --json 2>/dev/null | jq -r 'length' 2>/dev/null || echo "0"
}

# Count tasks assigned to ralph by status
count_ralph_tasks() {
  local status=$1
  bd list --status="$status" --assignee ralph --json 2>/dev/null | jq -r 'length' 2>/dev/null || echo "0"
}

# ============================================================================
# Test Setup
# ============================================================================

log_step "Setting up test environment"

# Create test directory
mkdir -p "$TEST_DIR"
log_info "Test directory: $TEST_DIR"

# Generate project from template with Python defaults
log_info "Generating project from template..."
copier copy --defaults --trust \
  --data project_name=testproj \
  --data project_description="Test project for ralph.sh" \
  --data author_name="Test User" \
  --data author_email="test@example.com" \
  --data github_username=testuser \
  --data language=python \
  --data package_manager=uv \
  --data framework=none \
  --data linter=ruff \
  --data license=MIT \
  "$SCRIPT_DIR" "$TEST_DIR/testproj"

cd "$TEST_DIR/testproj"
log_info "Project generated at: $(pwd)"

# Verify project structure
if [ ! -f "ralph.sh" ]; then
  log_error "ralph.sh not found in generated project"
  exit 1
fi

if [ ! -f "PROMPT.md" ]; then
  log_error "PROMPT.md not found in generated project"
  exit 1
fi

log_info "Project structure verified"

# ============================================================================
# Create Deterministic Tasks
# ============================================================================

log_step "Creating deterministic test tasks"

# Helper to extract task ID from bd create output
extract_task_id() {
  grep -oP 'Created issue: \K[^ ]+' || true
}

# Create simple, verifiable tasks
# Task 1: Create a calculator module with add function
TASK1_OUTPUT=$(bd create --title="Create src/calculator.py with add function" --type=task --assignee=ralph --priority=2 << 'EOF'
Create a new file `src/calculator.py` with a simple add function:

```python
def add(a: int, b: int) -> int:
    """Add two integers and return the result."""
    return a + b
```

Also create the `src/` directory if it doesn't exist.

Acceptance criteria:
- File exists at src/calculator.py
- Contains the add function with correct signature
- Function returns correct result (add(2, 3) == 5)
EOF
)
TASK1_ID=$(echo "$TASK1_OUTPUT" | extract_task_id)
log_info "Created task 1: $TASK1_ID"

# Task 2: Add subtract function
TASK2_OUTPUT=$(bd create --title="Add subtract function to calculator.py" --type=task --assignee=ralph --priority=2 << 'EOF'
Add a subtract function to `src/calculator.py`:

```python
def subtract(a: int, b: int) -> int:
    """Subtract b from a and return the result."""
    return a - b
```

Acceptance criteria:
- subtract function added to src/calculator.py
- Function returns correct result (subtract(5, 3) == 2)
EOF
)
TASK2_ID=$(echo "$TASK2_OUTPUT" | extract_task_id)
log_info "Created task 2: $TASK2_ID"

# Task 3: Add multiply function
TASK3_OUTPUT=$(bd create --title="Add multiply function to calculator.py" --type=task --assignee=ralph --priority=2 << 'EOF'
Add a multiply function to `src/calculator.py`:

```python
def multiply(a: int, b: int) -> int:
    """Multiply two integers and return the result."""
    return a * b
```

Acceptance criteria:
- multiply function added to src/calculator.py
- Function returns correct result (multiply(4, 3) == 12)
EOF
)
TASK3_ID=$(echo "$TASK3_OUTPUT" | extract_task_id)
log_info "Created task 3: $TASK3_ID"

# Set up dependencies: task2 depends on task1, task3 depends on task2
bd dep add "$TASK2_ID" "$TASK1_ID"
log_info "Added dependency: $TASK2_ID depends on $TASK1_ID"

bd dep add "$TASK3_ID" "$TASK2_ID"
log_info "Added dependency: $TASK3_ID depends on $TASK2_ID"

# Commit task setup (if there are changes)
git add -A
git diff --cached --quiet || git commit -m "Add test tasks for ralph.sh"

# Verify initial state
INITIAL_OPEN=$(count_ralph_tasks "open")
INITIAL_CLOSED=$(count_ralph_tasks "closed")
log_info "Initial state: $INITIAL_OPEN open tasks, $INITIAL_CLOSED closed tasks"

if [ "$INITIAL_OPEN" != "3" ]; then
  log_error "Expected 3 open tasks, got $INITIAL_OPEN"
  exit 1
fi

# Check what's ready (should be 1 due to dependencies)
READY_COUNT=$(bd ready --assignee ralph --json 2>/dev/null | jq -r 'length' 2>/dev/null || echo "0")
log_info "Ready tasks (no blockers): $READY_COUNT"

if [ "$READY_COUNT" != "1" ]; then
  log_warn "Expected 1 ready task due to dependencies, got $READY_COUNT"
fi

# ============================================================================
# Dry Run Mode
# ============================================================================

if [ "$DRY_RUN" = true ]; then
  log_step "DRY RUN MODE - Project setup complete"
  log_info "Test project created at: $TEST_DIR/testproj"
  log_info "Tasks created:"
  bd list --status=open
  echo ""
  log_info "Dependency tree:"
  bd dep tree "$TASK3_ID" 2>/dev/null || log_warn "Could not show dependency tree"
  echo ""
  log_info "To run tests manually:"
  echo "  cd $TEST_DIR/testproj"
  echo "  ./ralph.sh --tasks 1  # Should complete 1 task"
  echo "  bd list               # Check status"
  echo "  ./ralph.sh            # Complete remaining tasks"
  exit 0
fi

# ============================================================================
# Test 1: ralph --tasks 1
# ============================================================================

log_step "Test 1: ralph --tasks 1 (should complete exactly 1 task)"

./ralph.sh --tasks 1 --iterations 15

# Verify exactly 1 task completed
CLOSED_AFTER_T1=$(count_ralph_tasks "closed")
OPEN_AFTER_T1=$(count_ralph_tasks "open")

log_info "After Test 1: $CLOSED_AFTER_T1 closed, $OPEN_AFTER_T1 open"

if [ "$CLOSED_AFTER_T1" != "1" ]; then
  log_error "Test 1 FAILED: Expected 1 closed task, got $CLOSED_AFTER_T1"
  exit 2
fi

if [ "$OPEN_AFTER_T1" != "2" ]; then
  log_error "Test 1 FAILED: Expected 2 open tasks, got $OPEN_AFTER_T1"
  exit 2
fi

# Verify the file was created
if [ ! -f "src/calculator.py" ]; then
  log_error "Test 1 FAILED: src/calculator.py was not created"
  exit 2
fi

# Verify add function exists
if ! grep -q "def add" src/calculator.py; then
  log_error "Test 1 FAILED: add function not found in calculator.py"
  exit 2
fi

log_info "Test 1 PASSED: Exactly 1 task completed, file created correctly"

# ============================================================================
# Test 2: ralph (unlimited - complete remaining)
# ============================================================================

log_step "Test 2: ralph unlimited (should complete all remaining tasks)"

./ralph.sh --iterations 15

# Verify all tasks completed
CLOSED_AFTER_T2=$(count_ralph_tasks "closed")
OPEN_AFTER_T2=$(count_ralph_tasks "open")

log_info "After Test 2: $CLOSED_AFTER_T2 closed, $OPEN_AFTER_T2 open"

if [ "$CLOSED_AFTER_T2" != "3" ]; then
  log_error "Test 2 FAILED: Expected 3 closed tasks, got $CLOSED_AFTER_T2"
  exit 3
fi

if [ "$OPEN_AFTER_T2" != "0" ]; then
  log_error "Test 2 FAILED: Expected 0 open tasks, got $OPEN_AFTER_T2"
  exit 3
fi

# Verify all functions exist
if ! grep -q "def subtract" src/calculator.py; then
  log_error "Test 2 FAILED: subtract function not found in calculator.py"
  exit 3
fi

if ! grep -q "def multiply" src/calculator.py; then
  log_error "Test 2 FAILED: multiply function not found in calculator.py"
  exit 3
fi

log_info "Test 2 PASSED: All remaining tasks completed"

# ============================================================================
# Summary
# ============================================================================

log_step "All Tests Passed!"
echo ""
log_info "Test 1: --tasks 1 completed exactly 1 task ✓"
log_info "Test 2: unlimited ralph completed remaining 2 tasks ✓"
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
