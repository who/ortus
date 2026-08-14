#!/usr/bin/env bash
# pretty-grind-tail.sh — make a Grok/Claude grind log readable on a TTY.
#
# Usage:
#   tail -n 50 -f logs/grind-*.log | ./scripts/pretty-grind-tail.sh
#   ortus tail --raw --lines 0 logs/grind-20260814-140840.log | ./scripts/pretty-grind-tail.sh
#   ./scripts/pretty-grind-tail.sh logs/grind-20260814-140840.log
#
# Coalesces thought/text crumbs into paragraphs. Prints tool starts as one
# line. Passes grind scheduler lines ([YYYY-MM-DD …]) through. Drops usage
# blobs and available_commands spam.
set -euo pipefail

if ! command -v jq >/dev/null 2>&1; then
  echo "pretty-grind-tail: jq is required" >&2
  exit 1
fi

if [[ -t 1 ]]; then
  C_THINK=$(printf '\033[2m')     # dim
  C_TEXT=$(printf '\033[0m')
  C_TOOL=$(printf '\033[1;36m')   # bold cyan
  C_DONE=$(printf '\033[32m')     # green
  C_FAIL=$(printf '\033[31m')     # red
  C_GRIND=$(printf '\033[1;33m')  # bold yellow
  C_OFF=$(printf '\033[0m')
else
  C_THINK= C_TEXT= C_TOOL= C_DONE= C_FAIL= C_GRIND= C_OFF=
fi

buf_kind=
buf=

flush() {
  [[ -z ${buf:-} ]] && { buf_kind=; return; }
  # collapse internal newlines the model already sent
  local body
  body=$(printf '%s' "$buf" | tr -s ' \t' ' ')
  if [[ $buf_kind == thought ]]; then
    printf '%s  think  %s%s\n' "$C_THINK" "$body" "$C_OFF"
  else
    printf '%s  text   %s%s\n' "$C_TEXT" "$body" "$C_OFF"
  fi
  buf=
  buf_kind=
}

append() {
  local kind=$1 chunk=$2
  if [[ -n $buf_kind && $buf_kind != "$kind" ]]; then
    flush
  fi
  buf_kind=$kind
  buf+="$chunk"
  # model sent a paragraph break — show it
  if [[ $chunk == *$'\n'* ]]; then
    flush
  fi
}

summarize_tool() {
  jq -r '
    (.toolName // .title // "tool") as $name
    | .rawInput as $in
    | (
        $in.command
        // $in.target_file
        // $in.file_path
        // $in.query
        // $in.url
        // $in.tool_name
        // $in.pattern
        // empty
      ) as $detail
    | if $detail == "" then $name
      else ($name + "  " + ($detail | gsub("\n"; " ") | .[0:160]))
      end
  '
}

# stdin, or each file arg (cat, not follow — pipe tail -f for live)
if [[ $# -gt 0 ]]; then
  exec < <(cat -- "$@")
fi

while IFS= read -r line || [[ -n $line ]]; do
  # grind scheduler / progress
  if [[ $line == \[[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]\ * ]]; then
    flush
    printf '%s%s%s\n' "$C_GRIND" "$line" "$C_OFF"
    continue
  fi

  if [[ $line != \{* ]]; then
    # leftover plain text
    [[ -n $line ]] && printf '  raw    %s\n' "$line"
    continue
  fi

  if ! type=$(printf '%s\n' "$line" | jq -er '.type' 2>/dev/null); then
    continue
  fi

  case $type in
    thought)
      chunk=$(printf '%s\n' "$line" | jq -r '.data // empty')
      append thought "$chunk"
      ;;
    text)
      chunk=$(printf '%s\n' "$line" | jq -r '.data // empty')
      append text "$chunk"
      ;;
    tool_call)
      flush
      detail=$(printf '%s\n' "$line" | summarize_tool)
      printf '%s  tool   %s%s\n' "$C_TOOL" "$detail" "$C_OFF"
      ;;
    tool_call_update)
      status=$(printf '%s\n' "$line" | jq -r '.status // empty')
      case $status in
        completed)
          flush
          printf '%s  done   tool%s\n' "$C_DONE" "$C_OFF"
          ;;
        failed|error)
          flush
          printf '%s  fail   tool%s\n' "$C_FAIL" "$C_OFF"
          ;;
      esac
      ;;
    plan)
      flush
      printf '%s\n' "$line" | jq -r '
        .entries[]? |
        "  plan   [\(.status // "?")] \(.content)"
      '
      ;;
    usage|available_commands) ;;
    *)
      flush
      printf '  %s\n' "$type"
      ;;
  esac
done

flush
