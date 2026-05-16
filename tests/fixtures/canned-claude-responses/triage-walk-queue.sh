#!/usr/bin/env bash
# Canned claude scenario: triage walks the human queue and dispositions one.
echo '{"type":"system","subtype":"start","session_id":"triage"}'
echo '{"type":"assistant","message":{"content":"Reviewing first human-flagged issue."}}'
echo '{"type":"assistant","message":{"content":"Disposition recorded; ending turn."}}'
echo '{"type":"system","subtype":"end"}'
exit 0
