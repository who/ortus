#!/usr/bin/env bash
# ralph.sh — deprecation shim (Phase 5). All work delegates to goal.sh.
# Background: prd/PRD-goal-directive.md §Phase 5 / beads ortus-dcr4.
# Q4 (disableAllHooks): no --legacy bypass; goal.sh's check_hooks_enabled
# precheck fails fast with a docs link (ortus-sooj / ortus-dcr4.4).
echo '[ralph.sh] deprecated; delegating to goal.sh. See README.' >&2
exec "$(dirname "$0")/goal.sh" "$@"
