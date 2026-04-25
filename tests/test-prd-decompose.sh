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
# This file is the test substrate for T5.5 (scaffolding fixture + FR-301..304
# wording locks), T5.6 (FR-303 phantom annotation lands; advisory bounds), and
# T5.7 (FR-304 resolved-symbol Likely files shortlist; description placement).
# All three landings are static byte-checks on the prompt source coupled to
# the fixture PRD's phantom (AuthMiddleware.refreshToken) and real
# (GitConfigContext + extensions/context.py) references — verifying both
# ortus/prompts/prd-decompose-prompt.md and the template mirror teach the
# FR-301..304 contract verbatim.

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
# Static check: FR-303 phantom annotation lands; advisory bounds (body/desc/AC + priority/type)
# ============================================================================
# T5.6 — when codegraph_available is true and a work item references a phantom
# symbol (one that does NOT resolve in the graph, e.g.,
# AuthMiddleware.refreshToken in the fixture PRD), the FR-303 sub-paragraph
# must instruct the decomposer to:
#   1. After bd create, attach a bd comments add ... with the Appendix F
#      **CodeGraph references** block surfacing the phantom under "Unresolved:".
#   2. NEVER block issue creation, alter the issue body / description /
#      acceptance_criteria, or change --priority / --type from what the
#      decomposer would otherwise set absent the annotation.
#   3. Omit the comment silently when codegraph_search errors or the graph
#      is partial — defensive posture parity with the rest of the block.
#
# Locks the FR-303 advisory-bounds contract from accidental wording drift in
# BOTH prompt files. Extends T5.5's FR-303 anchors with the full advisory
# triplet (body/description/acceptance_criteria), the priority/type clause,
# the "advisory only" classifier, and the silent-omission defensive posture
# — none of which T5.5 locks. Couples explicitly to the fixture's
# AuthMiddleware.refreshToken so a T5.6 regression surfaces the phantom-
# symbol path the prompt is meant to handle.

log_step "Static check: FR-303 phantom annotation lands; advisory bounds (body/desc/AC + priority/type)"

# Mock codegraph_available environment: .codegraph/ + stub mcp__codegraph__codegraph_search
# (the tool the FR-301..304 sub-paragraph invokes per extracted reference).
FR303_PHANTOM_FIXTURE_TMPDIR="$(mktemp -d)"
mkdir -p "$FR303_PHANTOM_FIXTURE_TMPDIR/.codegraph"
echo "mcp__codegraph__codegraph_search" > "$FR303_PHANTOM_FIXTURE_TMPDIR/.codegraph/.stub-mcp-tool"

# Phantom-symbol fixture: $FIXTURE_PHANTOM_REF (AuthMiddleware.refreshToken,
# verified present in the fixture PRD on line 109). The stub codegraph_search
# response is empty, so FR-302 partitions the reference into the *unresolved*
# bucket; FR-303 must then surface it under "Unresolved:" in the Appendix F
# annotation comment without altering the issue body/desc/AC or priority/type.
echo "$FIXTURE_PHANTOM_REF" > "$FR303_PHANTOM_FIXTURE_TMPDIR/.codegraph/.stub-phantom-ref"
echo '{"results": []}' > "$FR303_PHANTOM_FIXTURE_TMPDIR/.codegraph/.stub-codegraph-search-empty"

if [ ! -d "$FR303_PHANTOM_FIXTURE_TMPDIR/.codegraph" ] \
   || [ ! -s "$FR303_PHANTOM_FIXTURE_TMPDIR/.codegraph/.stub-mcp-tool" ] \
   || [ ! -s "$FR303_PHANTOM_FIXTURE_TMPDIR/.codegraph/.stub-phantom-ref" ] \
   || [ ! -s "$FR303_PHANTOM_FIXTURE_TMPDIR/.codegraph/.stub-codegraph-search-empty" ]; then
  log_error "Failed to mock FR-303 phantom fixture at $FR303_PHANTOM_FIXTURE_TMPDIR"
  rm -rf "$FR303_PHANTOM_FIXTURE_TMPDIR"
  exit 1
fi
log_info "Mocked FR-303 fixture at: $FR303_PHANTOM_FIXTURE_TMPDIR (phantom $FIXTURE_PHANTOM_REF + empty search stub)"

# Anchors that prove the prompt instructs the decomposer to attach the
# advisory annotation comment AND bound it from altering body/desc/AC or
# priority/type. These extend T5.5's FR-303 anchors — the full triplet,
# the priority/type clause, the "advisory only" classifier, and the
# silent-omission defensive posture all live in a single sentence at line
# 30 of prd-decompose-prompt.md (the "advisory only" paragraph), so wording
# drift on any of them breaks this check.
FR303_PHANTOM_PHRASES=(
  'never alters the issue body / description / acceptance_criteria'
  '`--priority` or `--type`'
  'advisory only'
  'omit the comment silently'
  'graph is partial'
  'Investigate step (Ralph step 4)'
)

for prompt_file in "${PRD_DECOMPOSE_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    rm -rf "$FR303_PHANTOM_FIXTURE_TMPDIR"
    exit 1
  fi
  for phrase in "${FR303_PHANTOM_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" "$prompt_file"; then
      log_error "FR-303 advisory-bounds phrase missing in: $prompt_file"
      log_error "Expected verbatim: $phrase"
      rm -rf "$FR303_PHANTOM_FIXTURE_TMPDIR"
      exit 2
    fi
  done
  log_info "Verified FR-303 advisory-bounds contract in: $(basename "$prompt_file")"
done

rm -rf "$FR303_PHANTOM_FIXTURE_TMPDIR"

log_info "FR-303 phantom-annotation test PASSED — both prompt files lock the full advisory triplet (body/desc/AC + priority/type), 'advisory only' classifier, and silent-omission defensive posture"

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
# Static check: FR-304 resolved-symbol Likely files lands; advisory bounds
#               (description placement + derivation contract + silent skip)
# ============================================================================
# T5.7 — when codegraph_available is true and a work item references a real
# symbol (one that DOES resolve in the graph, e.g., GitConfigContext at
# extensions/context.py in the fixture PRD), the FR-304 sub-paragraph must
# instruct the decomposer to:
#   1. Before bd create of the work item's issue, append a one-line
#      `**Likely files**: <file-1>, <file-2>, ...` shortlist to the issue's
#      DESCRIPTION (not a comment) listing the resolved file path(s).
#   2. Derive files from the resolved-set's <symbol>@<file> records (FR-302),
#      deduplicating paths and preserving first-appearance order.
#   3. Keep the description-vs-comment distinction explicit: shortlist lands
#      in description (canonical starting scope for Investigate step 4), the
#      FR-303 resolved/unresolved sets land in a comment (advisory hint).
#   4. Skip silently when no references resolve, codegraph_search errors, or
#      the graph is partial — defensive posture parity with the rest of the
#      block.
#
# Locks the FR-304 resolved-symbol contract beyond T5.5's general FR-304
# anchors. Couples explicitly to the fixture's GitConfigContext +
# extensions/context.py so a regression on the resolved-symbol path
# surfaces here. Static byte-check on both prompt files.

log_step "Static check: FR-304 resolved-symbol Likely files lands; advisory bounds (description placement + derivation contract + silent skip)"

# Mock codegraph_available environment: .codegraph/ + stub mcp__codegraph__codegraph_search
# (the tool the FR-301..304 sub-paragraph invokes per extracted reference).
FR304_RESOLVED_FIXTURE_TMPDIR="$(mktemp -d)"
mkdir -p "$FR304_RESOLVED_FIXTURE_TMPDIR/.codegraph"
echo "mcp__codegraph__codegraph_search" > "$FR304_RESOLVED_FIXTURE_TMPDIR/.codegraph/.stub-mcp-tool"

# Resolved-symbol fixture: $FIXTURE_REAL_SYMBOL ($FIXTURE_REAL_PATH).
# The stub codegraph_search response returns one match, so FR-302
# partitions the reference into the *resolved* bucket; FR-304 must then
# emit a `**Likely files**: <real-path>` shortlist into the issue's
# description (not a comment) before bd create.
echo "$FIXTURE_REAL_SYMBOL" > "$FR304_RESOLVED_FIXTURE_TMPDIR/.codegraph/.stub-real-symbol"
echo "$FIXTURE_REAL_PATH"   > "$FR304_RESOLVED_FIXTURE_TMPDIR/.codegraph/.stub-real-path"
printf '{"results":[{"symbol":"%s","file":"%s"}]}\n' \
  "$FIXTURE_REAL_SYMBOL" "$FIXTURE_REAL_PATH" \
  > "$FR304_RESOLVED_FIXTURE_TMPDIR/.codegraph/.stub-codegraph-search-real"

if [ ! -d "$FR304_RESOLVED_FIXTURE_TMPDIR/.codegraph" ] \
   || [ ! -s "$FR304_RESOLVED_FIXTURE_TMPDIR/.codegraph/.stub-mcp-tool" ] \
   || [ ! -s "$FR304_RESOLVED_FIXTURE_TMPDIR/.codegraph/.stub-real-symbol" ] \
   || [ ! -s "$FR304_RESOLVED_FIXTURE_TMPDIR/.codegraph/.stub-real-path" ] \
   || [ ! -s "$FR304_RESOLVED_FIXTURE_TMPDIR/.codegraph/.stub-codegraph-search-real" ]; then
  log_error "Failed to mock FR-304 resolved fixture at $FR304_RESOLVED_FIXTURE_TMPDIR"
  rm -rf "$FR304_RESOLVED_FIXTURE_TMPDIR"
  exit 1
fi
log_info "Mocked FR-304 resolved fixture at: $FR304_RESOLVED_FIXTURE_TMPDIR ($FIXTURE_REAL_SYMBOL → $FIXTURE_REAL_PATH)"

# Anchors that prove the prompt instructs the decomposer to emit the
# Likely files shortlist into the description (not a comment) before
# bd create when at least one reference resolves. These extend T5.5's
# FR-304 anchors with: the verbatim template format, the temporal
# placement (before bd create), the description-vs-comment distinction,
# the derivation contract (from resolved-set <symbol>@<file>), the
# first-appearance order, the Investigate-step-4 canonical-scope purpose,
# and the silent-skip posture — all live in the FR-304 paragraph
# (lines 32-38 of prd-decompose-prompt.md), so wording drift on any of
# them breaks this check.
FR304_RESOLVED_PHRASES=(
  '<file-1>, <file-2>, ...'
  'before issuing `bd create`'
  '(not a comment)'
  'from the resolved-set'\''s `<symbol>@<file>` records'
  'preserve first-appearance order'
  'step 4 (Investigate) treats it as canonical starting scope'
  'description-vs-comment distinction explicit'
  'skip silently when no references resolve'
)

for prompt_file in "${PRD_DECOMPOSE_PROMPT_FILES[@]}"; do
  if [ ! -f "$prompt_file" ]; then
    log_error "Prompt file not found: $prompt_file"
    rm -rf "$FR304_RESOLVED_FIXTURE_TMPDIR"
    exit 1
  fi
  for phrase in "${FR304_RESOLVED_PHRASES[@]}"; do
    if ! grep -F -q -- "$phrase" "$prompt_file"; then
      log_error "FR-304 resolved-symbol phrase missing in: $prompt_file"
      log_error "Expected verbatim: $phrase"
      rm -rf "$FR304_RESOLVED_FIXTURE_TMPDIR"
      exit 2
    fi
  done
  log_info "Verified FR-304 resolved-symbol contract in: $(basename "$prompt_file")"
done

rm -rf "$FR304_RESOLVED_FIXTURE_TMPDIR"

log_info "FR-304 resolved-symbol test PASSED — both prompt files lock the description placement (not a comment, before bd create), the derivation contract (from resolved-set <symbol>@<file> with first-appearance order), the Investigate-step-4 canonical-scope purpose, and the silent-skip posture"

# ============================================================================
# Summary
# ============================================================================

log_step "All Tests Passed!"
echo ""
log_info "Fixture PRD presence: tests/fixtures/prd-decompose-fixture.md ✓"
log_info "FR-301 reference-extraction patterns: ✓"
log_info "FR-302 codegraph_search per reference + main-session forbiddance: ✓"
log_info "FR-303 advisory **CodeGraph references** annotation comment: ✓"
log_info "FR-303 phantom annotation lands; advisory bounds (body/desc/AC + priority/type): ✓"
log_info "FR-304 **Likely files** shortlist in description: ✓"
log_info "FR-304 resolved-symbol Likely files lands; advisory bounds (description placement + derivation contract + silent skip): ✓"
echo ""
log_info "T5.5 + T5.6 + T5.7 lock the FR-301..304 contract via static checks coupled to the fixture PRD."

if [ "$KEEP_PROJECT" = true ]; then
  log_info "Fixture preserved at: $FIXTURE_PRD"
fi

exit 0
