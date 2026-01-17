# Project Archetype

A Copier template for scaffolding new projects with built-in AI-assisted development workflows.

## What You Get

- **Beads issue tracking** - Git-native issue management with `bd` CLI
- **Claude Code integration** - Pre-configured CLAUDE.md with AI guidance
- **Ralph loops** - Automated task execution via `ralph.sh` and `mega-ralph.sh`
- **PRD templates** - Structured product requirements workflow
- **GitHub Actions CI** - Language-specific CI pipelines
- **Code quality tools** - Linter configurations per language

## Usage

```bash
copier copy ~/code/copier-project-archetype ./my-new-project
```

Or from a git remote:

```bash
copier copy gh:username/copier-project-archetype ./my-new-project
```

### Wizard Prompts

| Prompt | Description |
|--------|-------------|
| `project_name` | Lowercase name with hyphens (e.g., `my-api`) |
| `project_description` | Short project description |
| `author_name` | Your name |
| `author_email` | Your email |
| `language` | Python, TypeScript, Go, Rust, or Other |
| `package_manager` | Language-specific (uv, npm, cargo, etc.) |
| `framework` | Optional web framework |
| `linter` | Code linter (ruff, eslint, clippy, etc.) |
| `github_username` | For repository URL |
| `license` | MIT, Apache-2.0, GPL-3.0, etc. |

## Requirements

### System Dependencies

Install these tools before using generated projects:

| Tool | Purpose | Install |
|------|---------|---------|
| **beads** | Issue tracking | `cargo install beads` or [releases](https://github.com/Dicklesworthstone/beads) |
| **bv** | Beads TUI viewer | `cargo install beads_viewer` or [releases](https://github.com/Dicklesworthstone/beads_viewer) |
| **claude** | Claude CLI | `npm install -g @anthropic-ai/claude-cli` |
| **rg** | Fast search (ripgrep) | `apt install ripgrep` / `brew install ripgrep` |
| **fd** | Fast find | `apt install fd-find` / `brew install fd` |
| **jq** | JSON processing | `apt install jq` / `brew install jq` |

### Language-Specific

**Python:**
```bash
# uv (recommended)
curl -LsSf https://astral.sh/uv/install.sh | sh

# ruff
uv tool install ruff
```

**TypeScript/Node.js:**
```bash
# Node.js 20+
# npm/pnpm/yarn
```

**Go:**
```bash
# Go 1.22+
go install github.com/golangci/golangci-lint/cmd/golangci-lint@latest
```

**Rust:**
```bash
# Rust via rustup
rustup component add clippy rustfmt
```

## Generated Project Structure

```
my-project/
├── .beads/                 # Issue tracking database
│   ├── config.yaml
│   └── .gitignore
├── .claude/
│   └── settings.json       # Claude Code permissions
├── .github/
│   └── workflows/
│       └── ci.yml          # GitHub Actions CI
├── prd/
│   ├── PRD-PROMPT.md       # PRD generation template
│   └── .gitkeep
├── src/
│   └── .gitkeep
├── .gitignore              # Language-specific ignores
├── .mcp.json               # MCP server config
├── AGENTS.md               # Session protocol
├── CLAUDE.md               # AI guidance
├── PROMPT.md               # Ralph loop instructions
├── activity.md             # Work log
├── mega-ralph.sh           # Continuous task runner
└── ralph.sh                # Single task runner
```
## Workflows

### Ralph Loop (Single Task)

```bash
./ralph.sh [max_iterations]
```

Executes one beads task:
1. Finds ready work (`bd ready`)
2. Claims highest priority task
3. Implements the change
4. Runs verification
5. Commits and pushes

### Mega Ralph (Continuous)

```bash
./mega-ralph.sh [iterations_per_task] [idle_sleep]
```

Continuously processes tasks until queue is empty.

### PRD Generation

1. Edit `prd/PRD-PROMPT.md` with your topic
2. Run with Claude to generate structured PRD
3. Convert PRD to beads issues using Phase 4 prompt

## Work Execution Policy

Generated projects enforce:

> **All implementation work MUST go through Ralph loops.**

- Direct coding is not allowed in interactive Claude sessions
- Create beads issues instead of implementing directly
- Ralph loops execute the actual work
- Research and planning are allowed without Ralph

## License

MIT
