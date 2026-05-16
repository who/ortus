#!/usr/bin/env bash
# Canned claude scenario: interview picks a feature and runs a 1-Q interview.
echo '{"type":"system","subtype":"start","session_id":"interview"}'
echo '{"type":"assistant","message":{"content":"Interview started. What problem does this solve?"}}'
echo '{"type":"assistant","message":{"content":"OK noted; ending interview turn."}}'
echo '{"type":"system","subtype":"end"}'
exit 0
