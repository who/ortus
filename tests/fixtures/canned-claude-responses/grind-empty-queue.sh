#!/usr/bin/env bash
# Canned claude response: ortus grind invoked on an empty queue.
# Emits a tiny stream-json sequence that the /goal evaluator would judge
# as "queue empty" after a single turn, then exits 0.
set -e
echo '{"type":"system","subtype":"start","session_id":"empty-queue"}'
echo '{"type":"assistant","message":{"content":"bd ready --json returned [] — queue empty. Ending session."}}'
echo '{"type":"system","subtype":"end","ttf_ms":42}'
exit 0
