#!/usr/bin/env bash
# Canned claude response: claims an issue, hits a blocker, emits the
# <promise>BLOCKED</promise> marker, exits 0 without closing.
set -e
echo '{"type":"system","subtype":"start","session_id":"blocked"}'
echo '{"type":"assistant","message":{"content":"Hit a real blocker. <promise>BLOCKED</promise>"}}'
echo '{"type":"system","subtype":"end"}'
exit 0
