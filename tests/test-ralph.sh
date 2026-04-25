#!/bin/bash
# test-ralph.sh - Test suite for ralph.sh with deterministic project
#
# Usage: ./tests/test-ralph.sh [--dry-run] [--keep]
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
ORTUS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FIXTURES_DIR="$SCRIPT_DIR/fixtures"

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

# Count tasks by status (deprecated: used to filter by assignee)
count_tasks_by_status() {
  local status=$1
  bd list --status="$status" --json 2>/dev/null | jq -r 'length' 2>/dev/null || echo "0"
}

# Parse task definition file and create tasks
# Format: Sections separated by "---", each with "# Task N: Title" header
create_tasks_from_fixture() {
  local fixture_file=$1
  local task_ids=()

  if [ ! -f "$fixture_file" ]; then
    log_error "Fixture file not found: $fixture_file"
    exit 1
  fi

  log_info "Reading task definitions from: $fixture_file"

  # Helper function to create a single task
  create_single_task() {
    local title=$1
    local body=$2
    local task_output
    # Trim leading/trailing whitespace from body
    body=$(echo "$body" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    task_output=$(bd create --title="$title" --type=task --priority=2 --body-file - <<< "$body")
    echo "$task_output" | grep -oP 'Created issue: \K[^ ]+' || true
  }

  # Parse the fixture file into tasks
  local current_title=""
  local current_body=""
  local in_task=false
  local task_count=0

  while IFS= read -r line || [ -n "$line" ]; do
    # Check for task header
    if [[ "$line" =~ ^#\ Task\ [0-9]+:\ (.+)$ ]]; then
      # Save previous task if exists
      if [ "$in_task" = true ] && [ -n "$current_title" ]; then
        task_count=$((task_count + 1))
        local task_id
        task_id=$(create_single_task "$current_title" "$current_body")
        if [ -n "$task_id" ]; then
          task_ids+=("$task_id")
          log_info "Created task $task_count: $task_id - $current_title"
        else
          log_error "Failed to create task: $current_title"
          exit 1
        fi
      fi

      # Start new task
      current_title="${BASH_REMATCH[1]}"
      current_body=""
      in_task=true
    elif [[ "$line" == "---" ]]; then
      # Task separator - save current task
      if [ "$in_task" = true ] && [ -n "$current_title" ]; then
        task_count=$((task_count + 1))
        local task_id
        task_id=$(create_single_task "$current_title" "$current_body")
        if [ -n "$task_id" ]; then
          task_ids+=("$task_id")
          log_info "Created task $task_count: $task_id - $current_title"
        else
          log_error "Failed to create task: $current_title"
          exit 1
        fi
        current_title=""
        current_body=""
        in_task=false
      fi
    elif [ "$in_task" = true ]; then
      # Add line to current task body
      if [ -n "$current_body" ]; then
        current_body="$current_body"$'\n'"$line"
      else
        current_body="$line"
      fi
    fi
  done < "$fixture_file"

  # Handle last task (no trailing ---)
  if [ "$in_task" = true ] && [ -n "$current_title" ]; then
    task_count=$((task_count + 1))
    local task_id
    task_id=$(create_single_task "$current_title" "$current_body")
    if [ -n "$task_id" ]; then
      task_ids+=("$task_id")
      log_info "Created task $task_count: $task_id - $current_title"
    else
      log_error "Failed to create task: $current_title"
      exit 1
    fi
  fi

  # Export task IDs for dependency setup
  TASK_IDS=("${task_ids[@]}")
  log_info "Created $task_count tasks total"
}

# ============================================================================
# Static check: codegraph-absent fallback wording (byte-equivalent)
# ============================================================================
# When neither .codegraph/ nor mcp__codegraph__* tools are present (i.e.
# codegraph_available is false), step 4 of the Ralph prompt must execute the
# original fallback sentence verbatim. This guards the codegraph_unavailable
# branch from accidental wording drift in BOTH prompt files: the source
# ortus/prompts/ralph-prompt.md and the template/ralph-prompt.md.jinja.
#
# The fallback wording lives in the unconditional section of each prompt
# file, so this is a static byte-equivalence check on the prompt source — no
# copier render or shell setup is required. Runs before the heavy copier
# setup so it exercises independently.

log_step "Static check: codegraph-absent fallback wording"

CODEGRAPH_FALLBACK_SENTENCE="Search the codebase first — don't assume not implemented. Use subagents for broad searches."

CODEGRAPH_PROMPT_FILES=(
  "$ORTUS_DIR/ortus/prompts/ralph-prompt.md"
  "$ORTUS_DIR/template/ortus/prompts/ralph-prompt.md.jinja"
)

for prompt_file in "${CODEGRAPH_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    exit 1
  fi
  if ! grep -F -q -- "$CODEGRAPH_FALLBACK_SENTENCE" "$prompt_file"; then
    log_error "codegraph-absent (codegraph_available=false) fallback wording missing in: $prompt_file"
    log_error "Expected verbatim sentence: $CODEGRAPH_FALLBACK_SENTENCE"
    exit 1
  fi
  log_info "Verified codegraph-absent fallback wording in: $(basename "$prompt_file")"
done

log_info "codegraph-absent test PASSED — both prompt files contain byte-equivalent fallback sentence"

# ============================================================================
# Static check: codegraph-present routing wording (byte-equivalent)
# ============================================================================
# When .codegraph/ exists at the project root AND at least one mcp__codegraph__*
# tool is registered (i.e. codegraph_available is true), step 4 of the Ralph
# prompt must route investigation to the codegraph_* tools FIRST as the
# primary investigation surface. This guards the codegraph_available branch
# from accidental wording drift in BOTH prompt files: the source
# ortus/prompts/ralph-prompt.md and the template/ralph-prompt.md.jinja.
#
# The codegraph-present routing wording lives in the unconditional section
# of each prompt file (no Jinja conditional), so this is a static
# byte-equivalence check on the prompt source. We still mock the
# codegraph_available prerequisites (a .codegraph/ directory + a stub MCP
# tool name registration) for environmental fidelity. Runs before the heavy
# copier setup so it exercises independently.

log_step "Static check: codegraph-present routing wording"

# Mock codegraph_available environment: .codegraph/ + stub mcp__codegraph__* tool name
CODEGRAPH_PRESENT_TMPDIR="$(mktemp -d)"
mkdir -p "$CODEGRAPH_PRESENT_TMPDIR/.codegraph"
echo "mcp__codegraph__codegraph_search" > "$CODEGRAPH_PRESENT_TMPDIR/.codegraph/.stub-mcp-tool"
if [ ! -d "$CODEGRAPH_PRESENT_TMPDIR/.codegraph" ] || [ ! -f "$CODEGRAPH_PRESENT_TMPDIR/.codegraph/.stub-mcp-tool" ]; then
  log_error "Failed to set up codegraph_available mock fixture at $CODEGRAPH_PRESENT_TMPDIR"
  rm -rf "$CODEGRAPH_PRESENT_TMPDIR"
  exit 1
fi
log_info "Mocked codegraph_available environment at: $CODEGRAPH_PRESENT_TMPDIR (.codegraph/ + stub mcp__codegraph__codegraph_search)"

# Verbatim anchor: the routing sentence that opens the codegraph-present branch
CODEGRAPH_PRESENT_SENTENCE="If **\`codegraph_available\`**, use these tools as the primary investigation surface"

# The codegraph_* tools step 4 routes to first when codegraph_available
CODEGRAPH_PRIMARY_TOOLS=(
  "codegraph_search"
  "codegraph_callers"
  "codegraph_callees"
  "codegraph_impact"
  "codegraph_node"
)

for prompt_file in "${CODEGRAPH_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    rm -rf "$CODEGRAPH_PRESENT_TMPDIR"
    exit 1
  fi
  if ! grep -F -q -- "$CODEGRAPH_PRESENT_SENTENCE" "$prompt_file"; then
    log_error "codegraph-present (codegraph_available=true) routing wording missing in: $prompt_file"
    log_error "Expected verbatim phrase: $CODEGRAPH_PRESENT_SENTENCE"
    rm -rf "$CODEGRAPH_PRESENT_TMPDIR"
    exit 1
  fi
  for tool in "${CODEGRAPH_PRIMARY_TOOLS[@]}"; do
    if ! grep -F -q -- "\`$tool\`" "$prompt_file"; then
      log_error "Expected primary codegraph tool '\`$tool\`' to be listed in step 4 of: $prompt_file"
      rm -rf "$CODEGRAPH_PRESENT_TMPDIR"
      exit 1
    fi
  done
  log_info "Verified codegraph-present routing wording in: $(basename "$prompt_file")"
done

rm -rf "$CODEGRAPH_PRESENT_TMPDIR"

log_info "codegraph-present test PASSED — both prompt files route to mcp__codegraph__* tools first when codegraph_available"

# ============================================================================
# Test Setup
# ============================================================================

log_step "Setting up test environment"

# Create test directory
mkdir -p "$TEST_DIR"
log_info "Test directory: $TEST_DIR"

# Generate project from template with Python defaults
# Note: The template runs idea.sh at the end which calls Claude for up-sampling.
# We use --skip-tasks to avoid this interactive step, then manually initialize.
log_info "Generating project from template (skipping tasks)..."
copier copy --defaults --trust --skip-tasks \
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
  "$ORTUS_DIR" "$TEST_DIR/testproj"

cd "$TEST_DIR/testproj"
log_info "Project generated at: $(pwd)"

# Manually run the initialization tasks (except idea.sh)
log_info "Running manual initialization..."
chmod +x ortus/ralph.sh ortus/interview.sh ortus/idea.sh ortus/tail.sh 2>/dev/null || true
git init >/dev/null 2>&1
bd init >/dev/null 2>&1
git add -A >/dev/null 2>&1
git commit -m 'Initial commit from Ortus template' >/dev/null 2>&1
log_info "Initialization complete"

# Verify project structure
if [ ! -f "ortus/ralph.sh" ]; then
  log_error "ortus/ralph.sh not found in generated project"
  exit 1
fi

if [ ! -f "ortus/prompts/ralph-prompt.md" ]; then
  log_error "ortus/prompts/ralph-prompt.md not found in generated project"
  exit 1
fi

log_info "Project structure verified"

# ============================================================================
# Create Tasks from Fixture File
# ============================================================================

log_step "Creating test tasks from fixture file"

# Create tasks from the fixture file
create_tasks_from_fixture "$FIXTURES_DIR/calculator-tasks.md"

# Set up dependencies: each task depends on the previous one
if [ ${#TASK_IDS[@]} -ge 2 ]; then
  for ((i=1; i<${#TASK_IDS[@]}; i++)); do
    bd dep add "${TASK_IDS[$i]}" "${TASK_IDS[$((i-1))]}"
    log_info "Added dependency: ${TASK_IDS[$i]} depends on ${TASK_IDS[$((i-1))]}"
  done
fi

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
READY_COUNT=$(bd ready --json 2>/dev/null | jq -r 'length' 2>/dev/null || echo "0")
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
  bd dep tree "${TASK_IDS[-1]}" 2>/dev/null || log_warn "Could not show dependency tree"
  echo ""
  log_info "To run tests manually:"
  echo "  cd $TEST_DIR/testproj"
  echo "  ./ortus/ralph.sh --tasks 1  # Should complete 1 task"
  echo "  bd list                     # Check status"
  echo "  ./ortus/ralph.sh            # Complete remaining tasks"
  exit 0
fi

# ============================================================================
# Test 1: ralph --tasks 1
# ============================================================================

log_step "Test 1: ralph --tasks 1 (should complete exactly 1 task)"

./ortus/ralph.sh --tasks 1 --iterations 15

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

# Verify the src/ directory and greeting.py file was created
if [ ! -f "src/greeting.py" ]; then
  log_error "Test 1 FAILED: src/greeting.py was not created"
  exit 2
fi

# Verify greet function exists
if ! grep -q "def greet" src/greeting.py; then
  log_error "Test 1 FAILED: greet function not found in greeting.py"
  exit 2
fi

log_info "Test 1 PASSED: Exactly 1 task completed, file created correctly"

# ============================================================================
# Test 2: ralph (unlimited - complete remaining)
# ============================================================================

log_step "Test 2: ralph unlimited (should complete all remaining tasks)"

./ortus/ralph.sh --iterations 15

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
if ! grep -q "def greet_person" src/greeting.py; then
  log_error "Test 2 FAILED: greet_person function not found in greeting.py"
  exit 3
fi

if ! grep -q "def farewell" src/greeting.py; then
  log_error "Test 2 FAILED: farewell function not found in greeting.py"
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
