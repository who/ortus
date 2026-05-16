#!/usr/bin/env bash
# replay-reduce.sh — Reduce reports/replay-*.csv into an M1 + M3 PASS/FAIL report.
#
# Usage: ./scripts/replay-reduce.sh [options]
#
# Options:
#   --csv PATH               CSV emitted by scripts/replay-queue.sh
#                            (default: most recent reports/replay-*.csv).
#   --window-size N          Smart-zone window size in tokens for M3 utilization
#                            (default: 200000; pass 1000000 for the 1M-context beta).
#   --report-format md|txt   Output format (default: md). md emits headed sections
#                            suitable for direct paste into reports/goal-vs-ralph-<date>.md;
#                            txt strips markdown but preserves PASS/FAIL wording.
#   -h, --help               Show this help and exit.
#
# Output: M1 and M3 sections written to stdout. PASS/FAIL wording satisfies the
# ortus-bn4a.2 AC tests (a) 'M1.*pass|M1.*fail' and (b) 'M3.*pass|M3.*fail'
# (case-insensitive). Pipe to a .md file or paste verbatim into the dated report.
#
# CSV schema (from scripts/replay-queue.sh:25-31):
#   orchestrator,issue_id,wall_clock_sec,turn_n,input_tokens
# Per-issue rows: turn_n=ALL, input_tokens=NA (used for M1 median wall-clock).
# Per-turn rows : wall_clock_sec=NA, turn_n=<int>, input_tokens=<int> (used for M3).
# Failure rows  : issue_id="__CRASHED__" (skipped; see also __SETUP_FAILED__,
#                 __SEED_FAILED__ produced by replay-queue.sh).
#
# Computation:
#   M1 — per-orchestrator median of wall_clock_sec; ratio = goal_median / ralph_median.
#        PASS iff ratio <= 0.70. Even N => average of the two middle values;
#        odd N => the middle value.
#   M3 — utilization = input_tokens / window_size for every per-turn row across all
#        orchestrators. Sort; nearest-rank percentile (idx = int(p * N), clamped to
#        [1, N]) — pinned to the formula in the bn4a.2 19:28 operator runbook so
#        the reducer's output matches what the runbook produced when run by hand.
#        PASS iff p95 <= 0.60 AND p100 <= 0.80; each threshold is reported
#        independently so partial failures are visible.
#
# This script lives outside template/ on purpose: it is a measurement tool for
# the ortus repo itself (PRD §Phase 3 / E5), not part of the generated project
# surface — same reasoning as scripts/replay-queue.sh.

set -u

CSV=""
WINDOW_SIZE=200000
REPORT_FORMAT=md

while [[ $# -gt 0 ]]; do
  case $1 in
    --csv) CSV="$2"; shift 2 ;;
    --window-size) WINDOW_SIZE="$2"; shift 2 ;;
    --report-format) REPORT_FORMAT="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^[^#]/{/^#/{s/^# \?//;p;}}' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; echo "Run '$0 -h' for usage." >&2; exit 2 ;;
  esac
done

case "$REPORT_FORMAT" in
  md|txt) ;;
  *) echo "--report-format must be md or txt (got: $REPORT_FORMAT)" >&2; exit 2 ;;
esac

if ! [[ "$WINDOW_SIZE" =~ ^[1-9][0-9]*$ ]]; then
  echo "--window-size must be a positive integer (got: $WINDOW_SIZE)" >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)

if [ -z "$CSV" ]; then
  # Pick the most recent reports/replay-*.csv. ls -t orders by mtime, desc.
  # head -1 → newest; empty if no matches (ls -t to /dev/null swallows the
  # "no such file or directory" warning).
  CSV=$(ls -t "$repo_root"/reports/replay-*.csv 2>/dev/null | head -n 1)
  if [ -z "$CSV" ]; then
    echo "replay-reduce: no CSV given and no reports/replay-*.csv found under $repo_root" >&2
    exit 1
  fi
fi

if [ ! -f "$CSV" ]; then
  echo "replay-reduce: CSV not found: $CSV" >&2
  exit 1
fi

data_rows=$(awk 'NR>1 && NF>0' "$CSV" | wc -l)
if [ "$data_rows" -eq 0 ]; then
  echo "replay-reduce: CSV has no data rows: $CSV" >&2
  exit 1
fi

# LC_ALL=C pins awk's numeric parsing to "." as the decimal separator regardless
# of the operator's locale — replay-queue.sh emits "." for wall_clock_sec.
LC_ALL=C awk -F',' \
    -v window="$WINDOW_SIZE" \
    -v csv_path="$CSV" \
    -v fmt="$REPORT_FORMAT" '
function median(arr, n,    s, i, j, t) {
  if (n == 0) return -1
  for (i = 1; i <= n; i++) s[i] = arr[i]
  for (i = 1; i <= n; i++)
    for (j = i + 1; j <= n; j++)
      if (s[i] > s[j]) { t = s[i]; s[i] = s[j]; s[j] = t }
  if (n % 2 == 1) return s[(n + 1) / 2]
  return (s[n / 2] + s[n / 2 + 1]) / 2
}
function percentile_idx(p, n,    idx) {
  # Nearest-rank: idx = int(p * n), clamped to [1, n]. Matches the awk one-liner
  # in the ortus-bn4a.2 19:28 operator runbook (u[int(NR*0.95)]) so the reducer
  # produces the same M3 numbers the runbook produces when run by hand.
  idx = int(p * n)
  if (idx < 1) idx = 1
  if (idx > n) idx = n
  return idx
}
function sort_in_place(arr, n,    i, j, t) {
  for (i = 1; i <= n; i++)
    for (j = i + 1; j <= n; j++)
      if (arr[i] > arr[j]) { t = arr[i]; arr[i] = arr[j]; arr[j] = t }
}
BEGIN { ralph_n = 0; goal_n = 0; total_perturn = 0 }
NR == 1 { next }   # skip CSV header
{
  # CSV is double-quote-wrapped on data rows (printf and jq @csv both quote).
  # Strip every quote, then split on comma — none of the schema values
  # legitimately contain a comma or quote.
  gsub(/"/, "")
  n = split($0, F, ",")
  if (n < 5) next

  orch   = F[1]
  issue  = F[2]
  wall   = F[3]
  turn   = F[4]
  tokens = F[5]

  # Filter sentinel issue ids: __CRASHED__, __SETUP_FAILED__, __SEED_FAILED__.
  if (issue ~ /^__/) next

  if (turn == "ALL") {
    if (wall == "NA" || wall == "") next
    if (orch == "ralph") { ralph[++ralph_n] = wall + 0 }
    else if (orch == "goal") { goal[++goal_n] = wall + 0 }
  } else {
    if (turn !~ /^[0-9]+$/) next
    if (tokens == "NA" || tokens == "") next
    if (tokens !~ /^[0-9]+$/) next
    perturn[++total_perturn] = (tokens + 0) / window
  }
}
END {
  ralph_med = median(ralph, ralph_n)
  goal_med  = median(goal,  goal_n)

  # ----- M1 verdict ---------------------------------------------------------
  ratio = -1
  m1_reason = ""
  if (ralph_n == 0 && goal_n == 0) {
    m1_status = "FAIL"
    m1_reason = "no per-issue rows in CSV (all rows filtered or sentinel-tagged)"
  } else if (ralph_n == 0) {
    m1_status = "FAIL"
    m1_reason = "all ralph rows crashed; insufficient data"
  } else if (goal_n == 0) {
    m1_status = "FAIL"
    m1_reason = "all goal rows crashed; insufficient data"
  } else if (ralph_med <= 0) {
    m1_status = "FAIL"
    m1_reason = "ralph median wall-clock <= 0; cannot compute ratio"
  } else {
    ratio = goal_med / ralph_med
    m1_status = (ratio <= 0.70) ? "PASS" : "FAIL"
  }

  # ----- M3 verdict ---------------------------------------------------------
  m3_reason = ""
  if (total_perturn == 0) {
    p95 = -1; p100 = -1
    m3_p95_status   = "FAIL"
    m3_p100_status  = "FAIL"
    m3_overall      = "FAIL"
    m3_reason       = "no per-turn rows in CSV"
  } else {
    sort_in_place(perturn, total_perturn)
    p95  = perturn[percentile_idx(0.95, total_perturn)]
    p100 = perturn[total_perturn]
    m3_p95_status  = (p95  <= 0.60) ? "PASS" : "FAIL"
    m3_p100_status = (p100 <= 0.80) ? "PASS" : "FAIL"
    m3_overall     = (m3_p95_status == "PASS" && m3_p100_status == "PASS") ? "PASS" : "FAIL"
  }

  # ----- Render -------------------------------------------------------------
  if (fmt == "md") {
    print "## M1 — Boot-cost reduction (median wall-clock per orchestrator)"
    print ""
    print "Threshold: goal_median / ralph_median <= 0.70"
    print ""
    print "| Orchestrator | N issues | Median wall-clock (s) |"
    print "|---|---|---|"
    printf "| ralph | %d | %s |\n", ralph_n, (ralph_n > 0 ? sprintf("%.2f", ralph_med) : "NA")
    printf "| goal  | %d | %s |\n", goal_n,  (goal_n  > 0 ? sprintf("%.2f", goal_med)  : "NA")
    print ""
    if (ratio >= 0) {
      printf "Observed ratio: %.2f / %.2f = %.3f\n", goal_med, ralph_med, ratio
      printf "Result: **M1 %s** (%.3f %s 0.70)\n", m1_status, ratio, (m1_status == "PASS" ? "<=" : ">")
    } else {
      printf "Observed ratio: NA — %s\n", m1_reason
      printf "Result: **M1 %s** — %s\n", m1_status, m1_reason
    }
    print ""
    print "## M3 — Smart-zone discipline (main-context utilization)"
    print ""
    printf "Thresholds: p95 <= 60%%, p100 <= 80%%\n"
    printf "Window size: %d tokens\n", window
    printf "Per-turn samples: %d\n", total_perturn
    print ""
    if (total_perturn > 0) {
      printf "Observed: p95 = %.1f%%, p100 = %.1f%%\n", p95 * 100, p100 * 100
      print ""
      printf "- p95  = %.1f%%: **M3 %s** (%s 60%%)\n",  p95  * 100, m3_p95_status,  (m3_p95_status  == "PASS" ? "<=" : ">")
      printf "- p100 = %.1f%%: **M3 %s** (%s 80%%)\n",  p100 * 100, m3_p100_status, (m3_p100_status == "PASS" ? "<=" : ">")
      printf "- Overall: **M3 %s** (both thresholds %s)\n", m3_overall, (m3_overall == "PASS" ? "met" : "evaluated; see per-threshold lines above")
    } else {
      printf "Observed: NA — %s\n", m3_reason
      printf "Result: **M3 %s** — %s\n", m3_overall, m3_reason
    }
    print ""
    print "---"
    printf "Raw data: %s\n", csv_path
    printf "Window size used: %d tokens\n", window
    print  "Generated by: scripts/replay-reduce.sh"
  } else {
    # txt format: strip markdown, keep PASS/FAIL phrasing intact for AC regex.
    print "M1 - Boot-cost reduction (median wall-clock per orchestrator)"
    print "  Threshold: goal_median / ralph_median <= 0.70"
    printf "  ralph: N=%d, median=%s\n", ralph_n, (ralph_n > 0 ? sprintf("%.2fs", ralph_med) : "NA")
    printf "  goal : N=%d, median=%s\n", goal_n,  (goal_n  > 0 ? sprintf("%.2fs", goal_med)  : "NA")
    if (ratio >= 0) {
      printf "  ratio: %.3f\n", ratio
      printf "  M1 %s\n", m1_status
    } else {
      printf "  M1 %s - %s\n", m1_status, m1_reason
    }
    print ""
    print "M3 - Smart-zone discipline (main-context utilization)"
    printf "  Thresholds: p95 <= 60%%, p100 <= 80%%\n"
    printf "  Window size: %d tokens; per-turn samples: %d\n", window, total_perturn
    if (total_perturn > 0) {
      printf "  p95  = %.1f%%   M3 %s\n", p95  * 100, m3_p95_status
      printf "  p100 = %.1f%%   M3 %s\n", p100 * 100, m3_p100_status
      printf "  M3 %s overall\n", m3_overall
    } else {
      printf "  M3 %s - %s\n", m3_overall, m3_reason
    }
    print ""
    printf "Raw data: %s\n", csv_path
    printf "Window size used: %d tokens\n", window
  }
}
' "$CSV"
