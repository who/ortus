# Ortus

*Ortus* (Latin: "rising, origin, birth") — the point from which something springs into being.

An opinionated [Copier](https://github.com/copier-org/copier) template for scaffolding new projects with AI-assisted development workflows.

## Quick Start

### Prerequisites

```bash
# Install Copier
uv tool install copier
# or: pipx install copier
```

### Step 1: Generate Your Project

```bash
copier copy gh:who/ortus ./my-project
cd my-project
```

The generator will:
- Ask you about your project (name, language, framework, etc.)
- Scaffold the project structure
- Initialize git and beads automatically

You now have a blank slate ready for development.

### Step 2: Create and Refine Your Feature

Define what you're building by creating a feature and going through the interview process:

```bash
# Quick way: use idea.sh to create a feature
./idea.sh "A CLI tool that converts markdown to PDF with custom themes"

# Or create manually:
bd create --title="A CLI tool that converts markdown to PDF" --type=feature --assignee=ralph
```

Run the interactive interview to refine requirements:

```bash
# Claude asks you questions about the feature
./interview.sh
```

interview.sh will:
1. Ask clarifying questions about your feature
2. Generate a PRD document at `prd/PRD-[project-name].md`
3. Create implementation tasks from the approved PRD

### Step 3: Run Ralph

Then start the task implementation loop:

```bash
# Run until all tasks complete
./ralph.sh

# Complete exactly 1 task then exit
./ralph.sh --tasks 1

# Run in background
./ralph.sh &
```

Ralph implements tasks:
1. Find the next ready task (`bd ready`)
2. Claim and implement it
3. Run verification (tests, linting)
4. Commit and push changes
5. Mark the task complete

## What You Get

```
my-project/
├── .beads/                 # Issue tracking database
├── .claude/                # Claude Code permissions
├── .github/workflows/      # CI pipeline
├── prd/
│   └── PRD-PROMPT.md       # PRD generation template
├── src/                    # Your code goes here
├── CLAUDE.md               # AI guidance
├── prompt.md               # Ralph loop instructions
├── interview.sh            # Interactive interview → PRD → task creation
└── ralph.sh                # Task implementation loop
```

## Work Execution Policy

> **All implementation work MUST go through Ralph loops.**

- Direct coding is not allowed in interactive Claude sessions
- Create beads issues instead of implementing directly
- Ralph loops execute the actual work
- Research and planning are allowed without Ralph

## Requirements

Install these tools before using generated projects:

| Tool | Purpose |
|------|---------|
| [copier](https://github.com/copier-org/copier) | Project generator |
| [beads](https://github.com/steveyegge/beads) | Issue tracking |
| [bv](https://github.com/steveyegge/beads-viewer) | Beads TUI viewer |
| [claude](https://github.com/anthropics/claude-code) | Claude CLI |
| [jq](https://jqlang.github.io/jq/) | JSON processing |
| [rg](https://github.com/BurntSushi/ripgrep) | Fast search (ripgrep) |
| [fd](https://github.com/sharkdp/fd) | Fast file finder |

### Language-Specific Tools

- **Python:** [uv](https://github.com/astral-sh/uv), [ruff](https://github.com/astral-sh/ruff)
- **TypeScript:** Node.js 20+, npm/pnpm/yarn/bun
- **Go:** Go 1.22+, [golangci-lint](https://github.com/golangci/golangci-lint)
- **Rust:** [rustup](https://rustup.rs/), clippy, rustfmt

## License

MIT
