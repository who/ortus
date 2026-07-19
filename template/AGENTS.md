# Agent Instructions

This project uses **bd** (beads) for issue tracking. Run `bd onboard` to get started.

## Session Start

**Run `bd prime` as the first action of every session** (and again after a compaction).
It reloads the beads workflow context — command reference, ready-queue conventions,
and the session close protocol — that the rest of this file assumes you have.

Do not rely on the harness to do this for you. Under the Claude Code backend a
`SessionStart`/`PreCompact` hook (installed by `bd setup claude`) usually fires it
automatically, so the explicit call is a cheap no-op. Under the Codex backend it is
the only thing that primes you: Codex does have a hook system with a `SessionStart`
event, but hooks there must be explicitly trusted before they run, so Ortus does not
depend on one. Running `bd prime` yourself works on both backends regardless.

## CLI output convention

Every non-interactive `ortus` verb emits per-phase progress lines so the operator can tell "running" from "hung." Convention:

- Progress lines go to **stderr** so they don't pollute machine-readable stdout.
- Format: `[ortus <verb>] <phase>` — e.g. `[ortus plan] reading PRD from <path>`.
- One line per logical phase; close with a `[ortus <verb>] done (<short summary>)` line.
- For phases that legitimately take >5s, include a coarse expectation in the message (`... (this typically takes 1-3 min)`) so silence doesn't get misread as hang.
- Use `ortus.core.output.progress(verb, phase)` — do NOT roll your own `print(...)`.

Exempt verbs:
- `tail` — streaming-by-design, the output IS the work.
- `interview`, `triage` — interactive; the operator's typing provides the rhythm.

## Orchestrator

All autonomous loops run through `./ortus/goal.sh`, which drives a single long-lived `claude -p "/goal CONDITION"` session against the queue. The legacy `./ortus/ralph.sh` is now a one-line deprecation shim that prints a notice and `exec`s `goal.sh` — kept for one minor version so existing aliases and CI invocations keep working. New scripts should call `goal.sh` directly. Both share `.beads/ralph.flock`, so only one orchestrator runs at a time per repo. Logs land at `logs/goal-<timestamp>.log` (and `logs/ralph-*.log` for archival pre-shim runs); `./ortus/tail.sh` follows both transparently.

## Quick Reference

```bash
./ortus/goal.sh       # Drive the queue to zero (primary orchestrator)
./ortus/goal.sh --tasks 1   # One task and out
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --status in_progress  # Claim work
bd close <id>         # Complete work
bd dolt push          # Push beads to Dolt remote
```

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. When a remote is configured, work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY when a remote is configured:
   ```bash
   if [ -n "$(git remote)" ]; then
     git pull --rebase --autostash
     bd dolt push
     git push
     git status  # MUST show "up to date with origin"
   else
     echo "No git remote configured; skipping push (local-only project)."
   fi
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed (when a remote is configured)
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- When a remote is configured, work is NOT complete until `git push` succeeds
- NEVER stop before pushing when a remote exists - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
