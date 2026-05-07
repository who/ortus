#!/bin/bash
# recover-dolt.sh - Recover from cascaded dolt sql-server pile-up
#
# Usage: ./ortus/recover-dolt.sh [--dry-run]
#
# When bd's auto-start fails (waitForReady timeout, lost noms/LOCK race),
# orphan dolt sql-server processes accumulate and bd's `.beads/dolt-server.{pid,port}`
# state files can go missing. Subsequent bd calls then auto-start *more* dolts
# that fight the orphans for noms/LOCK, deepening the cascade.
#
# This script breaks the cycle:
#   1. Refuses to run if ./ortus/ralph.sh holds .beads/ralph.flock (ralph
#      already manages dolt's lifecycle — don't fight it).
#   2. SIGKILLs all dolt sql-server processes that are reachable from this
#      shell (host PID namespace).
#   3. Removes bd-owned state files (.beads/dolt-server.{lock,pid,port}).
#      Leaves .log alone for forensics; rotates only on demand.
#   4. Removes stale per-port circuit breakers in /tmp/beads-circuit/.
#   5. NEVER touches `.beads/dolt/.dolt/noms/LOCK` or any other dolt-internal
#      file. Per upstream gastownhall/beads#2933, removing those while a
#      dolt holds the flock causes silent corruption.
#   6. Starts a fresh dolt via `bd dolt start` and verifies.
#
# Exit codes: 0 success, 1 ralph is running, 2 bd dolt start failed.

set -e

DRY_RUN=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,/^[^#]/{/^#/{s/^# \?//;p;}}' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# Resolve project root by walking up from $PWD looking for .beads/.
root="$PWD"
while [ "$root" != "/" ] && [ ! -d "$root/.beads" ]; do
  root="$(dirname "$root")"
done
if [ ! -d "$root/.beads" ]; then
  echo "ERROR: no .beads/ directory found from $PWD up to /" >&2
  exit 1
fi
cd "$root"

say() { echo "[recover-dolt] $*"; }
do_or_say() {
  if [ -n "$DRY_RUN" ]; then echo "[dry-run] $*"; else eval "$@"; fi
}

# --- Step 1: refuse if ralph is running ----------------------------------
if [ -f .beads/ralph.flock ] && command -v flock >/dev/null 2>&1; then
  if ! flock -n .beads/ralph.flock true 2>/dev/null; then
    echo "ERROR: ralph.sh is currently running against this repo (holds .beads/ralph.flock)." >&2
    echo "       ralph already manages dolt's lifecycle. Stop ralph first, or wait for it to exit." >&2
    exit 1
  fi
fi

# --- Step 2: kill orphan dolt sql-servers --------------------------------
# Match by argv only — there's nothing project-specific in dolt sql-server's
# command line, so this kills ALL dolt sql-servers visible to this shell.
# That's intentional: per-project isolation isn't possible at the process
# level, and the alternative (let stale ones survive) is what got us here.
PIDS="$(pgrep -f 'dolt sql-server' 2>/dev/null || true)"
if [ -n "$PIDS" ]; then
  count="$(echo "$PIDS" | wc -w)"
  say "Killing ${count} dolt sql-server process(es): $(echo $PIDS | tr '\n' ' ')"
  do_or_say "kill -KILL $PIDS 2>/dev/null || true"
  if [ -z "$DRY_RUN" ]; then
    sleep 1
    REMAINING="$(pgrep -f 'dolt sql-server' 2>/dev/null || true)"
    if [ -n "$REMAINING" ]; then
      say "WARNING: some dolt processes survived SIGKILL: $REMAINING"
    fi
  fi
else
  say "No dolt sql-server processes running."
fi

# --- Step 3: remove bd-owned state files ---------------------------------
# Never touch .beads/dolt/.dolt/noms/LOCK or anything else under .beads/dolt/.
for f in .beads/dolt-server.lock .beads/dolt-server.pid .beads/dolt-server.port; do
  if [ -e "$f" ]; then
    say "Removing $f"
    do_or_say "rm -f '$f'"
  fi
done

# --- Step 4: clear stale circuit breakers --------------------------------
if compgen -G "/tmp/beads-circuit/*.json" >/dev/null 2>&1; then
  say "Clearing stale circuit-breaker files in /tmp/beads-circuit/"
  do_or_say "rm -f /tmp/beads-circuit/*.json"
fi

# --- Step 5: bring up a fresh dolt ---------------------------------------
if [ -n "$DRY_RUN" ]; then
  say "[dry-run] Would run: bd dolt start"
  exit 0
fi

say "Starting fresh dolt sql-server..."
if ! bd dolt start 2>&1; then
  echo "ERROR: bd dolt start failed. Check .beads/dolt-server.log for details." >&2
  exit 2
fi

# --- Step 6: verify ------------------------------------------------------
say "Verifying..."
if ! bd dolt status 2>&1 | grep -q 'Dolt server: running'; then
  echo "ERROR: bd dolt status reports the server is not running." >&2
  exit 2
fi

ALIVE_COUNT="$(pgrep -f 'dolt sql-server' 2>/dev/null | wc -l)"
if [ "$ALIVE_COUNT" -ne 1 ]; then
  say "WARNING: expected exactly 1 dolt sql-server, found $ALIVE_COUNT"
fi

say "Recovery complete. Ready for bd commands."
