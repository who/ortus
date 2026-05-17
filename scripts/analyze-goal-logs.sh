#!/usr/bin/env bash
# analyze-goal-logs.sh — Tally M2 sentinel regressions from goal-*.log stream-json.
#
# Usage: ./scripts/analyze-goal-logs.sh [options]
#
# Options:
#   --log PATH                       One or more goal-*.log files to aggregate.
#                                    Repeatable. (default: ALL logs/goal-*.log —
#                                    not just newest, because the M2 protocol
#                                    aggregates across 7 days.)
#   --report-format md|txt           Output format (default: md). md emits an M2
#                                    section suitable for direct paste into
#                                    reports/goal-vs-ralph-<date>.md; txt strips
#                                    markdown but preserves PASS/FAIL wording.
#   --include-ralph-baseline         Re-tally any logs/ralph-*.log alongside for the
#                                    >=1/100 ratio comparison (default: on).
#   --no-include-ralph-baseline      Suppress the ralph baseline section regardless
#                                    of whether ralph-*.log files exist.
#   -h, --help                       Show this help and exit.
#
# Output: an M2 block written to stdout. PASS/FAIL wording satisfies the
# ortus-bn4a.3 AC test (b) — case-insensitive match for `missed[_-]termination`
# alongside "zero" on PASS or non-zero counts on FAIL.
#
# Stream-json shape (per Claude Code v2.1.139+; matches logs/ralph-*.log samples
# when those logs are themselves stream-json — shell-only ralph logs without
# stream-json content tally to 0/0 by definition and are reported as such):
#   one NDJSON event per line, each tagged with {type, subtype, message?, ...}.
#   Assistant turns carry `.message.model`; main vs. evaluator is keyed off the
#   model name (matches /haiku/i for evaluator). Tool calls live under
#   `.message.content[].type == "tool_use"`. Tool results come back as user
#   events with `.message.content[].type == "tool_result"`.
#
# Discriminator (reused from scripts/eval-cost.sh per ortus-xkad design):
#   evaluator iff `.message.model` matches regex /haiku/i.
#
# Sentinel definitions (per ortus-bn4a.3 19:35 comment, runbook step 4):
#
#   missed_termination — an evaluator turn (haiku) whose text implies "done"
#     (regex /[Dd]one|[Cc]omplete|[Ee]xit|[Tt]erminate|[Cc]ondition.*[Mm]et/),
#     where the next non-evaluator assistant turn still exists in the log. The
#     evaluator told goal.sh to stop, but goal.sh kept going.
#
#   spurious_sleep — a tool_use of `ScheduleWakeup`, or a `Bash` tool_use whose
#     `.input.command` starts with `sleep`, when the most recent `bd ready`
#     tool_result in the transcript was non-empty (i.e. there was queued work,
#     so the sleep was unjustified). Heuristic; false positives possible —
#     operator may have run `sleep` for a legitimate non-bd reason. Documented
#     here per the issue design field's "document the false-positive risk".
#
# A `bd ready` tool_result is judged "empty" iff its content matches either the
# empty JSON array (`[]`) or English negation markers (`no ready`, `no issues`,
# `EMPTY`). Anything else is treated as non-empty.
#
# Edge cases (all degrade gracefully, never crash):
#   - No goal-*.log in logs/ and no --log given: clear stderr error, exit 1.
#   - --log file missing: clear stderr error, exit 1.
#   - Empty / unparseable stream-json: lines skipped silently; if zero events
#     parse, the tally is 0/0 and rendered as PASS-by-absence (consistent with
#     replay-reduce.sh and eval-cost.sh "no data" branches).
#   - --include-ralph-baseline with no ralph-*.log: emits
#     "ralph baseline: N/A — no historical ralph-*.log files in logs/".
#   - --include-ralph-baseline with shell-only (non-stream-json) ralph logs:
#     baseline tally is 0/0 and rendered with a "no stream-json events parsed"
#     caveat so the operator isn't misled by accidental zeros.
#   - jq missing on $PATH: clear stderr error, exit 1.
#
# This script lives outside template/ on purpose: it is a measurement tool for
# the ortus repo itself, not part of the generated project
# surface — same reasoning as scripts/replay-queue.sh, scripts/replay-reduce.sh,
# and scripts/eval-cost.sh.

set -u

REPORT_FORMAT=md
INCLUDE_RALPH_BASELINE=1
LOGS=()

while [[ $# -gt 0 ]]; do
  case $1 in
    --log) LOGS+=("$2"); shift 2 ;;
    --report-format) REPORT_FORMAT="$2"; shift 2 ;;
    --include-ralph-baseline) INCLUDE_RALPH_BASELINE=1; shift ;;
    --no-include-ralph-baseline) INCLUDE_RALPH_BASELINE=0; shift ;;
    -h|--help) sed -n '2,/^[^#]/{/^#/{s/^# \?//;p;}}' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; echo "Run '$0 -h' for usage." >&2; exit 2 ;;
  esac
done

case "$REPORT_FORMAT" in
  md|txt) ;;
  *) echo "--report-format must be md or txt (got: $REPORT_FORMAT)" >&2; exit 2 ;;
esac

if ! command -v jq >/dev/null 2>&1; then
  echo "analyze-goal-logs: jq is required but not on \$PATH" >&2
  echo "  install via: apt-get install jq | brew install jq | apk add jq" >&2
  exit 1
fi

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)

# Default --log: ALL logs/goal-*.log (not just newest — M2 aggregates across days).
if [ "${#LOGS[@]}" -eq 0 ]; then
  while IFS= read -r f; do
    [ -n "$f" ] && LOGS+=("$f")
  done < <(ls "$repo_root"/logs/goal-*.log 2>/dev/null)
  if [ "${#LOGS[@]}" -eq 0 ]; then
    echo "analyze-goal-logs: no --log given and no logs/goal-*.log found under $repo_root" >&2
    exit 1
  fi
fi

for log in "${LOGS[@]}"; do
  if [ ! -f "$log" ]; then
    echo "analyze-goal-logs: log not found: $log" >&2
    exit 1
  fi
done

# Build ralph baseline list (if enabled).
RALPH_LOGS=()
if [ "$INCLUDE_RALPH_BASELINE" -eq 1 ]; then
  while IFS= read -r f; do
    [ -n "$f" ] && RALPH_LOGS+=("$f")
  done < <(ls "$repo_root"/logs/ralph-*.log 2>/dev/null)
fi

# extract_events <log-file> ... — emit one TSV row per event-of-interest.
# Columns: <kind>\t<detail>
# Kinds:
#   assistant_main      — non-haiku assistant message (exists regardless of text)
#   assistant_eval      — haiku assistant message (evaluator)
#   tool_use_sleep      — ScheduleWakeup OR Bash sleep call
#   tool_use_bd_ready   — Bash invocation containing `bd ready`
#   tool_result         — tool_result body (used to classify the prior tool_use)
extract_events() {
  for log in "$@"; do
    jq -R --raw-output --slurp '
      split("\n")
      | map(fromjson? // empty)
      | map(
          . as $e |
          if $e.type == "assistant" then
            (($e.message.model // "")  as $m |
             ($e.message.content // []) as $c |
             ($c | map(select(.type == "text") | .text) | join(" ")) as $text |
             ($c | map(select(.type == "tool_use"))) as $tools |
             if ($m | test("haiku"; "i")) then
               # Evaluator turn: emit one assistant_eval row. Ignore any tools
               # (the /goal evaluator does not use tools).
               [["assistant_eval", $text]]
             else
               # Main turn: emit one assistant_main row, then one row per
               # tool_use classified as sleep / bd_ready (others ignored).
               [["assistant_main", $text]] +
               ($tools | map(
                  if .name == "ScheduleWakeup" then
                    ["tool_use_sleep", "ScheduleWakeup"]
                  elif (.name == "Bash" and ((.input.command // "") | test("^[[:space:]]*sleep([[:space:]]|$)"))) then
                    ["tool_use_sleep", ("Bash:" + ((.input.command // "") | tostring))]
                  elif (.name == "Bash" and ((.input.command // "") | test("bd[[:space:]]+ready"))) then
                    ["tool_use_bd_ready", ((.input.command // "") | tostring)]
                  else
                    empty
                  end
                ))
             end
            )
          elif $e.type == "user" then
            # Flatten tool_result bodies (string or array-of-blocks) into a
            # single short string so awk can pattern-match for "empty" markers.
            (($e.message.content // []) as $c |
             ($c | map(select(.type == "tool_result"))
                 | map(
                     if (.content | type) == "string" then .content
                     else (.content // [] | map(select(.type == "text") | .text) | join(" "))
                     end
                   )
                 | join(" ")
             ) as $result |
             if ($result | length) > 0 then [["tool_result", $result]] else [] end
            )
          else
            []
          end
        )
      | map(.[])
      | .[]
      | @tsv
    ' "$log"
  done
}

# tally_events — read TSV from stdin, emit "<missed_termination>\t<spurious_sleep>\t<events_parsed>"
tally_events() {
  awk -F'\t' '
  BEGIN {
    mt = 0
    ss = 0
    events = 0
    eval_said_done = 0
    last_bd_ready_nonempty = 0
    last_was_bd_ready_call = 0
  }
  {
    events++
    kind = $1
    detail = $2

    if (kind == "assistant_eval") {
      # Heuristic: evaluator implies "done" via these tokens.
      if (detail ~ /[Dd]one|[Cc]omplete|[Ee]xit|[Tt]erminate|[Cc]ondition.*[Mm]et/) {
        eval_said_done = 1
      } else {
        # Explicit "not done" — clear any stale flag from a prior evaluator turn.
        eval_said_done = 0
      }
    } else if (kind == "assistant_main") {
      # Existence of the next main turn after a "done" evaluator IS the regression.
      if (eval_said_done) {
        mt++
      }
      eval_said_done = 0
    } else if (kind == "tool_use_sleep") {
      if (last_bd_ready_nonempty) {
        ss++
      }
    } else if (kind == "tool_use_bd_ready") {
      last_was_bd_ready_call = 1
    } else if (kind == "tool_result") {
      if (last_was_bd_ready_call) {
        # Empty markers: bare JSON [], or English "no ready/no issues/EMPTY".
        if (detail ~ /^[[:space:]]*\[[[:space:]]*\][[:space:]]*$/ \
            || detail ~ /[Nn]o ready/ \
            || detail ~ /[Nn]o issues/ \
            || detail ~ /EMPTY/) {
          last_bd_ready_nonempty = 0
        } else {
          last_bd_ready_nonempty = 1
        }
        last_was_bd_ready_call = 0
      }
    }
  }
  END {
    printf "%d\t%d\t%d\n", mt, ss, events
  }
  '
}

# Goal tally.
GOAL_TSV=$(extract_events "${LOGS[@]}")
GOAL_TALLY=$(printf '%s\n' "$GOAL_TSV" | tally_events)
goal_mt=$(echo "$GOAL_TALLY" | cut -f1)
goal_ss=$(echo "$GOAL_TALLY" | cut -f2)
goal_events=$(echo "$GOAL_TALLY" | cut -f3)

# Ralph baseline tally.
ralph_state="disabled"
ralph_mt=0
ralph_ss=0
ralph_events=0
ralph_n_logs=0
if [ "$INCLUDE_RALPH_BASELINE" -eq 1 ]; then
  ralph_n_logs="${#RALPH_LOGS[@]}"
  if [ "$ralph_n_logs" -eq 0 ]; then
    ralph_state="no_logs"
  else
    RALPH_TSV=$(extract_events "${RALPH_LOGS[@]}")
    RALPH_TALLY=$(printf '%s\n' "$RALPH_TSV" | tally_events)
    ralph_mt=$(echo "$RALPH_TALLY" | cut -f1)
    ralph_ss=$(echo "$RALPH_TALLY" | cut -f2)
    ralph_events=$(echo "$RALPH_TALLY" | cut -f3)
    if [ "$ralph_events" -eq 0 ]; then
      ralph_state="no_streamjson"
    else
      ralph_state="ok"
    fi
  fi
fi

# Overall M2 verdict — both counts must be zero.
if [ "$goal_mt" -eq 0 ] && [ "$goal_ss" -eq 0 ]; then
  STATUS="PASS"
else
  STATUS="FAIL"
fi

logs_csv=$(IFS=, ; echo "${LOGS[*]}")

emit_md() {
  echo "## M2 — Sentinel-class regressions (missed_termination + spurious_sleep)"
  echo ""
  echo "Threshold: both counts == 0 across all goal-*.log"
  echo ""
  echo "| Sentinel | Goal count |"
  echo "|---|---|"
  echo "| missed_termination | $goal_mt |"
  echo "| spurious_sleep | $goal_ss |"
  echo ""
  if [ "$STATUS" = "PASS" ]; then
    echo "Result: **M2 PASS** — zero missed_termination and zero spurious_sleep across $((${#LOGS[@]})) goal-*.log file(s); $goal_events stream-json event(s) parsed."
  else
    reasons=""
    [ "$goal_mt" -gt 0 ] && reasons="${reasons}missed_termination=$goal_mt; "
    [ "$goal_ss" -gt 0 ] && reasons="${reasons}spurious_sleep=$goal_ss; "
    echo "Result: **M2 FAIL** — ${reasons}across $((${#LOGS[@]})) goal-*.log file(s)."
  fi
  echo ""
  if [ "$INCLUDE_RALPH_BASELINE" -eq 1 ]; then
    echo "### Ralph baseline (re-tallied for the >=1/100 missed-termination ratio)"
    echo ""
    case "$ralph_state" in
      no_logs)
        echo "ralph baseline: N/A — no historical ralph-*.log files in logs/"
        ;;
      no_streamjson)
        echo "ralph baseline: N/A — no stream-json events parsed from $ralph_n_logs ralph-*.log file(s) (logs are likely shell-only timestamped output)"
        ;;
      ok)
        echo "| Sentinel | Ralph count |"
        echo "|---|---|"
        echo "| missed_termination | $ralph_mt |"
        echo "| spurious_sleep | $ralph_ss |"
        echo ""
        echo "ralph baseline: missed_termination=$ralph_mt, spurious_sleep=$ralph_ss across $ralph_n_logs ralph-*.log file(s); $ralph_events stream-json event(s) parsed."
        ;;
    esac
    echo ""
  fi
  echo "---"
  echo "Raw data: $logs_csv"
  echo "Generated by: scripts/analyze-goal-logs.sh"
}

emit_txt() {
  echo "M2 - Sentinel-class regressions (missed_termination + spurious_sleep)"
  echo "  Threshold: both counts == 0 across all goal-*.log"
  echo "  missed_termination: $goal_mt"
  echo "  spurious_sleep: $goal_ss"
  if [ "$STATUS" = "PASS" ]; then
    echo "  M2 PASS - zero missed_termination and zero spurious_sleep across ${#LOGS[@]} file(s); $goal_events event(s) parsed"
  else
    reasons=""
    [ "$goal_mt" -gt 0 ] && reasons="${reasons}missed_termination=$goal_mt; "
    [ "$goal_ss" -gt 0 ] && reasons="${reasons}spurious_sleep=$goal_ss; "
    echo "  M2 FAIL - ${reasons}across ${#LOGS[@]} file(s)"
  fi
  if [ "$INCLUDE_RALPH_BASELINE" -eq 1 ]; then
    echo ""
    echo "Ralph baseline (re-tallied for >=1/100 missed-termination ratio):"
    case "$ralph_state" in
      no_logs)
        echo "  ralph baseline: N/A - no historical ralph-*.log files in logs/"
        ;;
      no_streamjson)
        echo "  ralph baseline: N/A - no stream-json events parsed from $ralph_n_logs ralph-*.log file(s)"
        ;;
      ok)
        echo "  missed_termination: $ralph_mt"
        echo "  spurious_sleep: $ralph_ss"
        echo "  events parsed: $ralph_events across $ralph_n_logs ralph-*.log file(s)"
        ;;
    esac
  fi
  echo ""
  echo "Raw data: $logs_csv"
}

if [ "$REPORT_FORMAT" = "md" ]; then
  emit_md
else
  emit_txt
fi
