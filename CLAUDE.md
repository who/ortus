# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## AI Guidance

* After receiving tool results, carefully reflect on their quality and determine optimal next steps before proceeding.
* For maximum efficiency, invoke multiple independent operations simultaneously rather than sequentially.
* Before you finish, verify your solution.
* Do what has been asked; nothing more, nothing less.
* NEVER create files unless absolutely necessary for achieving your goal.
* ALWAYS prefer editing an existing file to creating a new one.
* NEVER proactively create documentation files (*.md) or README files unless explicitly requested.

## Work Execution Policy

Track implementation work in beads and follow the session protocol in `AGENTS.md`. Use `ortus grind` for autonomous queue execution. The Python scheduler owns repetition and verifies results from bd state; workers handle one issue per fresh subprocess.

## Project Overview

Ortus is a Python CLI for bootstrapping and autonomously executing bd-tracked work with either Claude Code or Codex. Claude is the default; Codex is optional.

## Technology Stack

- **CLI**: Python + Typer
- **Project templates**: Jinja2 resources under `src/ortus/templates/`
- **Issue tracking**: beads (`bd`)
- **Supported Languages**: TypeScript, Python, Go, Rust, and others

## Development Guidelines

### Code Standards
* Template files use Jinja2 syntax and a `.jinja` extension.
* Claude-specific behavior stays in `ClaudeRunner`; Codex-specific behavior stays in `CodexRunner`.
* Never pass a literal `/goal` to `codex exec`.
* Run tests covering the changed surface; shared core or prompt changes justify the full suite.

## Command Reference

### Development

```bash
uv run pytest
uv build
ortus init /tmp/test-project --backend claude
ortus init /tmp/test-project-codex --backend codex
```

### File Operations - Use Fast Tools

```bash
# List files (FAST)
fd . -t f           # All files recursively
rg --files          # All files (respects .gitignore)
fd . -t d           # All directories

# Search content (FAST)
rg "search_term"                # Search in all files
rg -i "case_insensitive"        # Case-insensitive
rg "pattern" -g "*.ext"         # Only specific file type
rg -l "pattern"                 # Filenames with matches
rg -c "pattern"                 # Count matches per file
rg -n "pattern"                 # Show line numbers
rg -A 3 -B 3 "pattern"          # Context lines

# Find files by name (FAST)
fd "filename"                   # Find by name pattern
fd -e ext                       # All files with extension
```

### Banned Commands - Avoid These Slow Tools

* `tree` - use `fd` instead
* `find` - use `fd` or `rg --files`
* `grep` or `grep -r` - use `rg` instead
* `ls -R` - use `rg --files` or `fd`
* `cat file | grep` - use `rg pattern file`

### Search Strategy

1. Start broad, then narrow: `rg "partial" | rg "specific"`
2. Filter by type early: `rg "pattern" -g "*.ext"`
3. Batch patterns: `rg "(pattern1|pattern2|pattern3)"`
4. Limit scope: `rg "pattern" src/`

## Project Architecture

`src/ortus/commands/` contains the eight CLI verbs. `src/ortus/core/` contains shared scheduler, backend, bd, Git, sandbox, and rendering infrastructure. `src/ortus/prompts/` contains worker prompts and conditions. `src/ortus/templates/` contains the small per-project files emitted by `ortus init`.

## Issue Tracking

This project uses **beads** (`bd`) for issue tracking. See **AGENTS.md** for workflow and session protocol.

## Important Files

* **CLAUDE.md** - AI agent instructions (this file)
* **AGENTS.md** - Session rules and landing-the-plane protocol
* **src/ortus/core/agent.py** - backend selection and Codex runner
* **src/ortus/commands/grind.py** - deterministic subprocess-per-task scheduler

## Pro Tips for AI Agents

* Always use `--json` flags when available for programmatic use
* Use dependency trees to understand complex relationships
* Higher priority issues (0-1) are usually more important than lower (2-4)

## CodeGraph (optional)

If you have [CodeGraph](https://github.com/colbymchenry/codegraph) installed, the orchestrator will use it automatically; if not, nothing changes. Not required — the loop detects CodeGraph at runtime and falls back silently to grep/glob/Read when absent.


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

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. When a remote is configured, work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY when a remote is configured:
   ```bash
   if [ -n "$(git remote)" ]; then
     git pull --rebase
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
<!-- END BEADS INTEGRATION -->
