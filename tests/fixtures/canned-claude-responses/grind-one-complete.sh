#!/usr/bin/env bash
# Canned claude response: completes exactly one issue.
# Claims the first ready issue, makes a dummy edit, closes the issue. Runs
# in the cwd that ClaudeRunner set (= the repo root).
set -e
echo '{"type":"system","subtype":"start","session_id":"one-complete"}'

# Find the first ready non-epic issue (epics can't close until children do).
# Real claude would do the implementation; this canned shim represents the
# close-at-end behavior on a single concrete task.
first=$(bd ready --json | python3 -c "
import json, sys
d = json.load(sys.stdin)
for i in d:
    if i.get('issue_type') != 'epic':
        print(i['id'])
        break
")

if [ -n "$first" ]; then
  bd close "$first" --reason "canned grind-one-complete shim closed this" >/dev/null
  echo "{\"type\":\"assistant\",\"message\":{\"content\":\"Closed $first.\"}}"
fi

echo '{"type":"system","subtype":"end"}'
exit 0
