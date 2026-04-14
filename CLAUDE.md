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

**All implementation work MUST go through Ralph loops.**

When asked to implement features, fix bugs, or make code changes:

1. **Do NOT implement directly** - Instead, create beads issues with detailed descriptions
2. **Create well-structured issues** - Use `bd create` with clear titles, descriptions, and acceptance criteria
3. **Set up dependencies** - Use `bd dep add` to establish proper ordering
4. **Defer to Ralph** - The `ortus/ralph.sh` loop will execute the actual work via `ortus/prompts/ralph-prompt.md`

**Allowed without Ralph loop:**
- Answering questions about the codebase
- Reading/exploring files for research
- Creating beads issues
- Discussing architecture or approach

**Requires Ralph loop:**
- Writing or modifying code
- Creating new files
- Running tests or builds
- Any implementation work

This ensures all work is tracked, atomic, and follows the defined workflow.

## Project Overview

Ortus is a Copier template for bootstrapping projects with Claude Code integration, beads issue tracking, and Ralph automation loops.

## Technology Stack

- **Template Engine**: Copier (Python)
- **Template Syntax**: Jinja2
- **Supported Languages**: TypeScript, Python, Go, Rust, and others

## Development Guidelines

### Code Standards
* Template files use Jinja2 syntax for variable substitution
* Files needing variable substitution use `.jinja` extension
* Use `{% raw %}...{% endraw %}` to escape template markers in Jinja files

### Before Committing
1. Test template: `copier copy --defaults template /tmp/test-project`
2. Verify generated project structure

## Command Reference

### Development
```bash
# Test template generation
copier copy --defaults --data project_name=testproj --data github_username=testuser template /tmp/test-project
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

### File Structure

```
ortus/                        # Project root
├── ortus/                    # Ortus tooling (matches generated project layout)
│   ├── ralph.sh              # Ralph automation loop
│   ├── tail.sh               # Log viewer
│   ├── idea.sh               # Quick feature creation
│   ├── interview.sh          # Interactive interview → PRD → tasks
│   └── prompts/
│       ├── ralph-prompt.md   # Ralph loop prompt
│       └── prd-prompt.md     # PRD generation prompt
├── template/                 # Copier template files
│   ├── ortus/                # Template version of ortus/ tooling
│   └── *.jinja               # Jinja-templated files
├── copier.yaml               # Template configuration
├── .beads/                   # Issue tracking
├── AGENTS.md                 # Session rules
├── CLAUDE.md                 # This file
└── .claude/                  # Claude Code settings
```

## Issue Tracking

This project uses **beads** (`bd`) for issue tracking. See **AGENTS.md** for workflow and session protocol.

## Important Files

* **CLAUDE.md** - AI agent instructions (this file)
* **AGENTS.md** - Session rules and landing-the-plane protocol
* **copier.yaml** - Template configuration and questions
* **template/** - Files that get copied to new projects

## Pro Tips for AI Agents

* Always use `--json` flags when available for programmatic use
* Use dependency trees to understand complex relationships
* Higher priority issues (0-1) are usually more important than lower (2-4)


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

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->
