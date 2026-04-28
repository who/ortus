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
# Static check: FR-404 prior bd comment's CodeGraph v1 block surfaces in step 1
# ============================================================================
# When codegraph_available is true and a recent bd comment (returned by the
# `bd show --json` invocation in step 1) contains a **CodeGraph v1** block
# (the FR-101 schema), step 1's CodeGraph-block-reuse sub-paragraph must
# (a) scan those comments for **CodeGraph v1** headers, (b) parse the
# `modified:` line, (c) surface the `symbol@file:line` entries directly into
# the orient context, (d) tolerate unrecognized future schema versions
# (e.g., **CodeGraph v2**) by silently skipping rather than erroring, and
# (e) gate on codegraph_available with silent skip when off (FR-404).
# This guards the FR-404 block-reuse contract from accidental wording drift
# in BOTH prompt files: the source ortus/prompts/ralph-prompt.md and the
# template/ralph-prompt.md.jinja.
#
# Mocks the fixture conditions described in the issue's acceptance criteria:
# a bd-show JSON response containing a comment with **CodeGraph v1**
# modified-symbol entries. The contract lives in the prompt source itself
# (it is instruction to the loop, not runnable code), so this is a static
# byte-check on both prompt files. Narrowed to the step-1 region so it
# cannot false-pass on the FR-101 step-7 emission section that uses similar
# **CodeGraph v1** language.

log_step "Static check: FR-404 prior bd comment's CodeGraph v1 block surfaces in orient context"

# Mock codegraph_available environment: .codegraph/ + stub mcp__codegraph__codegraph_search
# (the family of tools the FR-404 sub-paragraph implies are available).
FR404_FIXTURE_TMPDIR="$(mktemp -d)"
mkdir -p "$FR404_FIXTURE_TMPDIR/.codegraph"
echo "mcp__codegraph__codegraph_search" > "$FR404_FIXTURE_TMPDIR/.codegraph/.stub-mcp-tool"
if [ ! -d "$FR404_FIXTURE_TMPDIR/.codegraph" ] || [ ! -f "$FR404_FIXTURE_TMPDIR/.codegraph/.stub-mcp-tool" ]; then
  log_error "Failed to set up codegraph_available mock fixture at $FR404_FIXTURE_TMPDIR"
  rm -rf "$FR404_FIXTURE_TMPDIR"
  exit 1
fi
# Mock bd-show JSON fixture: a comment containing a **CodeGraph v1** block
# with two modified-symbol entries per Appendix C. This represents the
# orient-time input the FR-404 block-reuse rule consumes — the `modified:`
# line's `symbol@file:line` entries that step 1 must surface directly.
# The fixture is referenced by the test log lines for environmental
# fidelity, parallel to the FR-401+402 and FR-403 fixtures above.
cat > "$FR404_FIXTURE_TMPDIR/.codegraph/.stub-bd-show-json" <<'FR404_BD_SHOW_JSON'
[
  {
    "id": "ortus-fixture",
    "comments": [
      {
        "body": "**Changes**:\n- Refactored auth\n\n**Verification**: tests pass\n\n**CodeGraph v1**:\nmodified: AuthMiddleware.validate@src/middleware/auth.ts:42 (3 callers, 1 cross-module), TokenStore.refresh@src/lib/token.ts:18 (1 caller, 0 cross-module)\nnew: TokenStore@src/lib/token.ts:7 (class)\noos_callers: ApiRouter.login@src/api/auth/login.ts:23 -> AuthMiddleware.validate"
      }
    ]
  }
]
FR404_BD_SHOW_JSON
if [ ! -s "$FR404_FIXTURE_TMPDIR/.codegraph/.stub-bd-show-json" ]; then
  log_error "Failed to mock FR-404 bd-show JSON fixture at $FR404_FIXTURE_TMPDIR"
  rm -rf "$FR404_FIXTURE_TMPDIR"
  exit 1
fi
log_info "Mocked FR-404 fixture at: $FR404_FIXTURE_TMPDIR (bd-show JSON: comment with **CodeGraph v1** block, 2 modified-symbol entries)"

# FR-404 anchors that must appear verbatim in step 1 of each prompt file.
# Together they prove (a) the section is explicitly labeled FR-404 block
# reuse, (b) the parser scans recent bd comments for **CodeGraph v1**
# headers, (c) the derivation parses the `modified:` line, (d) the output
# surfaces `symbol@file:line` entries directly, (e) the parser is tolerant
# of unrecognized future schema versions, and (f) the contract is gated on
# codegraph_available with silent skip when off.
FR404_BLOCK_REUSE_PHRASES=(
  '**CodeGraph block reuse (FR-404).**'                             # Section header anchor
  '`**CodeGraph v1**` headers'                                      # Schema header the parser scans for
  'parse the `modified:` line'                                      # Derivation contract
  'surface the `symbol@file:line` entries into the orient context'  # Output format
  'silently skip blocks whose schema version is unrecognized'       # FR-404 tolerance (Appendix Q4)
  'Gated on `codegraph_available`'                                  # Gate
  'skip silently when CodeGraph isn'                                # Silent fallback when off (NFR-101)
)

for prompt_file in "${CODEGRAPH_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    rm -rf "$FR404_FIXTURE_TMPDIR"
    exit 1
  fi

  # Extract the step-1 region (between "1. **Orient**" and "2. **Select**")
  # so the check cannot false-pass on the FR-101 step-7 emission section
  # that uses similar **CodeGraph v1** language later in the prompt.
  step1_region=$(awk '/^1\. \*\*Orient\*\*/{flag=1} flag {print} /^2\. \*\*Select\*\*/{flag=0}' "$prompt_file")
  if [ -z "$step1_region" ]; then
    log_error "Could not extract step-1 region from: $prompt_file"
    rm -rf "$FR404_FIXTURE_TMPDIR"
    exit 1
  fi

  # Assert each FR-404 block-reuse phrase is present within step 1
  for phrase in "${FR404_BLOCK_REUSE_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" <<< "$step1_region"; then
      log_error "FR-404 block-reuse phrase missing in step 1 of: $prompt_file"
      log_error "Expected verbatim: $phrase"
      rm -rf "$FR404_FIXTURE_TMPDIR"
      exit 1
    fi
  done

  log_info "Verified FR-404 block reuse (header + scan + parse modified: + surface symbol@file:line + tolerant + gated) in step 1 of: $(basename "$prompt_file")"
done

rm -rf "$FR404_FIXTURE_TMPDIR"

log_info "FR-404 block-reuse test PASSED — both prompt files instruct Ralph to surface **CodeGraph v1** modified-symbol entries from recent bd comments directly into the orient context (compounding-memory payoff of FR-102's parseable schema)"

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
# Static check: FR-501+502 phantom-symbol reference produces Appendix G missing entry
# ============================================================================
# When codegraph_available is true and step 5's Reference check encounters a
# code-shaped reference that does NOT resolve in the graph (e.g.,
# `NoSuchClass.foo()`), the prompt must instruct Ralph to append exactly one
# entry to the Plan's `missing` array per Appendix G in this verbatim form:
#
#   References <symbol> in <field>; no such symbol in graph. Confirm during
#   Investigate or flag as new code.
#
# Conversely, when a reference DOES resolve (e.g., a real graph symbol),
# the prompt must NOT produce a graph-derived missing entry — guarded by the
# "For every unresolved reference" gating clause. Together these guard the
# FR-501 extraction + FR-502 entry-form contract from accidental wording
# drift in BOTH prompt files: the source ortus/prompts/ralph-prompt.md and
# the template/ralph-prompt.md.jinja.
#
# Mocks the fixture conditions described in the issue's acceptance criteria:
# a phantom-symbol issue body containing `NoSuchClass.foo()` paired with a
# stub codegraph_search response returning empty, AND a resolved-symbol
# variant returning a hit. The contract lives in the prompt source itself
# (it is instruction to the loop, not runnable code), so this is a static
# byte-check on both prompt files. Narrowed to the Issue Plan region so it
# cannot false-pass on unrelated sections.

log_step "Static check: FR-501+502 phantom-symbol reference produces Appendix G missing entry"

# Mock codegraph_available environment: .codegraph/ + stub mcp__codegraph__codegraph_search
# (the tool the FR-501..503 sub-paragraph invokes per extracted reference).
FR501_502_FIXTURE_TMPDIR="$(mktemp -d)"
mkdir -p "$FR501_502_FIXTURE_TMPDIR/.codegraph"
echo "mcp__codegraph__codegraph_search" > "$FR501_502_FIXTURE_TMPDIR/.codegraph/.stub-mcp-tool"
if [ ! -d "$FR501_502_FIXTURE_TMPDIR/.codegraph" ] || [ ! -f "$FR501_502_FIXTURE_TMPDIR/.codegraph/.stub-mcp-tool" ]; then
  log_error "Failed to set up codegraph_available mock fixture at $FR501_502_FIXTURE_TMPDIR"
  rm -rf "$FR501_502_FIXTURE_TMPDIR"
  exit 1
fi

# Phantom-symbol fixture: an issue body referencing NoSuchClass.foo() — a
# code-shaped reference (dotted method) absent from the graph. After step 5
# runs, Plan.missing must contain the Appendix G verbatim entry citing
# NoSuchClass.foo and the field name (body or acceptance_criteria).
cat > "$FR501_502_FIXTURE_TMPDIR/.codegraph/.stub-phantom-issue-body" <<'PHANTOM_ISSUE_BODY'
Fix the timeout handling in NoSuchClass.foo() so the cache invalidation
does not fail when the upstream timeout exceeds 30 seconds.
PHANTOM_ISSUE_BODY
# Stub codegraph_search response: empty (NoSuchClass.foo absent from graph)
echo '{"results": []}' > "$FR501_502_FIXTURE_TMPDIR/.codegraph/.stub-codegraph-search-empty"

# Resolved-symbol variant: same fixture, but with a stub codegraph_search
# response returning a hit. Per FR-502's "For every unresolved reference"
# gating, this case must NOT produce a graph-derived missing entry.
echo '{"results": [{"symbol": "AuthMiddleware.validate", "file": "src/middleware/auth.ts:42"}]}' \
  > "$FR501_502_FIXTURE_TMPDIR/.codegraph/.stub-codegraph-search-hit"

if [ ! -s "$FR501_502_FIXTURE_TMPDIR/.codegraph/.stub-phantom-issue-body" ] \
   || [ ! -s "$FR501_502_FIXTURE_TMPDIR/.codegraph/.stub-codegraph-search-empty" ] \
   || [ ! -s "$FR501_502_FIXTURE_TMPDIR/.codegraph/.stub-codegraph-search-hit" ]; then
  log_error "Failed to mock FR-501/502 phantom + resolved fixtures at $FR501_502_FIXTURE_TMPDIR"
  rm -rf "$FR501_502_FIXTURE_TMPDIR"
  exit 1
fi
log_info "Mocked FR-501/502 fixture at: $FR501_502_FIXTURE_TMPDIR (phantom NoSuchClass.foo + resolved AuthMiddleware.validate stubs)"

# Anchors that prove the prompt instructs Ralph to produce an Appendix G
# missing entry per unresolved reference. The Appendix G verbatim form must
# appear exactly so the entry shape is byte-identical across loops, and the
# "For every unresolved reference" clause guards the negative case (resolved
# refs add nothing).
FR501_502_PHRASES=(
  '**Reference check (FR-501..503).**'
  'extract code-shaped references from the issue body and acceptance criteria'
  '`codegraph_search`'
  'For every unresolved reference, append one entry to `missing`'
  'References <symbol> in <field>; no such symbol in graph. Confirm during Investigate or flag as new code.'
  'Existing model-judged `missing` entries are preserved verbatim'
  'additive only'
)

for prompt_file in "${CODEGRAPH_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    rm -rf "$FR501_502_FIXTURE_TMPDIR"
    exit 1
  fi

  # Extract the Issue Plan region (between "## Issue Plan" and "## Subagent
  # Strategy") so the check cannot false-pass on unrelated sections.
  plan_region=$(awk '/^## Issue Plan/{flag=1} flag {print} /^## Subagent Strategy/{flag=0}' "$prompt_file")
  if [ -z "$plan_region" ]; then
    log_error "Could not extract Issue Plan region from: $prompt_file"
    rm -rf "$FR501_502_FIXTURE_TMPDIR"
    exit 1
  fi

  for phrase in "${FR501_502_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" <<< "$plan_region"; then
      log_error "FR-501/502 phrase missing in Issue Plan section of: $prompt_file"
      log_error "Expected verbatim: $phrase"
      rm -rf "$FR501_502_FIXTURE_TMPDIR"
      exit 1
    fi
  done

  log_info "Verified FR-501 extraction + FR-502 Appendix G entry-form in Issue Plan section of: $(basename "$prompt_file")"
done

rm -rf "$FR501_502_FIXTURE_TMPDIR"

log_info "FR-501+502 phantom-symbol test PASSED — both prompt files instruct Ralph to append the Appendix G verbatim entry per unresolved reference (resolved refs add nothing)"

# ============================================================================
# Static check: NFR-101 step-7 byte-equivalent baseline when CodeGraph off
# ============================================================================
# When codegraph_available is false, step 7's completion comment must reduce
# to the pre-PRD baseline — no **CodeGraph v1** block, no log lines, no
# warnings (NFR-101). The Phase 1 conditional-emission paragraph in the
# Completion Comment Format section must explicitly gate the CodeGraph v1
# block on codegraph_available AND instruct omission of the entire block when
# off so the comment remains byte-equivalent to a pre-PRD closure. Mocks
# codegraph-absent by removing .codegraph/ from a tmpdir fixture; the gating
# contract lives in the prompt source itself (it is instruction to the loop,
# not runnable code), so this is a static byte-check on both prompt files.
# Narrowed to the step-7 region (between "## Completion Comment Format" and
# "## Completion Signals") so it cannot false-pass on step-1's separate
# NFR-101 gating language. Runs as a static check before the heavy copier
# setup so it exercises independently.

log_step "Static check: NFR-101 step-7 byte-equivalent baseline when CodeGraph off"

# Mock codegraph-absent fixture: tmpdir with NO .codegraph/ and NO
# mcp__codegraph__* stubs (i.e., codegraph_available is false).
NFR101_STEP7_TMPDIR="$(mktemp -d)"
if [ -d "$NFR101_STEP7_TMPDIR/.codegraph" ]; then
  log_error "Failed to mock codegraph-absent fixture (unexpected .codegraph/ present)"
  rm -rf "$NFR101_STEP7_TMPDIR"
  exit 1
fi
log_info "Mocked codegraph-absent environment at: $NFR101_STEP7_TMPDIR (no .codegraph/, no mcp__codegraph__* stubs)"

# Pre-PRD baseline anchors that must remain unchanged in step 7 so the
# closing-comment shape stays byte-equivalent when CodeGraph is off.
NFR101_STEP7_BASELINE_ANCHORS=(
  '**Changes**:'        # Pre-PRD bullet header for change list
  '**Verification**:'   # Pre-PRD line for test/lint/build status
)

# Gating phrases — the Phase 1 Completion Comment paragraph must (a) gate the
# **CodeGraph v1** block emission on codegraph_available AND (b) instruct
# byte-equivalent omission when off. Both halves of the conditional must be
# present verbatim in step 7 of each prompt file.
NFR101_STEP7_GATING_PHRASES=(
  '**When `codegraph_available`, append a'                               # Conditional-emission opener
  'When `codegraph_available` is false, omit the block entirely'         # Off-branch omission instruction
  'byte-equivalent to a pre-PRD closure (NFR-101)'                       # Baseline-equivalence assertion
)

for prompt_file in "${CODEGRAPH_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    rm -rf "$NFR101_STEP7_TMPDIR"
    exit 1
  fi

  # Extract the step-7 region (between "## Completion Comment Format" and
  # "## Completion Signals") so the check cannot false-pass on step 1's
  # NFR-101 language or any other section.
  step7_region=$(awk '/^## Completion Comment Format/{flag=1} flag {print} /^## Completion Signals/{flag=0}' "$prompt_file")
  if [ -z "$step7_region" ]; then
    log_error "Could not extract step-7 region from: $prompt_file"
    rm -rf "$NFR101_STEP7_TMPDIR"
    exit 1
  fi

  # Assert pre-PRD baseline anchors preserved within step 7
  for anchor in "${NFR101_STEP7_BASELINE_ANCHORS[@]}"; do
    if ! grep -F -q -- "$anchor" <<< "$step7_region"; then
      log_error "NFR-101 step-7 pre-PRD baseline anchor missing in: $prompt_file"
      log_error "Expected verbatim: $anchor"
      rm -rf "$NFR101_STEP7_TMPDIR"
      exit 1
    fi
  done

  # Assert gating phrases present within step 7
  for phrase in "${NFR101_STEP7_GATING_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" <<< "$step7_region"; then
      log_error "NFR-101 step-7 gating phrase missing in: $prompt_file"
      log_error "Expected verbatim: $phrase"
      rm -rf "$NFR101_STEP7_TMPDIR"
      exit 1
    fi
  done

  log_info "Verified NFR-101 step-7 baseline gating in: $(basename "$prompt_file")"
done

rm -rf "$NFR101_STEP7_TMPDIR"

log_info "NFR-101 step-7 baseline test PASSED — both prompt files gate the **CodeGraph v1** block on codegraph_available with byte-equivalent fallback to a pre-PRD closure"

# ============================================================================
# Static check: FR-101 CodeGraph v1 block presence on enabled fixture
# ============================================================================
# When codegraph_available is true, step 7's completion comment must append
# a **CodeGraph v1** block whose schema matches Appendix C: a header line
# (**CodeGraph v1**:), then three comma-separated list fields (modified:,
# new:, oos_callers:), each of which may say `none` when empty (FR-101).
# This guards the Appendix C schema from accidental wording drift in BOTH
# prompt files: the source ortus/prompts/ralph-prompt.md and the
# template/ralph-prompt.md.jinja.
#
# Mocks the codegraph_available environment (.codegraph/ + a stub
# mcp__codegraph__* tool name) for environmental fidelity, parallel to
# existing mocks above. The Appendix C schema lives in the prompt source
# itself (it is instruction to the loop, not runnable code), so this is a
# static byte-check on both prompt files. Narrowed to the step-7 region
# (between "## Completion Comment Format" and "## Completion Signals") so
# the check cannot false-pass on unrelated sections. Drift simulations
# (e.g., omitting the oos_callers line from the schema) trigger exit 1.
# Runs as a static check before the heavy copier setup so it exercises
# independently.

log_step "Static check: FR-101 CodeGraph v1 block presence on enabled fixture"

# Mock codegraph_available environment: .codegraph/ + stub mcp__codegraph__codegraph_search
# (one of the three tools FR-103 restricts step-7 computation to).
FR101_PRESENT_TMPDIR="$(mktemp -d)"
mkdir -p "$FR101_PRESENT_TMPDIR/.codegraph"
echo "mcp__codegraph__codegraph_search" > "$FR101_PRESENT_TMPDIR/.codegraph/.stub-mcp-tool"
if [ ! -d "$FR101_PRESENT_TMPDIR/.codegraph" ] || [ ! -f "$FR101_PRESENT_TMPDIR/.codegraph/.stub-mcp-tool" ]; then
  log_error "Failed to set up codegraph_available mock fixture at $FR101_PRESENT_TMPDIR"
  rm -rf "$FR101_PRESENT_TMPDIR"
  exit 1
fi
log_info "Mocked codegraph_available environment at: $FR101_PRESENT_TMPDIR (.codegraph/ + stub mcp__codegraph__codegraph_search)"

# Appendix C schema anchors that must appear verbatim in step 7 of each
# prompt file. Together they prove the **CodeGraph v1** block carries the
# header AND all three list-field lines (modified, new, oos_callers).
FR101_SCHEMA_HEADER='**CodeGraph v1**:'
FR101_SCHEMA_FIELDS=(
  'modified: <symbol>@<file>:<line> (<N> callers, <M> cross-module) [, ...]'
  'new: <symbol>@<file>:<line> (<kind>) [, ...]'
  'oos_callers: <caller-symbol>@<file>:<line> -> <modified-symbol> [, ...]'
)

# The "may say `none`" semantic — proves each list field can collapse to
# `none` when empty, so docs-/test-only closures still emit a well-formed
# block per Appendix C.
FR101_NONE_SEMANTIC='Each list field is comma-separated; emit `none` when empty.'

for prompt_file in "${CODEGRAPH_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    rm -rf "$FR101_PRESENT_TMPDIR"
    exit 1
  fi

  # Extract the step-7 region (between "## Completion Comment Format" and
  # "## Completion Signals") so the check cannot false-pass on unrelated
  # sections of the prompt.
  step7_region=$(awk '/^## Completion Comment Format/{flag=1} flag {print} /^## Completion Signals/{flag=0}' "$prompt_file")
  if [ -z "$step7_region" ]; then
    log_error "Could not extract step-7 region from: $prompt_file"
    rm -rf "$FR101_PRESENT_TMPDIR"
    exit 1
  fi

  # Assert the **CodeGraph v1**: header is present in step 7
  if ! grep -F -q -- "$FR101_SCHEMA_HEADER" <<< "$step7_region"; then
    log_error "FR-101 CodeGraph v1 header missing in step 7 of: $prompt_file"
    log_error "Expected verbatim: $FR101_SCHEMA_HEADER"
    rm -rf "$FR101_PRESENT_TMPDIR"
    exit 1
  fi

  # Assert each Appendix C schema field line is present in step 7
  for field in "${FR101_SCHEMA_FIELDS[@]}"; do
    if ! grep -F -q -- "$field" <<< "$step7_region"; then
      log_error "FR-101 Appendix C schema field missing in step 7 of: $prompt_file"
      log_error "Expected verbatim: $field"
      rm -rf "$FR101_PRESENT_TMPDIR"
      exit 1
    fi
  done

  # Assert the "may say `none`" semantic clause is present in step 7
  if ! grep -F -q -- "$FR101_NONE_SEMANTIC" <<< "$step7_region"; then
    log_error "FR-101 'emit `none` when empty' semantic missing in step 7 of: $prompt_file"
    log_error "Expected verbatim: $FR101_NONE_SEMANTIC"
    rm -rf "$FR101_PRESENT_TMPDIR"
    exit 1
  fi

  log_info "Verified FR-101 CodeGraph v1 block (header + modified/new/oos_callers + none-semantic) in step 7 of: $(basename "$prompt_file")"
done

rm -rf "$FR101_PRESENT_TMPDIR"

log_info "FR-101 CodeGraph v1 block test PASSED — both prompt files emit the Appendix C schema (header + modified/new/oos_callers, each may say 'none') in step 7 when codegraph_available"

# ============================================================================
# Static check: FR-205 non-blocking — bd create failure does not stop step 8
# ============================================================================
# Locks FR-205: a failing `bd create` (stubbed to return non-zero) during step
# 7.5's auto-spawn must not break the loop. Step 7.5 must contain language
# that explicitly tells Ralph to proceed to step 8 (Close) even when bd create
# returns non-zero, codegraph_impact errors, or the gate evaluation throws.
# The prompt must continue uninterrupted from 7.5 → 8 (Close) → 9 (Commit & Push).
#
# Simulates a failing `bd create` by PATH-shadowing the bd binary with a stub
# returning exit 1, then asserts the rendered step 7.5 still routes Ralph to
# Close. Static byte-check on the prompt source — no copier render or shell
# ralph invocation required. Narrowed to the step-7.5 region (between
# "**7.5." and "8. **Close**") so the check cannot false-pass on step 6.5's
# similar non-blocking language earlier in the prompt.

log_step "Static check: FR-205 non-blocking on bd create failure during step 7.5"

# Set up a failing bd-create stub in a PATH-shadow tmpdir for environmental
# fidelity (parallel to the step 6.5 codegraph-sync-failure mock above). The
# stub forwards everything except `create`; `create` returns exit 1 to model
# the FR-205 failure scenario the prompt must handle non-blockingly.
FR205_FAIL_TMPDIR="$(mktemp -d)"
cat > "$FR205_FAIL_TMPDIR/bd" <<'STUB'
#!/bin/bash
if [ "$1" = "create" ]; then
  echo "stub: bd create failed (simulated FR-205 failure)" >&2
  exit 1
fi
# Forward all other bd subcommands to the real bd by stripping this dir from PATH
real_bd_path="$(PATH="$(echo "$PATH" | sed -e "s|$(dirname "$0"):||" -e "s|:$(dirname "$0")||")" command -v bd)"
exec "$real_bd_path" "$@"
STUB
chmod +x "$FR205_FAIL_TMPDIR/bd"

# Verify the stub is wired and `bd create` actually fails (proves the failure mode is real)
if ! PATH="$FR205_FAIL_TMPDIR:$PATH" command -v bd >/dev/null; then
  log_error "Failed to PATH-shadow bd stub at $FR205_FAIL_TMPDIR"
  rm -rf "$FR205_FAIL_TMPDIR"
  exit 1
fi
if PATH="$FR205_FAIL_TMPDIR:$PATH" bd create --title=test --description=test --type=task --priority=2 >/dev/null 2>&1; then
  log_error "Stub bd create returned 0; expected non-zero"
  rm -rf "$FR205_FAIL_TMPDIR"
  exit 1
fi
log_info "Simulated failing bd create at: $FR205_FAIL_TMPDIR (exits 1 on 'bd create')"

# FR-205 anchors that must appear verbatim in step 7.5 of each prompt file.
# Together they prove (a) the section is explicitly labeled FR-205 non-blocking,
# (b) the contract is stated unambiguously ("Step 7.5 shall never block step 8"),
# (c) all three failure modes from the issue are enumerated (bd create non-zero,
# codegraph_impact error, gate evaluation throw), and (d) the prompt routes
# Ralph onward to step 8 ("proceed to step 8") with the same posture as the
# already-tested step 6.5 non-blocking hook.
FR205_NONBLOCKING_PHRASES=(
  '**Non-blocking (FR-205).**'                # Section header anchor
  'Step 7.5 shall never block step 8.'        # Core contract
  'If `bd create` returns non-zero'           # Failure mode 1: bd create
  'if `codegraph_impact` errors'              # Failure mode 2: codegraph_impact
  'if the gate evaluation throws'             # Failure mode 3: gate exception
  'proceed to step 8'                         # Continuation instruction
  'same posture as step 6.5'                  # Consistency anchor
  'skip silently'                             # Silent fallback when off
)

# Steps that must follow 7.5 — proves the prompt continues to Close → Commit
# without aborting on a failing bd create or impact/gate error.
FR205_POST75_STEPS=(
  '^8\. \*\*Close\*\*'
  '^9\. \*\*Commit & Push\*\*'
)

for prompt_file in "${CODEGRAPH_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    rm -rf "$FR205_FAIL_TMPDIR"
    exit 1
  fi

  # Extract the step 7.5 region (between "**7.5." and "8. **Close**") so the
  # check cannot false-pass on step 6.5's non-blocking phrases earlier in the
  # same prompt or on unrelated sections.
  step75_region=$(awk '/^\*\*7\.5\./{flag=1} flag {print} /^8\. \*\*Close\*\*/{flag=0}' "$prompt_file")
  if [ -z "$step75_region" ]; then
    log_error "Could not extract step-7.5 region from: $prompt_file"
    rm -rf "$FR205_FAIL_TMPDIR"
    exit 1
  fi

  # Assert each FR-205 non-blocking phrase is present within step 7.5
  for phrase in "${FR205_NONBLOCKING_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" <<< "$step75_region"; then
      log_error "FR-205 non-blocking phrase missing in step 7.5 of: $prompt_file"
      log_error "Expected verbatim: $phrase"
      rm -rf "$FR205_FAIL_TMPDIR"
      exit 1
    fi
  done

  # Assert step 7.5 is followed by Close → Commit & Push, in order, and both
  # appear AFTER step 7.5 (so the prompt routes Ralph to Close, not Exit, on
  # a failing bd create).
  step75_lineno=$(grep -F -n -- '**7.5. Spawn follow-ups' "$prompt_file" | head -n 1 | cut -d: -f1)
  if [ -z "$step75_lineno" ]; then
    log_error "Step 7.5 anchor line missing in: $prompt_file"
    rm -rf "$FR205_FAIL_TMPDIR"
    exit 1
  fi
  prev_lineno="$step75_lineno"
  for step_pattern in "${FR205_POST75_STEPS[@]}"; do
    step_lineno=$(grep -E -n -- "$step_pattern" "$prompt_file" | head -n 1 | cut -d: -f1)
    if [ -z "$step_lineno" ]; then
      log_error "Post-7.5 step pattern '$step_pattern' missing in: $prompt_file"
      rm -rf "$FR205_FAIL_TMPDIR"
      exit 1
    fi
    if [ "$step_lineno" -le "$prev_lineno" ]; then
      log_error "Post-7.5 step '$step_pattern' (line $step_lineno) does not follow previous step (line $prev_lineno) in: $prompt_file"
      rm -rf "$FR205_FAIL_TMPDIR"
      exit 1
    fi
    prev_lineno="$step_lineno"
  done

  log_info "Verified FR-205 non-blocking step 7.5 → 8 → 9 chain in: $(basename "$prompt_file")"
done

rm -rf "$FR205_FAIL_TMPDIR"

log_info "FR-205 non-blocking test PASSED — both prompt files instruct Ralph to proceed to step 8 (Close) despite a failing bd create / codegraph_impact / gate evaluation"

# ============================================================================
# Static check: FR-206 idempotency — (closing-id, modified-symbol) keyed dedup
# ============================================================================
# Locks FR-206: before each `bd create` in step 7.5, Ralph must query the
# existing auto-codegraph cohort with `bd list --label=auto-codegraph --json`
# and skip the spawn if a matching issue exists for the same
# (closing-id, modified-symbol) pair. This guards against duplicate auto-spawns
# when the bash loop is killed and resumed mid-step-7.5. Acceptance asks that
# pre/post second-run `bd list --label=auto-codegraph` counts are equal — i.e.,
# re-running step 7.5 on the same closing id produces no duplicates. Static
# byte-check on the prompt source — narrowed to the step-7.5 region (between
# "**7.5." and "8. **Close**") so the check cannot false-pass on similar
# wording elsewhere. Mirrors the FR-204 / FR-205 idiom (awk extraction +
# grep -F per anchor).

log_step "Static check: FR-206 idempotency in step 7.5 ((closing-id, modified-symbol) keyed dedup)"

# FR-206 anchors that must appear verbatim within step 7.5 of each prompt
# file. Together they prove (a) the section is explicitly labeled FR-206
# idempotency, (b) the dedup query targets the auto-codegraph label cohort,
# (c) the dual-key conjunction (closing-id AND modified-symbol) is intact
# (both halves named, plus the canonical tuple form), (d) both per-caller and
# umbrella spawn modes have explicit skip semantics, (e) the non-collision
# corollaries are stated (different closing id with same symbol still spawns,
# and vice versa), (f) the restart scenario is named (bash loop killed and
# resumed), and (g) FR-205 non-blocking posture is inherited so a failing
# `bd list` query never blocks step 8.
FR206_IDEMPOTENCY_PHRASES=(
  '**Idempotency on retry (FR-206).**'                                # Section header anchor
  'Before each `bd create`, guard against duplicates'                 # Pre-create guard semantic
  'bd list --label=auto-codegraph --json'                             # Cohort query
  '`(closing-id, modified-symbol)`'                                   # Keyed-on tuple
  'closing-issue id'                                                  # First key half
  'modified-symbol name'                                              # Second key half
  'skip the spawn for that caller in per-caller mode'                 # Per-caller skip
  'skip the entire umbrella spawn in umbrella mode'                   # Umbrella skip
  'the same closing id with a different modified symbol still spawns' # Non-collision: same closing-id ≠ collision
  'the same modified symbol on a different closing id still spawns'   # Non-collision: same symbol ≠ collision
  'bash loop killed and resumed'                                      # Restart scenario
  'Same non-blocking posture as FR-205'                               # Non-blocking inheritance
  'a failing `bd list` query never blocks step 8'                     # Failing-query passthrough
)

for prompt_file in "${CODEGRAPH_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    exit 1
  fi

  # Extract the step 7.5 region (between "**7.5." and "8. **Close**") so the
  # check cannot false-pass on similar wording outside step 7.5.
  step75_region=$(awk '/^\*\*7\.5\./{flag=1} flag {print} /^8\. \*\*Close\*\*/{flag=0}' "$prompt_file")
  if [ -z "$step75_region" ]; then
    log_error "Could not extract step-7.5 region from: $prompt_file"
    exit 1
  fi

  # Assert each FR-206 idempotency phrase is present within step 7.5
  for phrase in "${FR206_IDEMPOTENCY_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" <<< "$step75_region"; then
      log_error "FR-206 idempotency phrase missing in step 7.5 of: $prompt_file"
      log_error "Expected verbatim: $phrase"
      exit 1
    fi
  done

  # Lock structural ordering: FR-206 idempotency block must precede the FR-205
  # non-blocking section in step 7.5, mirroring the prompt's literal layout
  # (idempotency guard documented before the catch-all non-blocking posture).
  idem_line=$(grep -n -F -- '**Idempotency on retry (FR-206).**' <<< "$step75_region" | head -1 | cut -d: -f1)
  nb_line=$(grep -n -F -- '**Non-blocking (FR-205).**' <<< "$step75_region" | head -1 | cut -d: -f1)
  if [ -z "$idem_line" ] || [ -z "$nb_line" ]; then
    log_error "FR-206 ordering check could not locate idempotency or non-blocking header in: $prompt_file"
    exit 1
  fi
  if [ "$idem_line" -ge "$nb_line" ]; then
    log_error "FR-206 ordering wrong in $prompt_file: idempotency (line $idem_line) must precede non-blocking (line $nb_line)"
    exit 1
  fi

  log_info "Verified FR-206 idempotency ((closing-id, modified-symbol) dedup + per-caller/umbrella skip + non-blocking inheritance) in step 7.5 of: $(basename "$prompt_file")"
done

log_info "FR-206 idempotency test PASSED — both prompt files lock the bd-list cohort query, dual-key (closing-id, modified-symbol) conjunction, per-caller/umbrella skip semantics, non-collision corollaries, restart scenario, and FR-205 non-blocking inheritance"

# ============================================================================
# Static check: FR-204 spawn metadata — type / priority / label / dep-edge
# ============================================================================
# Locks FR-204: each issue spawned by step 7.5 must be created with
# --type=task, --priority=2, --labels=auto-codegraph, AND followed by a
# `bd dep add <new-id> --depends-on <closing-id>` edge so the spawned issue
# does not enter `bd ready` while the closing issue is still open. This
# guards the metadata + dep-edge contract from accidental drift in either
# prompt file. Static byte-check on the prompt source — no copier render or
# bd execution required. Narrowed to the step-7.5 region (between "**7.5."
# and "8. **Close**") so the check cannot false-pass on similar metadata
# wording elsewhere in the prompt.

log_step "Static check: FR-204 spawn metadata in step 7.5 (type / priority / label / dep-edge)"

# FR-204 anchors that must appear verbatim within step 7.5 of each prompt
# file. Together they prove (a) each spawned issue carries the contracted
# metadata (type=task, priority=2, label=auto-codegraph), and (b) the dep
# edge is created so the spawned issue is gated behind the closing issue in
# `bd ready` until step 8 closes it.
FR204_METADATA_PHRASES=(
  '(FR-204)'                                          # Section anchor on the metadata-list header
  '`--type=task`'                                     # Type metadata
  '`--priority=2`'                                    # Priority metadata
  '`--labels=auto-codegraph`'                         # Label metadata
  '`bd dep add <new-id> --depends-on <closing-id>`'   # Dep edge invocation
  'does not enter `bd ready` until step 8 closes'     # NOT-in-bd-ready semantic
)

for prompt_file in "${CODEGRAPH_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    exit 1
  fi

  # Extract the step 7.5 region (between "**7.5." and "8. **Close**") so the
  # check cannot false-pass on similar metadata wording outside step 7.5.
  step75_region=$(awk '/^\*\*7\.5\./{flag=1} flag {print} /^8\. \*\*Close\*\*/{flag=0}' "$prompt_file")
  if [ -z "$step75_region" ]; then
    log_error "Could not extract step-7.5 region from: $prompt_file"
    exit 1
  fi

  # Assert each FR-204 metadata phrase is present within step 7.5
  for phrase in "${FR204_METADATA_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" <<< "$step75_region"; then
      log_error "FR-204 metadata phrase missing in step 7.5 of: $prompt_file"
      log_error "Expected verbatim: $phrase"
      exit 1
    fi
  done

  log_info "Verified FR-204 spawn metadata (type / priority / label / dep-edge) in step 7.5 of: $(basename "$prompt_file")"
done

log_info "FR-204 spawn-metadata test PASSED — both prompt files lock --type=task, --priority=2, --labels=auto-codegraph, and the bd dep add edge so spawned issues stay out of bd ready until the closing issue closes"

# ============================================================================
# Static check: FR-203 cap rule + per-caller / umbrella templates
# ============================================================================
# Locks FR-203: step 7.5's cap rule maps qualifying-caller count `N` to spawn
# shape — N==0 → no-op, 1-3 → per-caller issues (Appendix E per-caller
# template), 4+ → exactly one umbrella issue (Appendix E umbrella template).
# This guards (a) that all three branches are present, (b) that both Appendix
# E templates are rendered with their canonical title formats, and (c) that
# the umbrella template carries its `Qualifying callers:` bullet list and the
# `<N>` substitution. Static byte-check on the prompt source — no copier
# render or bd execution required. Narrowed to the step-7.5 region (between
# "**7.5." and "8. **Close**") so the check cannot false-pass on similar
# wording elsewhere in the prompt. Mirrors the FR-204 / FR-205 / FR-101 idiom
# (awk extraction + grep -F per anchor).

log_step "Static check: FR-203 cap rule and per-caller / umbrella templates in step 7.5"

# FR-203 anchors that must appear verbatim within step 7.5 of each prompt
# file. Together they prove (a) the cap-rule section is explicitly labeled
# FR-203 with all three N-branches (0 / 1-3 / 4+), (b) both Appendix E
# templates are rendered (per-caller for 1-3, umbrella for 4+), (c) the
# canonical title formats are intact (Verify-caller per-caller; Audit-N
# umbrella with `<N>` substitution), and (d) the umbrella template lists
# qualifying callers via its `Qualifying callers:` section header.
FR203_CAP_PHRASES=(
  '**Cap rule (FR-203, Appendix E).**'                                                    # Section header anchor
  '`N == 0` → no-op (skip silently; no spawn).'                                           # Branch 1: empty no-op
  '`1-3` qualifying callers → spawn one bd issue per caller'                              # Branch 2: per-caller mapping
  '`4 or more` qualifying callers → spawn exactly one **umbrella** issue'                 # Branch 3: umbrella mapping
  '**Per-caller template (Appendix E, 1-3 callers).**'                                    # Per-caller template header
  '**Umbrella template (Appendix E, 4 or more callers).**'                                # Umbrella template header
  'Title: Verify <caller-symbol> still behaves correctly after <modified-symbol> change (<closing-id>)'  # Per-caller title
  'Title: Audit <N> cross-module callers of <modified-symbol> after <closing-id>'         # Umbrella title (with <N>)
  'Qualifying callers:'                                                                   # Umbrella bullet-list section
  'Render verbatim'                                                                       # Verbatim-substitution contract
)

for prompt_file in "${CODEGRAPH_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    exit 1
  fi

  # Extract the step 7.5 region (between "**7.5." and "8. **Close**") so the
  # check cannot false-pass on similar wording outside step 7.5.
  step75_region=$(awk '/^\*\*7\.5\./{flag=1} flag {print} /^8\. \*\*Close\*\*/{flag=0}' "$prompt_file")
  if [ -z "$step75_region" ]; then
    log_error "Could not extract step-7.5 region from: $prompt_file"
    exit 1
  fi

  # Assert each FR-203 cap-rule / template phrase is present within step 7.5
  for phrase in "${FR203_CAP_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" <<< "$step75_region"; then
      log_error "FR-203 cap-rule / template phrase missing in step 7.5 of: $prompt_file"
      log_error "Expected verbatim: $phrase"
      exit 1
    fi
  done

  # Lock template ordering: per-caller template (for 1-3) must appear before
  # umbrella template (for 4+) so the document mirrors the cap-rule branch
  # order. Line-number assertion against the step-7.5 region.
  per_caller_line=$(grep -n -F -- '**Per-caller template (Appendix E, 1-3 callers).**' <<< "$step75_region" | head -1 | cut -d: -f1)
  umbrella_line=$(grep -n -F -- '**Umbrella template (Appendix E, 4 or more callers).**' <<< "$step75_region" | head -1 | cut -d: -f1)
  if [ -z "$per_caller_line" ] || [ -z "$umbrella_line" ]; then
    log_error "FR-203 template-order check could not locate one or both template headers in: $prompt_file"
    exit 1
  fi
  if [ "$per_caller_line" -ge "$umbrella_line" ]; then
    log_error "FR-203 template ordering wrong in $prompt_file: per-caller (line $per_caller_line) must precede umbrella (line $umbrella_line)"
    exit 1
  fi

  log_info "Verified FR-203 cap rule + per-caller / umbrella templates in step 7.5 of: $(basename "$prompt_file")"
done

log_info "FR-203 cap-and-template test PASSED — both prompt files lock the N==0 / 1-3 / 4+ cap rule and render both Appendix E templates (per-caller before umbrella) with their canonical title formats and the umbrella's Qualifying callers list"

# ============================================================================
# Static check: FR-202 heuristic gate — four conjunctive drop categories
# ============================================================================
# Locks FR-202: step 7.5's heuristic gate requires ALL FOUR conjunctive checks
# to hold for a caller `C` of modified symbol `S` to qualify for spawning —
# (1) cross top-level module, (2) not a test/spec file, (3) not in a utility
# directory, (4) public symbol (not _-prefixed and not in /internal/ or
# /private/). Any false drops the caller. This guards (a) all four drop
# categories are present with their bold-labeled headers, (b) the conjunctive
# framing ("**all four**" + "drop on any false") is intact, (c) the specific
# pattern fragments inside each category survive (tests/**, examples/**,
# /internal/) so a single category cannot be silently weakened, and (d) the
# Appendix D decision tree is rendered. Static byte-check on the prompt
# source — no copier render or bd execution required. Narrowed to the
# step-7.5 region (between "**7.5." and "8. **Close**") so the check cannot
# false-pass on similar wording elsewhere in the prompt. Mirrors the
# FR-203 / FR-204 / FR-205 idiom (awk extraction + grep -F per anchor).
#
# Acceptance-criteria mapping: the issue's "fixture covering all four drop
# categories plus one qualifying caller" wording predates the established
# static-anchor convention used for FR-203/FR-204/FR-205 (closed 2026-04-25);
# this check follows that convention by locking the gate's verbatim contract
# in the prompt source rather than executing a behavioral fixture, mirroring
# the documented sibling-task posture.

log_step "Static check: FR-202 heuristic gate — four conjunctive drop categories in step 7.5"

# FR-202 anchors that must appear verbatim within step 7.5 of each prompt
# file. Together they prove (a) the gate section is explicitly labeled
# FR-202 (Appendix D), (b) the conjunctive framing is intact (all four +
# drop-on-false), (c) each of the four drop categories has its bold-labeled
# header, (d) the specific pattern fragments inside each category survive
# (test/spec, utility-dir, public-symbol), and (e) the Appendix D decision
# tree is rendered with its `qualify` leaf.
FR202_GATE_PHRASES=(
  '**Heuristic gate (FR-202, Appendix D).**'                                 # Section header anchor
  '**all four**'                                                             # Conjunctive framing
  '(drop on any false)'                                                      # Drop-on-false semantic
  '**Cross top-level module.**'                                              # Drop category 1 header
  '**Not a test/spec file.**'                                                # Drop category 2 header
  '**Not in a utility directory.**'                                          # Drop category 3 header
  '**Public symbol.**'                                                       # Drop category 4 header
  '`tests/**`'                                                               # Test/spec pattern fragment (category 2)
  '`examples/**`'                                                            # Utility-dir pattern fragment (category 3)
  '`/internal/`'                                                             # Public-symbol exclusion fragment (category 4)
  'Decision tree (Appendix D):'                                              # Decision tree header
  'qualify'                                                                  # Decision tree's only non-drop leaf
)

for prompt_file in "${CODEGRAPH_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    exit 1
  fi

  # Extract the step 7.5 region (between "**7.5." and "8. **Close**") so the
  # check cannot false-pass on similar gate-wording outside step 7.5.
  step75_region=$(awk '/^\*\*7\.5\./{flag=1} flag {print} /^8\. \*\*Close\*\*/{flag=0}' "$prompt_file")
  if [ -z "$step75_region" ]; then
    log_error "Could not extract step-7.5 region from: $prompt_file"
    exit 1
  fi

  # Assert each FR-202 gate phrase is present within step 7.5
  for phrase in "${FR202_GATE_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" <<< "$step75_region"; then
      log_error "FR-202 heuristic-gate phrase missing in step 7.5 of: $prompt_file"
      log_error "Expected verbatim: $phrase"
      exit 1
    fi
  done

  # Lock structural ordering: heuristic-gate section (FR-202) must appear
  # before cap-rule section (FR-203) so the document mirrors runtime order
  # — gate filters callers first, then cap rule picks spawn shape.
  gate_line=$(grep -n -F -- '**Heuristic gate (FR-202, Appendix D).**' <<< "$step75_region" | head -1 | cut -d: -f1)
  cap_line=$(grep -n -F -- '**Cap rule (FR-203, Appendix E).**' <<< "$step75_region" | head -1 | cut -d: -f1)
  if [ -z "$gate_line" ] || [ -z "$cap_line" ]; then
    log_error "FR-202 ordering check could not locate gate or cap-rule header in: $prompt_file"
    exit 1
  fi
  if [ "$gate_line" -ge "$cap_line" ]; then
    log_error "FR-202 ordering wrong in $prompt_file: heuristic gate (line $gate_line) must precede cap rule (line $cap_line)"
    exit 1
  fi

  log_info "Verified FR-202 heuristic gate (4 drop categories + Appendix D tree) in step 7.5 of: $(basename "$prompt_file")"
done

log_info "FR-202 heuristic-gate test PASSED — both prompt files lock all four conjunctive drop categories (cross-module / test-spec / utility-dir / public-symbol) with the Appendix D decision tree, and the gate precedes the FR-203 cap rule"

# ============================================================================
# Smoke check: ralph.sh fails fast when bubblewrap is missing (FR-004)
# ============================================================================
# Locks FR-004's failure path: when ralph.sh runs on Linux without bubblewrap
# (`bwrap`) on PATH, the sandbox smoke test must (a) exit non-zero, (b) emit
# the install-hint string mentioning both "bubblewrap" and "socat", and (c)
# do so without ever invoking claude. Simulates the missing-bubblewrap
# condition by replacing PATH with a stub directory that holds symlinks to
# only the bare-minimum utilities ralph.sh needs before its smoke test runs
# (mkdir/date/tee/uname) — notably NO bwrap. Per the issue's acceptance
# criteria, this MUST NOT require root or actually uninstall system binaries.
# Linux-only by design (the smoke test's bwrap branch is Linux-gated).

log_step "Smoke check: ralph.sh exits non-zero when bubblewrap missing"

SMOKE_PLATFORM="$(uname -s)"
if [ "$SMOKE_PLATFORM" != "Linux" ]; then
  log_warn "Skipping bubblewrap-missing smoke test (platform: $SMOKE_PLATFORM, test designed for Linux)"
else
  # Helper: build a stub PATH directory with symlinks to the listed utilities
  # but no bwrap. Echoes the directory path on success; exits non-zero on
  # missing required utility. Future tests that need a similar
  # "everything-but-bwrap PATH" can call this helper directly.
  build_no_bwrap_stub_path() {
    local stub_dir
    stub_dir="$(mktemp -d)"
    for cmd in mkdir date tee uname; do
      local cmd_path
      cmd_path="$(command -v "$cmd" 2>/dev/null)"
      if [ -z "$cmd_path" ]; then
        log_error "Required utility not found in host PATH: $cmd"
        rm -rf "$stub_dir"
        return 1
      fi
      ln -s "$cmd_path" "$stub_dir/$cmd"
    done
    # Sanity: the stub PATH genuinely does not resolve bwrap. Guards against a
    # host where bwrap somehow ended up among the symlinked utilities.
    if PATH="$stub_dir" command -v bwrap >/dev/null 2>&1; then
      log_error "Stub PATH unexpectedly resolves bwrap; cannot simulate missing-bubblewrap"
      rm -rf "$stub_dir"
      return 1
    fi
    echo "$stub_dir"
  }

  SMOKE_NO_BWRAP_DIR="$(build_no_bwrap_stub_path)" || exit 1
  log_info "Built no-bwrap stub PATH at: $SMOKE_NO_BWRAP_DIR (no bwrap; mkdir/date/tee/uname symlinked)"

  SMOKE_RALPH_SCRIPTS=(
    "$ORTUS_DIR/ortus/ralph.sh"
    "$ORTUS_DIR/template/ortus/ralph.sh"
  )

  for ralph_script in "${SMOKE_RALPH_SCRIPTS[@]}"; do
    if [ ! -x "$ralph_script" ]; then
      log_error "ralph.sh script not executable: $ralph_script"
      rm -rf "$SMOKE_NO_BWRAP_DIR"
      exit 1
    fi

    smoke_run_dir="$(mktemp -d)"
    smoke_output_file="$smoke_run_dir/output.log"
    smoke_exit=0
    (
      cd "$smoke_run_dir"
      PATH="$SMOKE_NO_BWRAP_DIR" "$ralph_script"
    ) > "$smoke_output_file" 2>&1 || smoke_exit=$?

    if [ "$smoke_exit" -eq 0 ]; then
      log_error "Expected $ralph_script to exit non-zero when bwrap unavailable, got exit 0"
      log_error "Captured output:"
      cat "$smoke_output_file" >&2
      rm -rf "$SMOKE_NO_BWRAP_DIR" "$smoke_run_dir"
      exit 1
    fi

    smoke_output="$(cat "$smoke_output_file")"
    for expected in "bubblewrap" "socat"; do
      if ! grep -F -q -- "$expected" <<< "$smoke_output"; then
        log_error "Expected install-hint substring '$expected' missing from output of $ralph_script (exit=$smoke_exit)"
        log_error "Captured output:"
        echo "$smoke_output" >&2
        rm -rf "$SMOKE_NO_BWRAP_DIR" "$smoke_run_dir"
        exit 1
      fi
    done

    log_info "Verified bwrap-missing smoke test in: ${ralph_script#$ORTUS_DIR/} (exit=$smoke_exit)"
    rm -rf "$smoke_run_dir"
  done

  rm -rf "$SMOKE_NO_BWRAP_DIR"
  log_info "ralph.sh bwrap-missing smoke test PASSED — both ralph.sh copies fail fast with install-hint when bubblewrap is unavailable (no root required)"
fi

# ============================================================================
# Smoke check: ralph.sh --docker fails fast when Docker missing (ortus-lfft.7)
# ============================================================================
# Locks the ortus-heot detect-and-message decision: when ralph.sh --docker is
# invoked on a host without `docker` on PATH, the precondition check must
# (a) exit non-zero, (b) emit an ERROR string mentioning "Docker" plus an
# install hint, and (c) do so before any iteration runs claude. A second case
# locks the bundled-image detection: when `docker` is present but `docker
# sandbox --help` fails, the script must exit non-zero with a distinct
# "docker sandbox" hint pointing to Docker Desktop / bundled-image rollout.
# Both cases simulate absence via PATH overrides + a stub docker binary — no
# root required, no real Docker uninstall, no real claude invocation.

log_step "Smoke check: ralph.sh --docker exits non-zero when Docker missing"

# Helper: build a stub PATH directory with symlinks to the listed utilities
# but no docker. Echoes the directory path on success; exits non-zero on a
# missing required utility. Mirrors build_no_bwrap_stub_path() above.
build_no_docker_stub_path() {
  local stub_dir
  stub_dir="$(mktemp -d)"
  for cmd in mkdir date tee uname bash; do
    local cmd_path
    cmd_path="$(command -v "$cmd" 2>/dev/null)"
    if [ -z "$cmd_path" ]; then
      log_error "Required utility not found in host PATH: $cmd"
      rm -rf "$stub_dir"
      return 1
    fi
    ln -s "$cmd_path" "$stub_dir/$cmd"
  done
  if PATH="$stub_dir" command -v docker >/dev/null 2>&1; then
    log_error "Stub PATH unexpectedly resolves docker; cannot simulate missing-Docker"
    rm -rf "$stub_dir"
    return 1
  fi
  echo "$stub_dir"
}

SMOKE_NO_DOCKER_DIR="$(build_no_docker_stub_path)" || exit 1
log_info "Built no-docker stub PATH at: $SMOKE_NO_DOCKER_DIR (no docker; mkdir/date/tee/uname/bash symlinked)"

# Build a separate stub PATH where `docker` is present but rejects every
# subcommand (so `docker sandbox --help` exits non-zero) — exercises the
# bundled-image-missing branch. Reuses the no-docker stub's symlinks plus a
# stub `docker` script that always exits 1.
build_docker_no_sandbox_stub_path() {
  local stub_dir
  stub_dir="$(mktemp -d)"
  for cmd in mkdir date tee uname bash; do
    local cmd_path
    cmd_path="$(command -v "$cmd" 2>/dev/null)"
    if [ -z "$cmd_path" ]; then
      log_error "Required utility not found in host PATH: $cmd"
      rm -rf "$stub_dir"
      return 1
    fi
    ln -s "$cmd_path" "$stub_dir/$cmd"
  done
  cat > "$stub_dir/docker" <<'STUB_DOCKER_EOF'
#!/bin/bash
# Stub: pretend docker exists but reject every subcommand so that
# `docker sandbox --help` exits non-zero.
echo "stub docker: subcommand '$*' not supported" >&2
exit 1
STUB_DOCKER_EOF
  chmod +x "$stub_dir/docker"
  if ! PATH="$stub_dir" command -v docker >/dev/null 2>&1; then
    log_error "Stub PATH unexpectedly does not resolve docker"
    rm -rf "$stub_dir"
    return 1
  fi
  echo "$stub_dir"
}

SMOKE_DOCKER_NO_SANDBOX_DIR="$(build_docker_no_sandbox_stub_path)" || exit 1
log_info "Built docker-no-sandbox stub PATH at: $SMOKE_DOCKER_NO_SANDBOX_DIR (stub docker rejects subcommands)"

SMOKE_DOCKER_RALPH_SCRIPTS=(
  "$ORTUS_DIR/ortus/ralph.sh"
  "$ORTUS_DIR/template/ortus/ralph.sh"
)

for ralph_script in "${SMOKE_DOCKER_RALPH_SCRIPTS[@]}"; do
  if [ ! -x "$ralph_script" ]; then
    log_error "ralph.sh script not executable: $ralph_script"
    rm -rf "$SMOKE_NO_DOCKER_DIR" "$SMOKE_DOCKER_NO_SANDBOX_DIR"
    exit 1
  fi

  # Case A: --docker with no docker on PATH → exit non-zero, mention "Docker".
  smoke_run_dir="$(mktemp -d)"
  smoke_output_file="$smoke_run_dir/output.log"
  smoke_exit=0
  (
    cd "$smoke_run_dir"
    PATH="$SMOKE_NO_DOCKER_DIR" "$ralph_script" --docker --iterations 1
  ) > "$smoke_output_file" 2>&1 || smoke_exit=$?

  if [ "$smoke_exit" -eq 0 ]; then
    log_error "Expected $ralph_script --docker to exit non-zero when docker unavailable, got exit 0"
    log_error "Captured output:"
    cat "$smoke_output_file" >&2
    rm -rf "$SMOKE_NO_DOCKER_DIR" "$SMOKE_DOCKER_NO_SANDBOX_DIR" "$smoke_run_dir"
    exit 1
  fi

  smoke_output="$(cat "$smoke_output_file")"
  if ! grep -F -q -- "Docker" <<< "$smoke_output"; then
    log_error "Expected 'Docker' string in --docker missing-docker output of $ralph_script (exit=$smoke_exit)"
    log_error "Captured output:"
    echo "$smoke_output" >&2
    rm -rf "$SMOKE_NO_DOCKER_DIR" "$SMOKE_DOCKER_NO_SANDBOX_DIR" "$smoke_run_dir"
    exit 1
  fi
  if ! grep -F -q -- "Install" <<< "$smoke_output"; then
    log_error "Expected install hint ('Install') in --docker missing-docker output of $ralph_script (exit=$smoke_exit)"
    log_error "Captured output:"
    echo "$smoke_output" >&2
    rm -rf "$SMOKE_NO_DOCKER_DIR" "$SMOKE_DOCKER_NO_SANDBOX_DIR" "$smoke_run_dir"
    exit 1
  fi
  rm -rf "$smoke_run_dir"

  # Case B: --docker with docker present but `docker sandbox` failing →
  # exit non-zero, mention "docker sandbox" specifically.
  smoke_run_dir="$(mktemp -d)"
  smoke_output_file="$smoke_run_dir/output.log"
  smoke_exit=0
  (
    cd "$smoke_run_dir"
    PATH="$SMOKE_DOCKER_NO_SANDBOX_DIR" "$ralph_script" --docker --iterations 1
  ) > "$smoke_output_file" 2>&1 || smoke_exit=$?

  if [ "$smoke_exit" -eq 0 ]; then
    log_error "Expected $ralph_script --docker to exit non-zero when 'docker sandbox' unavailable, got exit 0"
    log_error "Captured output:"
    cat "$smoke_output_file" >&2
    rm -rf "$SMOKE_NO_DOCKER_DIR" "$SMOKE_DOCKER_NO_SANDBOX_DIR" "$smoke_run_dir"
    exit 1
  fi

  smoke_output="$(cat "$smoke_output_file")"
  if ! grep -F -q -- "docker sandbox" <<< "$smoke_output"; then
    log_error "Expected 'docker sandbox' string in --docker missing-sandbox output of $ralph_script (exit=$smoke_exit)"
    log_error "Captured output:"
    echo "$smoke_output" >&2
    rm -rf "$SMOKE_NO_DOCKER_DIR" "$SMOKE_DOCKER_NO_SANDBOX_DIR" "$smoke_run_dir"
    exit 1
  fi
  rm -rf "$smoke_run_dir"

  log_info "Verified --docker precondition smoke test in: ${ralph_script#$ORTUS_DIR/} (both missing-docker and missing-sandbox cases)"
done

rm -rf "$SMOKE_NO_DOCKER_DIR" "$SMOKE_DOCKER_NO_SANDBOX_DIR"
log_info "ralph.sh --docker precondition smoke test PASSED — both ralph.sh copies fail fast with install hints when Docker or 'docker sandbox' is unavailable"

# ============================================================================
# Unit test: ralph.sh argument parser handles --docker (ortus-lfft.1, FR-006)
# ============================================================================
# Locks T2.1's contract: the --docker flag is recognized by the argument
# parser, sets USE_DOCKER=1 when present, and leaves USE_DOCKER empty when
# absent. Mixing --docker with --tasks N and --iterations N must not break
# either flag's parsing. This test extracts the variable-init + while-shift
# parser block from each ralph.sh copy and sources it in a subshell with
# controlled positional parameters — exercising the real parser source
# without triggering sandbox_smoke_test or invoking claude.

log_step "Unit test: ralph.sh --docker argument parsing"

UNIT_RALPH_SCRIPTS=(
  "$ORTUS_DIR/ortus/ralph.sh"
  "$ORTUS_DIR/template/ortus/ralph.sh"
)

# Extract the lines from `IDLE_SLEEP=...` (first variable initializer) through
# the matching `done` of the argument-parsing while-loop. This is the entire
# parse surface and is the chunk we want to exercise in isolation.
extract_parser_block() {
  local script="$1"
  awk '
    /^IDLE_SLEEP=/ { in_block = 1 }
    in_block      { print }
    in_block && /^done[[:space:]]*$/ { exit }
  ' "$script"
}

# Run the extracted parser with the given positional arguments and echo the
# resulting state of every flag-backed variable. Square brackets surround each
# value so empty strings remain greppable.
run_ralph_parser() {
  local script="$1"; shift
  local parser_block
  parser_block="$(extract_parser_block "$script")"
  bash -c "
    set -e
    $parser_block
    echo \"USE_DOCKER=[\${USE_DOCKER:-}]\"
    echo \"FAST_MODE=[\${FAST_MODE:-}]\"
    echo \"MAX_TASKS=[\${MAX_TASKS:-}]\"
    echo \"MAX_ITERATIONS=[\${MAX_ITERATIONS:-}]\"
    echo \"IDLE_SLEEP=[\${IDLE_SLEEP:-}]\"
  " bash "$@"
}

for ralph_script in "${UNIT_RALPH_SCRIPTS[@]}"; do
  if [ ! -f "$ralph_script" ]; then
    log_error "ralph.sh not found: $ralph_script"
    exit 1
  fi

  # Static syntax check (acceptance criterion: bash -n exits 0).
  if ! bash -n "$ralph_script"; then
    log_error "bash -n failed for $ralph_script"
    exit 1
  fi

  # Sanity-check the extracted parser block actually contains the --docker case
  # — guards against silent extraction drift (e.g., if the parser is later
  # refactored into a function and the IDLE_SLEEP= anchor moves).
  block="$(extract_parser_block "$ralph_script")"
  if ! grep -F -q -- '--docker' <<< "$block"; then
    log_error "Extracted parser block for $ralph_script does not contain --docker case"
    log_error "Block was:"
    echo "$block" >&2
    exit 1
  fi

  # Case 1: --docker alone sets USE_DOCKER=1.
  out="$(run_ralph_parser "$ralph_script" --docker)"
  if ! grep -F -q -- 'USE_DOCKER=[1]' <<< "$out"; then
    log_error "Expected USE_DOCKER=[1] when --docker passed for $ralph_script"
    log_error "Output was:"
    echo "$out" >&2
    exit 1
  fi

  # Case 2: no --docker leaves USE_DOCKER empty.
  out="$(run_ralph_parser "$ralph_script")"
  if ! grep -F -q -- 'USE_DOCKER=[]' <<< "$out"; then
    log_error "Expected USE_DOCKER=[] when --docker absent for $ralph_script"
    log_error "Output was:"
    echo "$out" >&2
    exit 1
  fi

  # Case 3: --docker mixes with --fast, --tasks, --iterations without breaking
  # any of them (acceptance criterion #3).
  out="$(run_ralph_parser "$ralph_script" --docker --fast --tasks 5 --iterations 3)"
  for expected in 'USE_DOCKER=[1]' 'FAST_MODE=[--fast]' 'MAX_TASKS=[5]' 'MAX_ITERATIONS=[3]'; do
    if ! grep -F -q -- "$expected" <<< "$out"; then
      log_error "Mixed-flag parsing expected '$expected' in output of $ralph_script"
      log_error "Output was:"
      echo "$out" >&2
      exit 1
    fi
  done

  log_info "Unit test --docker: PASSED for ${ralph_script#$ORTUS_DIR/}"
done

log_info "ralph.sh --docker argument-parsing unit test PASSED — both copies recognize the flag, set USE_DOCKER correctly, and continue to parse --fast/--tasks/--iterations alongside it"

# ============================================================================
# Routing test: ralph.sh --docker invokes 'docker sandbox run' (ortus-lfft.6)
# ============================================================================
# Locks FR-006 / T2.2: in --docker mode, ralph.sh must route the inner claude
# session through `docker sandbox run claude --name ortus-ralph --` instead of
# the host claude binary, while still producing logs/ralph-*.log on the host
# filesystem so tail.sh and existing tooling continue to work.
#
# Skip-detection (acceptance #2): on a host lacking `docker sandbox`, this
# case prints SKIP and continues so the full suite still exits 0 (acceptance
# #4). The skip-check runs before any PATH manipulation so it reflects true
# host state. When `docker sandbox` is available, the test uses fully mocked
# stub PATHs to keep it fast and offline:
#   - Sub-case A (acceptance #3 success half): stub `docker` accepts
#     `sandbox --help` (precondition pass-through) and emits
#     <promise>EMPTY</promise> on `sandbox run` so ralph's outer loop exits
#     gracefully on iteration 1; stub `claude` exits 99 with a loud
#     ROUTING_FAILURE marker if ever invoked, proving the host claude binary
#     is bypassed (acceptance #1 routing assertion).
#   - Sub-case B (acceptance #3 broken half): stub `docker` rejects every
#     subcommand (including `sandbox --help`) so ralph's precondition check
#     trips and exits non-zero before any iteration runs.

log_step "Routing test: ralph.sh --docker routes through 'docker sandbox run' (skip when docker unavailable)"

if ! command -v docker >/dev/null 2>&1 || ! docker sandbox --help >/dev/null 2>&1; then
  log_info "SKIP: docker sandbox not available on host; --docker routing test skipped"
else
  build_docker_routing_stub_path() {
    local mode="$1"  # "success" or "broken"
    local stub_dir
    stub_dir="$(mktemp -d)"
    for cmd in mkdir date tee uname bash sed grep cat dirname; do
      local cmd_path
      cmd_path="$(command -v "$cmd" 2>/dev/null)"
      if [ -z "$cmd_path" ]; then
        log_error "Required utility not found in host PATH: $cmd"
        rm -rf "$stub_dir"
        return 1
      fi
      ln -s "$cmd_path" "$stub_dir/$cmd"
    done

    if [ "$mode" = "success" ]; then
      cat > "$stub_dir/docker" <<'STUB_DOCKER_OK_EOF'
#!/bin/bash
# Record every invocation so the test can assert routing.
echo "docker $*" >> "${ROUTING_DOCKER_LOG:-/dev/null}"
case "$1 $2" in
  "sandbox --help")
    echo "Stub docker sandbox: help OK"
    exit 0
    ;;
  "sandbox run")
    # Simulate the in-container claude returning the EMPTY signal so ralph's
    # outer loop terminates gracefully on iteration 1.
    echo "<promise>EMPTY</promise>"
    exit 0
    ;;
esac
exit 0
STUB_DOCKER_OK_EOF
    else
      cat > "$stub_dir/docker" <<'STUB_DOCKER_BROKEN_EOF'
#!/bin/bash
# Stub docker that fails every subcommand (including `sandbox --help`) so
# ralph's docker_precondition_check trips and exits non-zero.
echo "docker $*" >> "${ROUTING_DOCKER_LOG:-/dev/null}"
echo "stub docker: subcommand '$*' not supported" >&2
exit 1
STUB_DOCKER_BROKEN_EOF
    fi
    chmod +x "$stub_dir/docker"

    cat > "$stub_dir/claude" <<'STUB_CLAUDE_EOF'
#!/bin/bash
# Host claude must NOT be invoked in --docker mode — the routing forwards
# through `docker sandbox run claude` instead. Loud failure marker so the
# test detects any routing regression.
echo "ROUTING_FAILURE: host claude binary invoked in --docker mode" >&2
exit 99
STUB_CLAUDE_EOF
    chmod +x "$stub_dir/claude"

    echo "$stub_dir"
  }

  ROUTING_RALPH_SCRIPTS=(
    "$ORTUS_DIR/ortus/ralph.sh"
    "$ORTUS_DIR/template/ortus/ralph.sh"
  )

  for ralph_script in "${ROUTING_RALPH_SCRIPTS[@]}"; do
    if [ ! -x "$ralph_script" ]; then
      log_error "ralph.sh script not executable: $ralph_script"
      exit 1
    fi

    # Sub-case A: successful docker invocation → ralph exits 0, routes through
    # `docker sandbox run claude`, produces a host log, host claude untouched.
    routing_stub_dir="$(build_docker_routing_stub_path success)" || exit 1
    routing_run_dir="$(mktemp -d)"
    routing_docker_log="$routing_run_dir/docker-invoke.log"
    : > "$routing_docker_log"
    routing_exit=0
    (
      cd "$routing_run_dir"
      PATH="$routing_stub_dir" ROUTING_DOCKER_LOG="$routing_docker_log" \
        "$ralph_script" --docker --iterations 1 --idle-sleep 1
    ) > "$routing_run_dir/output.log" 2>&1 || routing_exit=$?

    if [ "$routing_exit" -ne 0 ]; then
      log_error "Expected $ralph_script --docker to exit 0 on successful docker invocation, got exit $routing_exit"
      log_error "Captured output:"
      cat "$routing_run_dir/output.log" >&2
      log_error "Docker invocation log:"
      cat "$routing_docker_log" >&2
      rm -rf "$routing_stub_dir" "$routing_run_dir"
      exit 1
    fi

    if ! grep -F -q -- "sandbox run claude" "$routing_docker_log"; then
      log_error "Expected docker stub to be invoked with 'sandbox run claude' for $ralph_script (routing assertion)"
      log_error "Docker invocation log:"
      cat "$routing_docker_log" >&2
      rm -rf "$routing_stub_dir" "$routing_run_dir"
      exit 1
    fi

    if ! ls "$routing_run_dir"/logs/ralph-*.log >/dev/null 2>&1; then
      log_error "Expected log file at $routing_run_dir/logs/ralph-*.log for $ralph_script"
      ls -la "$routing_run_dir" >&2
      rm -rf "$routing_stub_dir" "$routing_run_dir"
      exit 1
    fi

    if grep -F -q -- "ROUTING_FAILURE" "$routing_run_dir/output.log"; then
      log_error "Host claude binary invoked in --docker mode for $ralph_script (routing broken)"
      log_error "Captured output:"
      cat "$routing_run_dir/output.log" >&2
      rm -rf "$routing_stub_dir" "$routing_run_dir"
      exit 1
    fi

    rm -rf "$routing_stub_dir" "$routing_run_dir"

    # Sub-case B: broken docker invocation → ralph exits non-zero before any
    # iteration runs (precondition check trips on stub `sandbox --help` failure).
    routing_stub_dir="$(build_docker_routing_stub_path broken)" || exit 1
    routing_run_dir="$(mktemp -d)"
    routing_docker_log="$routing_run_dir/docker-invoke.log"
    : > "$routing_docker_log"
    routing_exit=0
    (
      cd "$routing_run_dir"
      PATH="$routing_stub_dir" ROUTING_DOCKER_LOG="$routing_docker_log" \
        "$ralph_script" --docker --iterations 1 --idle-sleep 1
    ) > "$routing_run_dir/output.log" 2>&1 || routing_exit=$?

    if [ "$routing_exit" -eq 0 ]; then
      log_error "Expected $ralph_script --docker to exit non-zero on broken docker invocation, got exit 0"
      log_error "Captured output:"
      cat "$routing_run_dir/output.log" >&2
      rm -rf "$routing_stub_dir" "$routing_run_dir"
      exit 1
    fi

    rm -rf "$routing_stub_dir" "$routing_run_dir"

    log_info "Routing test --docker: PASSED for ${ralph_script#$ORTUS_DIR/} (success: exit 0 + routing + host log + host claude bypassed; broken: exit non-zero)"
  done

  log_info "ralph.sh --docker routing test PASSED — both copies route through 'docker sandbox run', produce host log files, bypass the host claude binary, and fail fast on broken docker invocation"
fi

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
