#!/bin/bash
# test-prd-decompose.sh - Test suite for PRD-decompose CodeGraph reference
# validation (FR-301..304 from prd/PRD-ortus-codegraph-beads-integration.md).
#
# Usage: ./tests/test-prd-decompose.sh [--keep]
#
# Options:
#   --keep      Keep any tmp fixtures created during the run (default: cleanup)
#
# Exit codes:
#   0 - All tests passed
#   1 - Test setup failed (fixture missing, prompt files missing, etc.)
#   2 - A static check failed (wording drift in either prompt file)
#
# This file is the test substrate for T5.6 (FR-303 phantom annotation) and
# T5.7 (FR-304 Likely files shortlist). T5.5 lands the scaffolding: the
# fixture PRD and the static checks that lock the FR-301..304 wording in
# both ortus/prompts/prd-decompose-prompt.md and the template mirror.
# T5.6/T5.7 will extend this script with end-to-end decomposition tests
# that exercise actual codegraph_search behavior.

set -e

# Defaults
KEEP_PROJECT=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORTUS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FIXTURES_DIR="$SCRIPT_DIR/fixtures"
FIXTURE_PRD="$FIXTURES_DIR/prd-decompose-fixture.md"

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
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

# The two prd-decompose prompt files that must stay byte-equivalent for the
# unconditional FR-301..304 block. parity is enforced by
# scripts/check-ortus-parity.sh; this test additionally locks the wording.
PRD_DECOMPOSE_PROMPT_FILES=(
  "$ORTUS_DIR/ortus/prompts/prd-decompose-prompt.md"
  "$ORTUS_DIR/template/ortus/prompts/prd-decompose-prompt.md"
)

# ============================================================================
# Static check: fixture PRD present at expected path with both reference kinds
# ============================================================================
# Acceptance criteria for T5.5:
#   - Fixture PRD seeded with one real reference and one phantom reference.
#   - Fixture file present at expected path.
#
# The fixture is the substrate T5.6/T5.7 build on, so this test must hard-fail
# if the fixture goes missing or loses either reference kind.

log_step "Static check: fixture PRD present with one real + one phantom reference"

if [ ! -f "$FIXTURE_PRD" ]; then
  log_error "Fixture PRD missing at expected path: $FIXTURE_PRD"
  exit 1
fi
log_info "Fixture PRD present at: $FIXTURE_PRD"

# Phantom reference — dotted method that must NOT resolve in this project's
# graph. Locked to the design field's example so T5.6 can grep for it.
FIXTURE_PHANTOM_REF="AuthMiddleware.refreshToken"

# Real reference — both a CamelCase symbol and a recognized-extension path
# that DO resolve in this project's graph (extensions/context.py defines
# GitConfigContext). T5.7 will assert the FR-304 Likely files shortlist
# names extensions/context.py.
FIXTURE_REAL_SYMBOL="GitConfigContext"
FIXTURE_REAL_PATH="extensions/context.py"

if ! grep -F -q -- "$FIXTURE_PHANTOM_REF" "$FIXTURE_PRD"; then
  log_error "Fixture PRD missing phantom reference: $FIXTURE_PHANTOM_REF"
  exit 2
fi
log_info "Verified phantom reference present: $FIXTURE_PHANTOM_REF"

if ! grep -F -q -- "$FIXTURE_REAL_SYMBOL" "$FIXTURE_PRD"; then
  log_error "Fixture PRD missing real CamelCase symbol: $FIXTURE_REAL_SYMBOL"
  exit 2
fi
log_info "Verified real CamelCase symbol present: $FIXTURE_REAL_SYMBOL"

if ! grep -F -q -- "$FIXTURE_REAL_PATH" "$FIXTURE_PRD"; then
  log_error "Fixture PRD missing real source-extension path: $FIXTURE_REAL_PATH"
  exit 2
fi
log_info "Verified real source-extension path present: $FIXTURE_REAL_PATH"

# Sanity-check: the real path must point at an actual file in the repo, so
# T5.7's FR-304 shortlist assertion (resolved-set → description) remains
# meaningful when CodeGraph is available.
if [ ! -f "$ORTUS_DIR/$FIXTURE_REAL_PATH" ]; then
  log_error "Fixture references $FIXTURE_REAL_PATH but the file is absent from the repo"
  exit 2
fi
log_info "Verified real path resolves to a real repo file: $ORTUS_DIR/$FIXTURE_REAL_PATH"

log_info "Fixture-presence test PASSED — fixture has both reference kinds and the real path is a real file"

# ============================================================================
# Static check: FR-301 reference-extraction patterns wording
# ============================================================================
# When codegraph_available, the FR-301 sub-paragraph in prd-decompose-prompt.md
# must declare the three reference-extraction patterns (CamelCase, dotted
# methods, source-extension paths) verbatim. This guards the FR-301 contract
# from accidental wording drift in BOTH prompt files: ortus/prompts/
# prd-decompose-prompt.md and template/ortus/prompts/prd-decompose-prompt.md.

log_step "Static check: FR-301 reference-extraction patterns"

FR301_HEADER='**Reference extraction (FR-301).**'
FR301_PATTERNS=(
  'CamelCase identifiers'
  '[A-Z][A-Za-z0-9_]*'
  'dotted methods'
  '[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*'
  'file paths containing'
  '.ts'
  '.tsx'
  '.js'
  '.py'
  '.rs'
  '.go'
  '.java'
  '.rb'
)

for prompt_file in "${PRD_DECOMPOSE_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    exit 1
  fi
  if ! grep -F -q -- "$FR301_HEADER" "$prompt_file"; then
    log_error "FR-301 header missing in: $prompt_file"
    log_error "Expected verbatim: $FR301_HEADER"
    exit 2
  fi
  for pattern in "${FR301_PATTERNS[@]}"; do
    if ! grep -F -q -- "$pattern" "$prompt_file"; then
      log_error "FR-301 reference-extraction pattern missing in: $prompt_file"
      log_error "Expected verbatim: $pattern"
      exit 2
    fi
  done
  log_info "Verified FR-301 patterns in: $(basename "$prompt_file")"
done

log_info "FR-301 patterns test PASSED — both prompt files declare CamelCase / dotted-method / source-extension patterns verbatim"

# ============================================================================
# Static check: FR-302 per-reference validation via codegraph_search
# ============================================================================
# The FR-302 sub-paragraph must require codegraph_search per extracted
# reference and partition into resolved/unresolved sets. It must also forbid
# main-session calls to codegraph_explore / codegraph_context (parity with
# step 4 of ralph-prompt.md).

log_step "Static check: FR-302 per-reference validation via codegraph_search"

FR302_HEADER='**Per-reference validation (FR-302).**'
FR302_PHRASES=(
  'codegraph_search'
  '*Resolved*'
  '*Unresolved*'
  '<symbol>@<file>'
)
FR302_FORBIDDEN_TOOLS=(
  'codegraph_explore'
  'codegraph_context'
)

for prompt_file in "${PRD_DECOMPOSE_PROMPT_FILES[@]}"; do
  if ! grep -F -q -- "$FR302_HEADER" "$prompt_file"; then
    log_error "FR-302 header missing in: $prompt_file"
    log_error "Expected verbatim: $FR302_HEADER"
    exit 2
  fi
  for phrase in "${FR302_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" "$prompt_file"; then
      log_error "FR-302 phrase missing in: $prompt_file"
      log_error "Expected verbatim: $phrase"
      exit 2
    fi
  done
  # FR-302 must explicitly forbid the heavy tools from the main session.
  # We assert each forbidden tool name appears in the prompt only inside a
  # "Do not call" / "do not use" sentence — we check the negative wording
  # lives in the same file.
  if ! grep -F -q -- "Do not call \`codegraph_explore\` or \`codegraph_context\`" "$prompt_file"; then
    log_error "FR-302 main-session forbiddance missing in: $prompt_file"
    log_error "Expected verbatim: Do not call \`codegraph_explore\` or \`codegraph_context\`"
    exit 2
  fi
  for tool in "${FR302_FORBIDDEN_TOOLS[@]}"; do
    if ! grep -F -q -- "$tool" "$prompt_file"; then
      log_error "FR-302 expected forbidden-tool name '$tool' missing entirely from: $prompt_file"
      exit 2
    fi
  done
  log_info "Verified FR-302 contract in: $(basename "$prompt_file")"
done

log_info "FR-302 codegraph_search test PASSED — both prompt files require codegraph_search and forbid codegraph_explore/codegraph_context in the main session"

# ============================================================================
# Static check: FR-303 advisory annotation comment (Appendix F)
# ============================================================================
# FR-303 must require a bd comments add containing the **CodeGraph references**
# block per Appendix F template, AND must explicitly state the comment is
# advisory: never blocks creation, never alters the issue body, never changes
# priority/type. This guards the FR-303 contract from wording drift.

log_step "Static check: FR-303 advisory annotation comment (Appendix F)"

FR303_HEADER='**Annotation comment (FR-303).**'
FR303_PHRASES=(
  'bd comments add'
  '**CodeGraph references**'
  'Unresolved:'
  'Resolved:'
  'never blocks issue creation'
  'never alters the issue body'
)

for prompt_file in "${PRD_DECOMPOSE_PROMPT_FILES[@]}"; do
  if ! grep -F -q -- "$FR303_HEADER" "$prompt_file"; then
    log_error "FR-303 header missing in: $prompt_file"
    log_error "Expected verbatim: $FR303_HEADER"
    exit 2
  fi
  for phrase in "${FR303_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" "$prompt_file"; then
      log_error "FR-303 phrase missing in: $prompt_file"
      log_error "Expected verbatim: $phrase"
      exit 2
    fi
  done
  log_info "Verified FR-303 advisory-annotation contract in: $(basename "$prompt_file")"
done

log_info "FR-303 annotation test PASSED — both prompt files require the Appendix F annotation and explicitly bound it as advisory"

# ============================================================================
# Static check: FR-304 Likely files shortlist (description, not comment)
# ============================================================================
# FR-304 must require a one-line **Likely files**: ... shortlist appended to
# the issue DESCRIPTION (not a comment) when at least one extracted reference
# resolves. The description-vs-comment distinction matters: step 4 (Investigate)
# treats the description as canonical scope, the FR-303 comment as an advisory
# hint. This guards both the placement and the wording.

log_step "Static check: FR-304 Likely files shortlist (description placement)"

FR304_HEADER='**Likely files shortlist (FR-304).**'
FR304_PHRASES=(
  '**Likely files**:'
  'append a one-line'
  'shortlist'
  'description'
  'deduplicate paths'
)

for prompt_file in "${PRD_DECOMPOSE_PROMPT_FILES[@]}"; do
  if ! grep -F -q -- "$FR304_HEADER" "$prompt_file"; then
    log_error "FR-304 header missing in: $prompt_file"
    log_error "Expected verbatim: $FR304_HEADER"
    exit 2
  fi
  for phrase in "${FR304_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" "$prompt_file"; then
      log_error "FR-304 phrase missing in: $prompt_file"
      log_error "Expected verbatim: $phrase"
      exit 2
    fi
  done
  log_info "Verified FR-304 Likely-files-in-description contract in: $(basename "$prompt_file")"
done

log_info "FR-304 Likely files test PASSED — both prompt files require the shortlist in the issue description (not a comment)"

# ============================================================================
# Summary
# ============================================================================

log_step "All Tests Passed!"
echo ""
log_info "Fixture PRD presence: tests/fixtures/prd-decompose-fixture.md ✓"
log_info "FR-301 reference-extraction patterns: ✓"
log_info "FR-302 codegraph_search per reference + main-session forbiddance: ✓"
log_info "FR-303 advisory **CodeGraph references** annotation comment: ✓"
log_info "FR-304 **Likely files** shortlist in description: ✓"
echo ""
log_info "T5.6/T5.7 will extend this script with end-to-end decomposition tests."

if [ "$KEEP_PROJECT" = true ]; then
  log_info "Fixture preserved at: $FIXTURE_PRD"
fi

exit 0
