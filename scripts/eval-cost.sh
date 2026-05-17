#!/usr/bin/env bash
# eval-cost.sh — Reduce goal-*.log stream-json into a Q3 evaluator/main PASS/FAIL block.
#
# Usage: ./scripts/eval-cost.sh [options]
#
# Options:
#   --log PATH               One or more goal-*.log files to aggregate. Repeatable.
#                            (default: most recent logs/goal-*.log)
#   --threshold PCT          PASS threshold for evaluator/main token ratio, percent.
#                            (default: 5; design threshold "<5%
#                            of main spend" as the "negligible" threshold.)
#   --report-format md|txt   Output format (default: md). md emits a headed Q3
#                            section suitable for direct paste into
#                            reports/goal-vs-ralph-<date>.md; txt strips markdown
#                            but preserves PASS/FAIL wording.
#   -h, --help               Show this help and exit.
#
# Output: a Q3 block written to stdout. PASS/FAIL wording satisfies the
# ortus-bn4a.4 AC test (a) regex `Q3|evaluator cost|negligible` (case-insensitive).
#
# Stream-json shape (per Claude Code v2.1.139+; matches logs/ralph-*.log samples):
#   one NDJSON event per line, each tagged with {type, subtype, message?, ...}.
#   Assistant turns carry `.message.model` and `.message.usage.input_tokens`.
#   Main model: `claude-opus-4-*` / `claude-sonnet-4-*` (anything not Haiku).
#   Evaluator: `claude-haiku-4-*` — by design, the /goal
#   subagent runs on Haiku to keep the post-turn judgment "negligible" in cost.
#
# Discriminator (documented per ortus-xkad design field):
#   evaluator iff (.message.model) matches regex /haiku/i.
#   Stable across stream-json schema versions because the model name itself is
#   the load-bearing signal; finer-grained tags (e.g., .subtype="evaluator") may
#   shift between Claude Code releases, but the model field has not.
#
# Edge cases:
#   - No main turns: FAIL with reason "no main turns; insufficient data".
#   - No evaluator turns (but main turns present): ratio = 0%, PASS by absence
#     — clean degradation matching replay-reduce.sh's all-crashed branch.
#   - Stream-json lines that fail to parse: skipped silently (logs may contain
#     partial lines from interrupted runs).
#   - jq missing on $PATH: clear stderr error, exit 1.
#   - Log file missing or empty: clear stderr error, exit 1.
#
# This script lives outside template/ on purpose: it is a measurement tool for
# the ortus repo itself, not part of the generated project
# surface — same reasoning as scripts/replay-queue.sh and scripts/replay-reduce.sh.

set -u

THRESHOLD=5
REPORT_FORMAT=md
LOGS=()

while [[ $# -gt 0 ]]; do
  case $1 in
    --log) LOGS+=("$2"); shift 2 ;;
    --threshold) THRESHOLD="$2"; shift 2 ;;
    --report-format) REPORT_FORMAT="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^[^#]/{/^#/{s/^# \?//;p;}}' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; echo "Run '$0 -h' for usage." >&2; exit 2 ;;
  esac
done

case "$REPORT_FORMAT" in
  md|txt) ;;
  *) echo "--report-format must be md or txt (got: $REPORT_FORMAT)" >&2; exit 2 ;;
esac

# Accept both integer and decimal thresholds (e.g., 5, 5.0, 0.5).
if ! [[ "$THRESHOLD" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  echo "--threshold must be a non-negative number (got: $THRESHOLD)" >&2
  exit 2
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "eval-cost: jq is required but not on \$PATH" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)

if [ "${#LOGS[@]}" -eq 0 ]; then
  # Pick the most recent logs/goal-*.log. ls -t orders by mtime, desc; head -1 → newest.
  default_log=$(ls -t "$repo_root"/logs/goal-*.log 2>/dev/null | head -n 1)
  if [ -z "$default_log" ]; then
    echo "eval-cost: no --log given and no logs/goal-*.log found under $repo_root" >&2
    exit 1
  fi
  LOGS=("$default_log")
fi

for log in "${LOGS[@]}"; do
  if [ ! -f "$log" ]; then
    echo "eval-cost: log not found: $log" >&2
    exit 1
  fi
done

# Extract (model, input_tokens) tuples for every assistant event across all logs.
# `jq -R 'fromjson?'` tolerates partial/non-JSON lines (skip silently). `select`
# requires both a non-empty .message.model and a numeric .message.usage.input_tokens.
extract_tuples() {
  for log in "${LOGS[@]}"; do
    jq -R --raw-output --slurp '
      split("\n")
      | map(fromjson? // empty)
      | map(select(.type == "assistant"
                   and (.message.model // "") != ""
                   and (.message.usage.input_tokens // null) != null))
      | map([.message.model, (.message.usage.input_tokens // 0)] | @tsv)
      | .[]
    ' "$log"
  done
}

TUPLES=$(extract_tuples)

# Awk computes the aggregates and renders the report. LC_ALL=C pins decimal parsing.
echo "$TUPLES" | LC_ALL=C awk -F'\t' \
    -v threshold="$THRESHOLD" \
    -v fmt="$REPORT_FORMAT" \
    -v logs_csv="$(IFS=, ; echo "${LOGS[*]}")" '
BEGIN {
  eval_count = 0; main_count = 0
  eval_tokens = 0; main_tokens = 0
}
NF >= 2 {
  model = $1
  tokens = $2 + 0
  if (model ~ /haiku/) {
    eval_count++
    eval_tokens += tokens
  } else {
    main_count++
    main_tokens += tokens
  }
}
END {
  status = ""; reason = ""; ratio_pct = 0
  if (main_count == 0 && eval_count == 0) {
    status = "FAIL"
    reason = "no assistant turns found in log(s); insufficient data"
  } else if (main_count == 0) {
    status = "FAIL"
    reason = "no main turns; insufficient data"
  } else if (eval_count == 0) {
    status = "PASS"
    ratio_pct = 0
    reason = "no evaluator-tagged events found; ratio = 0%; PASS by absence"
  } else if (main_tokens <= 0) {
    status = "FAIL"
    reason = "main input_tokens sum is 0; cannot compute ratio"
  } else {
    ratio_pct = 100.0 * eval_tokens / main_tokens
    status = (ratio_pct <= threshold + 0) ? "PASS" : "FAIL"
  }

  if (fmt == "md") {
    print "## Q3 — Haiku evaluator cost (evaluator/main input-token ratio)"
    print ""
    printf "Threshold: evaluator/main <= %s%%\n", threshold
    print ""
    print "| Bucket | Turns | Input tokens |"
    print "|---|---|---|"
    printf "| evaluator (haiku) | %d | %d |\n", eval_count, eval_tokens
    printf "| main              | %d | %d |\n", main_count, main_tokens
    print ""
    if (status == "PASS" && eval_count == 0 && main_count > 0) {
      printf "Observed ratio: 0.00%% — no evaluator-tagged events found\n"
      printf "Result: **Q3 PASS** — evaluator cost negligible (PASS by absence)\n"
    } else if (status == "PASS" || (status == "FAIL" && main_count > 0 && eval_count > 0)) {
      printf "Observed ratio: %d / %d = %.2f%%\n", eval_tokens, main_tokens, ratio_pct
      printf "Result: **Q3 %s** (%.2f%% %s %s%%)\n", status, ratio_pct, (status == "PASS" ? "<=" : ">"), threshold
    } else {
      printf "Observed ratio: NA — %s\n", reason
      printf "Result: **Q3 %s** — %s\n", status, reason
    }
    print ""
    print "---"
    printf "Raw data: %s\n", logs_csv
    print  "Generated by: scripts/eval-cost.sh"
  } else {
    # txt format: strip markdown, keep PASS/FAIL phrasing intact for AC regex.
    print "Q3 - Haiku evaluator cost (evaluator/main input-token ratio)"
    printf "  Threshold: evaluator/main <= %s%%\n", threshold
    printf "  evaluator (haiku): turns=%d, input_tokens=%d\n", eval_count, eval_tokens
    printf "  main             : turns=%d, input_tokens=%d\n", main_count, main_tokens
    if (status == "PASS" && eval_count == 0 && main_count > 0) {
      printf "  ratio: 0.00%% (no evaluator-tagged events; PASS by absence)\n"
      printf "  Q3 PASS - evaluator cost negligible\n"
    } else if (status == "PASS" || (status == "FAIL" && main_count > 0 && eval_count > 0)) {
      printf "  ratio: %.2f%%\n", ratio_pct
      printf "  Q3 %s\n", status
    } else {
      printf "  Q3 %s - %s\n", status, reason
    }
    printf "  Raw data: %s\n", logs_csv
  }
}
'
