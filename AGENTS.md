# Agent Instructions

This project uses **bd** (beads) for issue tracking. Run `bd onboard` to get started.

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


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

See the canonical session-close checklist at the top of this file.
<!-- END BEADS INTEGRATION -->
