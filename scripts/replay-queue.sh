#!/usr/bin/env bash
# replay-queue.sh - One-shot replay harness for the ralph.sh <-> goal.sh comparison (FR-023).
#
# Usage: ./scripts/replay-queue.sh [options]
#
# Options:
#   --queue-spec PATH       Shell-sourceable file of bd-create commands defining the
#                           replay queue (default: scripts/queues/baseline-20.txt).
#   --tmpdir PATH           Working directory for the two orchestrator clones
#                           (default: $(mktemp -d -t ortus-replay-XXXX)).
#   --output PATH           CSV output file
#                           (default: reports/replay-<YYYYMMDD-HHMMSS>.csv).
#   --orchestrator NAME     One of: ralph, goal, both (default: both).
#   --copier-template PATH  Template directory to clone with `copier copy`
#                           (default: <repo-root>/template).
#   --max-tasks N           Pass --tasks N to each orchestrator (default: 20,
#                           matching the baseline queue size).
#   --timeout SEC           Per-orchestrator timeout in seconds
#                           (default: 7200 = 2h; lets a 20-issue queue finish
#                           even under a slow Haiku evaluator).
#   --dry-run               Print the planned actions and exit 0 without
#                           spawning copier, bd, or any orchestrator.
#   -h, --help              Show this help and exit.
#
# Output schema (CSV columns):
#   orchestrator,issue_id,wall_clock_sec,turn_n,input_tokens
#
# Per-issue wall-clock is reported once per issue (turn_n=ALL, input_tokens=NA);
# per-turn input_tokens are reported once per stream-json assistant turn
# (wall_clock_sec=NA). Failure rows use wall_clock_sec=NA,input_tokens=NA and
# carry the issue_id "__CRASHED__" so downstream tooling can filter them out.
#
# The harness runs both orchestrators sequentially against IDENTICAL clones
# (same template generation, same seeded bd queue, same git remote shape).
# It does not run them in parallel — they share .beads/ralph.flock when
# pointed at the same repo, and even with separate tmpdirs sequential runs
# keep CPU/memory contention out of the wall-clock measurement.
#
# This script lives outside template/ on purpose: it is a measurement tool
# for the ortus repo itself (PRD §Phase 3 / E5), not part of the generated
# project surface. See scripts/check-ortus-parity.sh for why measurement
# tooling does not need a template mirror.

set -u

# ----- Defaults --------------------------------------------------------------

QUEUE_SPEC=""
TMPDIR_ARG=""
OUTPUT=""
ORCHESTRATOR="both"
COPIER_TEMPLATE=""
MAX_TASKS=20
TIMEOUT=7200
DRY_RUN=""

# ----- Argument parsing ------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case $1 in
    --queue-spec) QUEUE_SPEC="$2"; shift 2 ;;
    --tmpdir) TMPDIR_ARG="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    --orchestrator) ORCHESTRATOR="$2"; shift 2 ;;
    --copier-template) COPIER_TEMPLATE="$2"; shift 2 ;;
    --max-tasks) MAX_TASKS="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,/^[^#]/{/^#/{s/^# \?//;p;}}' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; echo "Run '$0 -h' for usage." >&2; exit 2 ;;
  esac
done

case "$ORCHESTRATOR" in
  ralph|goal|both) ;;
  *) echo "--orchestrator must be one of: ralph, goal, both (got: $ORCHESTRATOR)" >&2; exit 2 ;;
esac

# ----- Resolve defaults that need filesystem state ---------------------------

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)

[ -z "$QUEUE_SPEC" ] && QUEUE_SPEC="$repo_root/scripts/queues/baseline-20.txt"
[ -z "$COPIER_TEMPLATE" ] && COPIER_TEMPLATE="$repo_root/template"
[ -z "$OUTPUT" ] && OUTPUT="$repo_root/reports/replay-$(date '+%Y%m%d-%H%M%S').csv"
if [ -z "$TMPDIR_ARG" ]; then
  TMPDIR_ARG=$(mktemp -d -t ortus-replay-XXXX 2>/dev/null \
    || mktemp -d "${TMPDIR:-/tmp}/ortus-replay-XXXX")
fi

# Canonicalize once so dry-run output and runtime use the same absolute paths.
QUEUE_SPEC=$(readlink -f "$QUEUE_SPEC" 2>/dev/null || echo "$QUEUE_SPEC")
COPIER_TEMPLATE=$(readlink -f "$COPIER_TEMPLATE" 2>/dev/null || echo "$COPIER_TEMPLATE")
OUTPUT_DIR=$(dirname "$OUTPUT")
TMPDIR_ARG=$(readlink -f "$TMPDIR_ARG" 2>/dev/null || echo "$TMPDIR_ARG")

# ----- Preflight: queue spec must exist and look sane ------------------------

if [ ! -f "$QUEUE_SPEC" ]; then
  echo "replay-queue: queue spec not found: $QUEUE_SPEC" >&2
  exit 1
fi

# Count bd create lines in the queue spec. The script accepts both
# one-issue-per-line and shell-continued (\ at EOL) forms; collapse the
# continuations first, then grep.
QUEUE_CREATE_COUNT=$(awk '
  /\\$/  { sub(/\\$/, ""); buf = buf $0; next }
  /./    { print buf $0; buf = "" }
  END    { if (buf != "") print buf }
' "$QUEUE_SPEC" | grep -cE '^[[:space:]]*bd[[:space:]]+create([[:space:]]|$)')

if [ "$QUEUE_CREATE_COUNT" -eq 0 ]; then
  echo "replay-queue: queue spec contains no 'bd create' lines: $QUEUE_SPEC" >&2
  exit 1
fi

# ----- Helpers ---------------------------------------------------------------

# bd-create idempotency would be nice but the orchestrators are the system
# under test; if they double-spawn, that is itself signal. Don't gate on it.

build_orchestrator_argv() {
  # build_orchestrator_argv ralph|goal <project-dir>
  # Echoes the argv that would be exec'd against the orchestrator clone.
  local orch="$1"
  local proj="$2"
  case "$orch" in
    ralph) printf '%s\n' "$proj/ortus/ralph.sh" "--tasks" "$MAX_TASKS" ;;
    goal)  printf '%s\n' "$proj/ortus/goal.sh"  "--tasks" "$MAX_TASKS" ;;
    *) echo "build_orchestrator_argv: unknown orchestrator: $orch" >&2; return 1 ;;
  esac
}

clone_to() {
  # clone_to <dest> — render the template into <dest> with copier --defaults
  # and a baseline answer set. The answers cover every required copier.yaml
  # prompt so --defaults can hydrate non-defaulted ones too (project_name has
  # no default; github_username defaults to empty which is fine here).
  local dest="$1"
  copier copy --defaults --trust \
    --data project_name=ortus-replay \
    --data github_username=ortus-replay \
    --data author_name="Ortus Replay" \
    --data author_email="replay@ortus.local" \
    --data project_description="Replay-harness clone for goal-vs-ralph measurement" \
    "$COPIER_TEMPLATE" "$dest"
}

stage_remote() {
  # stage_remote <project-dir>
  # Initialize a local bare repo and wire it as origin so the orchestrators'
  # `git push` paths stay active (mirroring real-world generated projects).
  # File-URL remote = no network, no auth, no flakiness.
  local proj="$1"
  local bare="$TMPDIR_ARG/remotes/$(basename "$proj").git"
  mkdir -p "$bare"
  git -C "$bare" init --bare --quiet
  git -C "$proj" remote remove origin 2>/dev/null || true
  git -C "$proj" remote add origin "file://$bare"
}

seed_queue() {
  # seed_queue <project-dir>
  # Source the queue spec inside the project dir so its `bd create` calls
  # land in the project's own beads DB. Use bash -e so a malformed line in
  # the spec surfaces immediately rather than silently dropping issues.
  local proj="$1"
  (cd "$proj" && bash -e "$QUEUE_SPEC")
}

run_orchestrator() {
  # run_orchestrator <ralph|goal> <project-dir>
  # Runs the orchestrator with /usr/bin/time -f '%e' capturing the wall-clock
  # to <project-dir>/wallclock.txt, and tees stream-json to logs/<orch>-*.log
  # already (the orchestrator does that itself). Returns 0 on clean exit,
  # non-zero on crash/timeout.
  local orch="$1"
  local proj="$2"
  local argv
  mapfile -t argv < <(build_orchestrator_argv "$orch" "$proj")
  local wallclock="$proj/wallclock.txt"
  # `timeout --preserve-status` keeps the orchestrator's exit code visible
  # when it exits cleanly within the budget; on timeout, exit is 124.
  /usr/bin/time -f '%e' -o "$wallclock" \
    timeout --preserve-status -s TERM "$TIMEOUT" \
    "${argv[@]}"
}

parse_stream_json() {
  # parse_stream_json <log-file> <orchestrator-name>
  # Emits CSV rows for every assistant turn carrying a usage.input_tokens
  # field. jq tolerates the line-delimited stream-json format. Lines that
  # are not JSON (the orchestrator's [timestamp] log lines, the startup
  # banner) are skipped via `--input-filename /dev/null` + try/catch wrapping
  # at the jq layer.
  local log="$1"
  local orch="$2"
  local issue="$3"  # ALL when summarizing, or a specific bd id when known
  # The line-delimited form: each event is one JSON document. We strip
  # non-JSON noise with grep -E '^{' first, then let jq pick out usage
  # entries. The `// empty` guard suppresses output for events without a
  # usage block (most stream-json events).
  grep -E '^[[:space:]]*\{' "$log" 2>/dev/null \
    | jq -r --arg orch "$orch" --arg issue "$issue" '
        (.message.usage.input_tokens // .usage.input_tokens // empty) as $tokens
        | select($tokens != null and $tokens != "")
        | [$orch, $issue, "NA", (input_line_number | tostring), ($tokens | tostring)]
        | @csv
      ' 2>/dev/null \
    || true
}

emit_wallclock_row() {
  # emit_wallclock_row <orch> <project-dir> <issue-id>
  # Reads the wall-clock recorded by run_orchestrator and emits one CSV
  # row per replay. wallclock.txt may be missing if the orchestrator was
  # killed before time(1) finished — in that case emit a CRASHED row so
  # the absence is explicit in the CSV rather than silently dropped.
  local orch="$1"
  local proj="$2"
  local issue="$3"
  local wallclock="$proj/wallclock.txt"
  if [ ! -s "$wallclock" ]; then
    printf '"%s","%s","%s","%s","%s"\n' "$orch" "__CRASHED__" "NA" "ALL" "NA"
    return
  fi
  # `/usr/bin/time -f '%e'` prints just the elapsed-seconds float on its
  # own line. Guard against multi-line output (some time variants prepend
  # an error message) by taking the last numeric token.
  local secs
  secs=$(awk '/^[[:space:]]*[0-9]+(\.[0-9]+)?[[:space:]]*$/ { v=$1 } END { print v }' "$wallclock")
  [ -z "$secs" ] && secs="NA"
  printf '"%s","%s","%s","%s","%s"\n' "$orch" "$issue" "$secs" "ALL" "NA"
}

# ----- Dry-run path ----------------------------------------------------------

if [ -n "$DRY_RUN" ]; then
  echo "=== replay-queue: planned actions (dry-run; no commands executed) ==="
  echo "Queue spec        : $QUEUE_SPEC"
  echo "  bd-create count : $QUEUE_CREATE_COUNT (target: 20)"
  echo "Copier template   : $COPIER_TEMPLATE"
  echo "Tmpdir            : $TMPDIR_ARG"
  echo "Output CSV        : $OUTPUT"
  echo "Output directory  : $OUTPUT_DIR (will be mkdir -p)"
  echo "Orchestrators     : $ORCHESTRATOR"
  echo "Max tasks/orch    : $MAX_TASKS"
  echo "Timeout/orch      : ${TIMEOUT}s"
  echo ""
  for orch in ralph goal; do
    case "$ORCHESTRATOR" in both|$orch) ;; *) continue ;; esac
    proj="$TMPDIR_ARG/$orch"
    echo "[$orch] plan:"
    echo "  1. copier copy --defaults --trust $COPIER_TEMPLATE $proj"
    echo "  2. git init bare $TMPDIR_ARG/remotes/$(basename "$proj").git"
    echo "  3. git -C $proj remote add origin file://$TMPDIR_ARG/remotes/$(basename "$proj").git"
    echo "  4. (cd $proj && bash -e $QUEUE_SPEC)   # seeds $QUEUE_CREATE_COUNT bd issues"
    mapfile -t argv < <(build_orchestrator_argv "$orch" "$proj")
    printf "  5. /usr/bin/time -f %%e timeout --preserve-status -s TERM ${TIMEOUT}s"
    printf ' %q' "${argv[@]}"
    printf '\n'
    echo "  6. parse $proj/logs/${orch}-*.log -> per-turn usage.input_tokens CSV rows"
    echo "  7. emit wall-clock summary row"
    echo ""
  done
  echo "CSV schema: orchestrator,issue_id,wall_clock_sec,turn_n,input_tokens"
  echo "Failure rows: issue_id=__CRASHED__, wall_clock_sec=NA, input_tokens=NA"
  exit 0
fi

# ----- Runtime path ----------------------------------------------------------

mkdir -p "$OUTPUT_DIR" "$TMPDIR_ARG"
echo "orchestrator,issue_id,wall_clock_sec,turn_n,input_tokens" > "$OUTPUT"

for orch in ralph goal; do
  case "$ORCHESTRATOR" in both|$orch) ;; *) continue ;; esac
  proj="$TMPDIR_ARG/$orch"
  echo "=== replay-queue: setting up $orch clone at $proj ==="
  if ! clone_to "$proj"; then
    echo "replay-queue: copier copy failed for $orch; recording CRASHED row" >&2
    emit_wallclock_row "$orch" "$proj" "__SETUP_FAILED__" >> "$OUTPUT"
    continue
  fi
  stage_remote "$proj"
  if ! seed_queue "$proj"; then
    echo "replay-queue: queue seed failed for $orch; recording CRASHED row" >&2
    emit_wallclock_row "$orch" "$proj" "__SEED_FAILED__" >> "$OUTPUT"
    continue
  fi

  echo "=== replay-queue: running $orch (timeout ${TIMEOUT}s) ==="
  # We deliberately do NOT `set -e` around run_orchestrator — the AC requires
  # graceful handling of a crashing orchestrator so the other still runs.
  rc=0
  run_orchestrator "$orch" "$proj" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "replay-queue: $orch exited rc=$rc; recording row but continuing" >&2
  fi

  emit_wallclock_row "$orch" "$proj" "ALL" >> "$OUTPUT"
  for log in "$proj"/logs/${orch}-*.log; do
    [ -e "$log" ] || continue
    parse_stream_json "$log" "$orch" "ALL" >> "$OUTPUT"
  done
done

echo "=== replay-queue: complete ==="
echo "CSV output: $OUTPUT"
echo "Tmpdir:     $TMPDIR_ARG (preserve for inspection; rm -rf when done)"
exit 0
