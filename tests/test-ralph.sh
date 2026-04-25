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
# Static check: step 6.5 freshness hook is non-blocking on codegraph sync failure
# ============================================================================
# Locks FR-005: a failing `codegraph sync` (missing binary or non-zero exit)
# must not break the loop. Step 6.5 must contain language that explicitly
# tells Ralph to ignore the exit code / proceed regardless, and the prompt
# must continue uninterrupted from 6.5 → 7 (Log) → 8 (Close) → 9 (Commit & Push).
#
# Simulates a failing `codegraph sync` by PATH-shadowing the codegraph binary
# with a stub returning exit 1, then asserts the rendered step 6.5 still
# routes Ralph to Commit. Static byte-check on the prompt source — no copier
# render or shell ralph invocation required.

log_step "Static check: step 6.5 non-blocking on codegraph sync failure"

# Set up a failing codegraph stub in a PATH-shadow tmpdir
CODEGRAPH_FAIL_TMPDIR="$(mktemp -d)"
cat > "$CODEGRAPH_FAIL_TMPDIR/codegraph" <<'STUB'
#!/bin/bash
echo "stub: codegraph sync failed (simulated)" >&2
exit 1
STUB
chmod +x "$CODEGRAPH_FAIL_TMPDIR/codegraph"

# Verify the stub is wired and actually fails (proves the failure mode is real)
if ! PATH="$CODEGRAPH_FAIL_TMPDIR:$PATH" command -v codegraph >/dev/null; then
  log_error "Failed to PATH-shadow codegraph stub at $CODEGRAPH_FAIL_TMPDIR"
  rm -rf "$CODEGRAPH_FAIL_TMPDIR"
  exit 1
fi
if PATH="$CODEGRAPH_FAIL_TMPDIR:$PATH" codegraph sync >/dev/null 2>&1; then
  log_error "Stub codegraph sync returned 0; expected non-zero"
  rm -rf "$CODEGRAPH_FAIL_TMPDIR"
  exit 1
fi
log_info "Simulated failing codegraph sync at: $CODEGRAPH_FAIL_TMPDIR (exits 1)"

# Anchor: the step 6.5 sentence opens with this exact bold marker
CODEGRAPH_STEP65_ANCHOR="**6.5. Refresh the index (best-effort).**"

# Phrases that explicitly state non-blocking semantics. At least one must
# appear in step 6.5 of each prompt file.
CODEGRAPH_NONBLOCKING_PHRASES=(
  "Ignore the exit code"
  "Do not block the loop"
  "best-effort"
)

# Steps that must follow 6.5 — proves the prompt continues to Commit
# without aborting on a failing sync.
CODEGRAPH_POST65_STEPS=(
  '^7\. \*\*Log\*\*'
  '^8\. \*\*Close\*\*'
  '^9\. \*\*Commit & Push\*\*'
)

for prompt_file in "${CODEGRAPH_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    rm -rf "$CODEGRAPH_FAIL_TMPDIR"
    exit 1
  fi

  # Extract the step 6.5 line
  step65_line=$(grep -F -- "$CODEGRAPH_STEP65_ANCHOR" "$prompt_file" || true)
  if [ -z "$step65_line" ]; then
    log_error "Step 6.5 anchor missing in: $prompt_file"
    log_error "Expected anchor: $CODEGRAPH_STEP65_ANCHOR"
    rm -rf "$CODEGRAPH_FAIL_TMPDIR"
    exit 1
  fi

  # Assert non-blocking language is present in step 6.5
  found_nonblocking=0
  for phrase in "${CODEGRAPH_NONBLOCKING_PHRASES[@]}"; do
    if grep -F -q -- "$phrase" <<< "$step65_line"; then
      found_nonblocking=$((found_nonblocking + 1))
    fi
  done
  if [ "$found_nonblocking" -lt 1 ]; then
    log_error "Step 6.5 in $prompt_file lacks non-blocking language"
    log_error "Expected at least one of: ${CODEGRAPH_NONBLOCKING_PHRASES[*]}"
    rm -rf "$CODEGRAPH_FAIL_TMPDIR"
    exit 1
  fi

  # Assert step 6.5 is followed by Log → Close → Commit & Push, in order, and
  # all appear AFTER step 6.5 (so the prompt routes Ralph to Commit, not Exit).
  step65_lineno=$(grep -F -n -- "$CODEGRAPH_STEP65_ANCHOR" "$prompt_file" | head -n 1 | cut -d: -f1)
  prev_lineno="$step65_lineno"
  for step_pattern in "${CODEGRAPH_POST65_STEPS[@]}"; do
    step_lineno=$(grep -E -n -- "$step_pattern" "$prompt_file" | head -n 1 | cut -d: -f1)
    if [ -z "$step_lineno" ]; then
      log_error "Post-6.5 step pattern '$step_pattern' missing in: $prompt_file"
      rm -rf "$CODEGRAPH_FAIL_TMPDIR"
      exit 1
    fi
    if [ "$step_lineno" -le "$prev_lineno" ]; then
      log_error "Post-6.5 step '$step_pattern' (line $step_lineno) does not follow previous step (line $prev_lineno) in: $prompt_file"
      rm -rf "$CODEGRAPH_FAIL_TMPDIR"
      exit 1
    fi
    prev_lineno="$step_lineno"
  done

  log_info "Verified non-blocking step 6.5 → 7 → 8 → 9 chain in: $(basename "$prompt_file")"
done

rm -rf "$CODEGRAPH_FAIL_TMPDIR"

log_info "codegraph-sync-failure test PASSED — both prompt files instruct Ralph to proceed to Commit despite a failing codegraph sync"

# ============================================================================
# Static check: NFR-101 step-1 byte-equivalent baseline when CodeGraph off
# ============================================================================
# When codegraph_available is false, step 1's orient context must reduce to
# the bd list pull verbatim — no activity-read block, no CodeGraph v1 block
# reuse, no log lines, no warnings (NFR-101). The FR-401..403 (activity-read)
# and FR-404 (block reuse) sub-paragraphs of step 1 must explicitly gate on
# codegraph_available AND emit nothing when off ("skip silently" — not
# log/warn/placeholder). Mocks codegraph-absent by removing .codegraph/ from
# a tmpdir fixture; the gating contract lives in the prompt source itself, so
# this is a static byte-check on both prompt files. Narrowed to the step-1
# region so it cannot false-pass on step 6.5's gating language.

log_step "Static check: NFR-101 step-1 byte-equivalent baseline when CodeGraph off"

# Mock codegraph-absent fixture: tmpdir with NO .codegraph/ and NO
# mcp__codegraph__* stubs (i.e., codegraph_available is false).
NFR101_ABSENT_TMPDIR="$(mktemp -d)"
if [ -d "$NFR101_ABSENT_TMPDIR/.codegraph" ]; then
  log_error "Failed to mock codegraph-absent fixture (unexpected .codegraph/ present)"
  rm -rf "$NFR101_ABSENT_TMPDIR"
  exit 1
fi
log_info "Mocked codegraph-absent environment at: $NFR101_ABSENT_TMPDIR (no .codegraph/, no mcp__codegraph__* stubs)"

# The verbatim FR-401 bd-list invocation that must remain in step 1 unchanged
NFR101_BD_LIST_INVOCATION="bd list --sort updated --all --limit 10 --json | jq -r '.[].id' | xargs bd show --json"

# Gating phrases — each FR-401..403 (activity-read) and FR-404 (block reuse)
# sub-paragraph in step 1 must contain language that skips silently when off.
NFR101_GATING_PHRASES=(
  'When `codegraph_available`'        # Activity-read paragraph opens with this gate
  'Gated on `codegraph_available`'    # FR-404 paragraph closes with this gate
  "skip silently"                     # Silent fallback (no warnings, no extra blocks)
)

for prompt_file in "${CODEGRAPH_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    rm -rf "$NFR101_ABSENT_TMPDIR"
    exit 1
  fi

  # Extract the step-1 region (between "1. **Orient**" and "2. **Select**")
  step1_region=$(awk '/^1\. \*\*Orient\*\*/{flag=1} flag {print} /^2\. \*\*Select\*\*/{flag=0}' "$prompt_file")
  if [ -z "$step1_region" ]; then
    log_error "Could not extract step-1 region from: $prompt_file"
    rm -rf "$NFR101_ABSENT_TMPDIR"
    exit 1
  fi

  # Assert FR-401 bd-list invocation preserved verbatim within step 1
  if ! grep -F -q -- "$NFR101_BD_LIST_INVOCATION" <<< "$step1_region"; then
    log_error "NFR-101 bd-list invocation missing/altered in step 1 of: $prompt_file"
    log_error "Expected: $NFR101_BD_LIST_INVOCATION"
    rm -rf "$NFR101_ABSENT_TMPDIR"
    exit 1
  fi

  # Assert gating phrases present within step 1
  for phrase in "${NFR101_GATING_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" <<< "$step1_region"; then
      log_error "NFR-101 gating phrase missing in step 1 of: $prompt_file"
      log_error "Expected verbatim: $phrase"
      rm -rf "$NFR101_ABSENT_TMPDIR"
      exit 1
    fi
  done

  log_info "Verified NFR-101 baseline gating in step 1 of: $(basename "$prompt_file")"
done

rm -rf "$NFR101_ABSENT_TMPDIR"

log_info "NFR-101 baseline test PASSED — both prompt files gate FR-401..404 sub-paragraphs in step 1 on codegraph_available with silent skip"

# ============================================================================
# Static check: FR-401+402 presence-when-on (bd-list preserved + activity surfaced)
# ============================================================================
# When codegraph_available is true, step 1 of the Ralph prompt must (a)
# preserve the existing FR-401 bd-list comment-history pull verbatim AND
# (b) additionally emit the FR-402 git-derived CodeGraph activity-read
# block. This guards both halves of the orient context from accidental
# wording drift in BOTH prompt files: the source ortus/prompts/ralph-prompt.md
# and the template/ralph-prompt.md.jinja.
#
# Mocks the codegraph_available environment (.codegraph/ + a stub
# mcp__codegraph__codegraph_files tool name) for environmental fidelity,
# parallel to existing mocks above. The FR-402 activity-read contract lives
# in the prompt source itself (it is instruction to the loop, not runnable
# code), so this is a static byte-check on the prompt source. Narrowed to
# the step-1 region so it cannot false-pass on later sections.

log_step "Static check: FR-401+402 presence-when-on (bd-list preserved + CodeGraph activity surfaced)"

# Mock codegraph_available environment: .codegraph/ + stub mcp__codegraph__codegraph_files
FR401_402_TMPDIR="$(mktemp -d)"
mkdir -p "$FR401_402_TMPDIR/.codegraph"
echo "mcp__codegraph__codegraph_files" > "$FR401_402_TMPDIR/.codegraph/.stub-mcp-tool"
if [ ! -d "$FR401_402_TMPDIR/.codegraph" ] || [ ! -f "$FR401_402_TMPDIR/.codegraph/.stub-mcp-tool" ]; then
  log_error "Failed to set up codegraph_available mock fixture at $FR401_402_TMPDIR"
  rm -rf "$FR401_402_TMPDIR"
  exit 1
fi
log_info "Mocked codegraph_available environment at: $FR401_402_TMPDIR (.codegraph/ + stub mcp__codegraph__codegraph_files)"

# FR-401: the bd-list comment-history pull invocation that must remain in step 1 verbatim
FR401_BD_LIST_INVOCATION="bd list --sort updated --all --limit 10 --json | jq -r '.[].id' | xargs bd show --json"

# FR-402: anchors for the git-derived CodeGraph activity-read sub-paragraph
# that must additionally appear in step 1 alongside the bd-list pull when
# codegraph_available. Together they prove the orient context contains BOTH
# the bd-list comment-history block AND the new CodeGraph activity block.
FR402_ACTIVITY_PHRASES=(
  '**Activity read (FR-401..403).**'    # Sub-paragraph header
  'surface recent CodeGraph activity'   # Semantic anchor
  'git log -20 --name-only | sort -u'   # File-list derivation invocation
  '`codegraph_files`'                   # Primary enrichment tool
)

for prompt_file in "${CODEGRAPH_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    rm -rf "$FR401_402_TMPDIR"
    exit 1
  fi

  # Extract the step-1 region (between "1. **Orient**" and "2. **Select**")
  step1_region=$(awk '/^1\. \*\*Orient\*\*/{flag=1} flag {print} /^2\. \*\*Select\*\*/{flag=0}' "$prompt_file")
  if [ -z "$step1_region" ]; then
    log_error "Could not extract step-1 region from: $prompt_file"
    rm -rf "$FR401_402_TMPDIR"
    exit 1
  fi

  # FR-401: assert bd-list invocation preserved verbatim within step 1
  if ! grep -F -q -- "$FR401_BD_LIST_INVOCATION" <<< "$step1_region"; then
    log_error "FR-401 bd-list invocation missing/altered in step 1 of: $prompt_file"
    log_error "Expected verbatim: $FR401_BD_LIST_INVOCATION"
    rm -rf "$FR401_402_TMPDIR"
    exit 1
  fi

  # FR-402: assert each activity-read anchor is present within step 1
  for phrase in "${FR402_ACTIVITY_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" <<< "$step1_region"; then
      log_error "FR-402 activity-read phrase missing in step 1 of: $prompt_file"
      log_error "Expected verbatim: $phrase"
      rm -rf "$FR401_402_TMPDIR"
      exit 1
    fi
  done

  log_info "Verified FR-401 bd-list AND FR-402 activity-read in step 1 of: $(basename "$prompt_file")"
done

rm -rf "$FR401_402_TMPDIR"

log_info "FR-401+402 presence-when-on test PASSED — both prompt files preserve bd-list verbatim AND surface CodeGraph activity in step 1"

# ============================================================================
# Static check: FR-403 activity-read cap (30 files / 50 symbols, no error)
# ============================================================================
# When codegraph_available is true and the recent-commits file list exceeds
# the cap, step 1's activity-read sub-paragraph must (a) declare a 30-unique-
# files cap, (b) declare a 50-symbols cap, and (c) instruct the loop to
# truncate beyond the cap rather than erroring (FR-403). This guards the
# cap contract from accidental wording drift in BOTH prompt files.
#
# Mocks the fixture conditions described in the issue's acceptance criteria:
# 50 files in recent commits, 100 symbols total via a stub codegraph_files
# response. The cap contract lives in the prompt source itself (it is
# instruction to the loop, not runnable code), so this is a static
# byte-check on both prompt files. Narrowed to the step-1 region so it
# cannot false-pass on later sections of the prompt.

log_step "Static check: FR-403 activity-read cap (30 files / 50 symbols, no error)"

# Mock the >30-file / >50-symbol fixture: a tmpdir with 50 dummy files and
# a stub codegraph_files response file claiming 100 symbols total. This
# matches the issue's acceptance condition ("Fixture: 50 files in recent
# commits, 100 symbols total via mocked codegraph_files response") for
# environmental fidelity, parallel to existing mocks above.
FR403_FIXTURE_TMPDIR="$(mktemp -d)"
mkdir -p "$FR403_FIXTURE_TMPDIR/.codegraph"
echo "mcp__codegraph__codegraph_files" > "$FR403_FIXTURE_TMPDIR/.codegraph/.stub-mcp-tool"
for i in $(seq 1 50); do
  : > "$FR403_FIXTURE_TMPDIR/file-$i.txt"
done
fr403_file_count=$(find "$FR403_FIXTURE_TMPDIR" -maxdepth 1 -type f -name 'file-*.txt' | wc -l)
if [ "$fr403_file_count" -ne 50 ]; then
  log_error "Failed to mock 50-file fixture (got $fr403_file_count)"
  rm -rf "$FR403_FIXTURE_TMPDIR"
  exit 1
fi
# Stub codegraph_files response claiming 100 symbols across the 50 files
{
  echo "files: 50"
  echo "symbols: 100"
} > "$FR403_FIXTURE_TMPDIR/.codegraph/.stub-codegraph-files-response"
log_info "Mocked FR-403 over-cap fixture at: $FR403_FIXTURE_TMPDIR (50 files, stub response: 100 symbols)"

# Cap phrases that must appear verbatim in step 1 of each prompt file.
# (a) and (b) declare the caps; (c) declares truncate-rather-than-error.
FR403_CAP_PHRASES=(
  '**30 unique files**'                         # File cap
  '**50 symbols**'                              # Symbol cap
  'truncate beyond the cap rather than erroring' # No-error truncation semantic
)

for prompt_file in "${CODEGRAPH_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    rm -rf "$FR403_FIXTURE_TMPDIR"
    exit 1
  fi

  # Extract the step-1 region (between "1. **Orient**" and "2. **Select**")
  step1_region=$(awk '/^1\. \*\*Orient\*\*/{flag=1} flag {print} /^2\. \*\*Select\*\*/{flag=0}' "$prompt_file")
  if [ -z "$step1_region" ]; then
    log_error "Could not extract step-1 region from: $prompt_file"
    rm -rf "$FR403_FIXTURE_TMPDIR"
    exit 1
  fi

  # Assert each cap phrase is present within step 1
  for phrase in "${FR403_CAP_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" <<< "$step1_region"; then
      log_error "FR-403 cap phrase missing in step 1 of: $prompt_file"
      log_error "Expected verbatim: $phrase"
      rm -rf "$FR403_FIXTURE_TMPDIR"
      exit 1
    fi
  done

  log_info "Verified FR-403 cap (30 files / 50 symbols / truncate-not-error) in step 1 of: $(basename "$prompt_file")"
done

rm -rf "$FR403_FIXTURE_TMPDIR"

log_info "FR-403 cap test PASSED — both prompt files declare 30-file / 50-symbol caps and truncate beyond the cap rather than erroring"

# ============================================================================
# Static check: FR-503 auto-flip forbidden (model discretion preserved)
# ============================================================================
# When codegraph_available is true and step 5 appends a graph-derived missing
# entry to the Plan JSON, the model-judged has_enough_info value MUST NOT
# automatically flip to false on graph signal alone (FR-503). The flip stays
# at the model's discretion, since the symbol may legitimately be new code
# introduced by this very issue. This guards the FR-503 anti-auto-flip
# contract from accidental wording drift in BOTH prompt files: the source
# ortus/prompts/ralph-prompt.md and the template/ralph-prompt.md.jinja.
#
# Mocks the fixture conditions described in the issue's acceptance criteria:
# a Plan JSON with has_enough_info=true and a single graph-derived missing
# entry (per Appendix G), expecting has_enough_info to remain true after
# step-5 enrichment. The contract lives in the prompt source itself (it is
# instruction to the loop, not runnable code), so this is a static
# byte-check on both prompt files. Narrowed to the Issue Plan region so it
# cannot false-pass on the scheduler block that follows ("If has_enough_info
# is false, post a bd comment ... and emit BLOCKED").

log_step "Static check: FR-503 auto-flip forbidden (has_enough_info preserved on graph signal)"

# Mock codegraph_available environment: .codegraph/ + stub mcp__codegraph__codegraph_search
# (the tool the FR-501..503 sub-paragraph invokes per extracted reference).
FR503_FIXTURE_TMPDIR="$(mktemp -d)"
mkdir -p "$FR503_FIXTURE_TMPDIR/.codegraph"
echo "mcp__codegraph__codegraph_search" > "$FR503_FIXTURE_TMPDIR/.codegraph/.stub-mcp-tool"
if [ ! -d "$FR503_FIXTURE_TMPDIR/.codegraph" ] || [ ! -f "$FR503_FIXTURE_TMPDIR/.codegraph/.stub-mcp-tool" ]; then
  log_error "Failed to set up codegraph_available mock fixture at $FR503_FIXTURE_TMPDIR"
  rm -rf "$FR503_FIXTURE_TMPDIR"
  exit 1
fi
# Mock Plan JSON fixture: has_enough_info=true with one graph-derived missing
# entry per Appendix G. This represents the post-enrichment state the FR-503
# anti-auto-flip rule guards — the scenario in which a naive scheduler might
# incorrectly flip has_enough_info to false purely because the graph could
# not resolve a referenced symbol. The fixture is referenced by the test
# log lines for environmental fidelity, parallel to the FR-403 fixture above.
cat > "$FR503_FIXTURE_TMPDIR/.codegraph/.stub-plan-json" <<'FR503_PLAN_JSON'
{
  "has_enough_info": true,
  "missing": ["References NoSuchClass.foo in body; no such symbol in graph. Confirm during Investigate or flag as new code."],
  "implementation_steps": ["..."],
  "verification_steps": ["..."],
  "closure_reason": "..."
}
FR503_PLAN_JSON
if [ ! -s "$FR503_FIXTURE_TMPDIR/.codegraph/.stub-plan-json" ]; then
  log_error "Failed to mock FR-503 Plan JSON fixture at $FR503_FIXTURE_TMPDIR"
  rm -rf "$FR503_FIXTURE_TMPDIR"
  exit 1
fi
log_info "Mocked FR-503 fixture at: $FR503_FIXTURE_TMPDIR (Plan JSON: has_enough_info=true + 1 graph-derived missing entry)"

# FR-503 anchors that must appear verbatim in the Issue Plan region of each
# prompt file. Together they prove the prompt forbids auto-flipping
# has_enough_info on graph signal alone AND explicitly attributes the flip
# decision to the model's discretion (since a referenced symbol may
# legitimately be new code introduced by this very issue).
FR503_ANTI_AUTOFLIP_PHRASES=(
  '**Per FR-503, a graph-derived'                                 # FR-503 anchor that opens the rule
  'does NOT automatically flip'                                   # Anti-auto-flip imperative
  "flip stays at the model's discretion"                          # Discretion clause
)

for prompt_file in "${CODEGRAPH_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    rm -rf "$FR503_FIXTURE_TMPDIR"
    exit 1
  fi

  # Extract the Issue Plan region (between "## Issue Plan" and "## Subagent Strategy")
  # so the check cannot false-pass on the scheduler block that handles a
  # has_enough_info=false plan further down in the same section.
  plan_region=$(awk '/^## Issue Plan/{flag=1} flag {print} /^## Subagent Strategy/{flag=0}' "$prompt_file")
  if [ -z "$plan_region" ]; then
    log_error "Could not extract Issue Plan region from: $prompt_file"
    rm -rf "$FR503_FIXTURE_TMPDIR"
    exit 1
  fi

  # Assert each anti-auto-flip phrase is present within the Issue Plan region
  for phrase in "${FR503_ANTI_AUTOFLIP_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" <<< "$plan_region"; then
      log_error "FR-503 anti-auto-flip phrase missing in Issue Plan section of: $prompt_file"
      log_error "Expected verbatim: $phrase"
      rm -rf "$FR503_FIXTURE_TMPDIR"
      exit 1
    fi
  done

  log_info "Verified FR-503 anti-auto-flip in Issue Plan section of: $(basename "$prompt_file")"
done

rm -rf "$FR503_FIXTURE_TMPDIR"

log_info "FR-503 auto-flip-forbidden test PASSED — both prompt files explicitly forbid auto-flipping has_enough_info on graph signal alone (model discretion preserved)"

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
INITIAL_OPEN=$(count_tasks "open")
INITIAL_CLOSED=$(count_tasks "closed")
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
CLOSED_AFTER_T1=$(count_tasks "closed")
OPEN_AFTER_T1=$(count_tasks "open")

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
CLOSED_AFTER_T2=$(count_tasks "closed")
OPEN_AFTER_T2=$(count_tasks "open")

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
