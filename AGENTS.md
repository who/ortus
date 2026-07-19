# Agent Instructions

This project uses **bd** (beads) for issue tracking. Run `bd onboard` to get started.

## Session Start

**Run `bd prime` as the first action of every session** (and again after a compaction).
Under the Claude Code backend a `SessionStart`/`PreCompact` hook usually fires it for
you, so the explicit call is a cheap no-op. Under the Codex backend nothing fires it:
Codex 0.144.x does have a hook system with a `SessionStart` event, but hooks must be
explicitly trusted before they run, so Ortus does not depend on one. Running
`bd prime` yourself works on both backends regardless.

## Supported platforms

Linux + macOS only. Windows was dropped 2026-05-17. Do not add Windows-specific code paths without explicit operator direction; Windows users should use WSL2.

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

## Acceptance-criteria convention

When writing bd-issue acceptance criteria for code-changing work, prefer **"tests covering the changed surface must pass; CI catches regressions elsewhere"** over **"uv run pytest must pass"**. The full ~5min suite is the GitHub Actions matrix's job (`.github/workflows/test.yml`, Linux+macOS × Python 3.10/3.11/3.12); inner grind iterations should run only the targeted subset that exercises the changed files. The exception is changes under `src/ortus/core/` (shared infrastructure) or `src/ortus/prompts/` (affects every future iteration) — those touch enough downstream that the full local run is justified.

## Orchestrator

All autonomous loops run through `ortus grind`, the supported Python CLI. It selects and claims one bd issue, launches one fresh backend worker, and trusts observable bd state to decide whether the iteration succeeded, orphaned a claim, or made no change. Claude remains the default and uses a narrow `/goal` worker; Codex is optional and uses a plain `codex exec` task because slash commands are not expanded by that non-interactive surface.

## Quick Reference

```bash
ortus grind .         # Drive the queue to zero
ortus grind . --tasks 1     # One task and out
ortus grind . --backend codex  # Override the configured backend
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
