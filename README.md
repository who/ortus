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

### Step 2: Kickstart Your Feature

Run `./idea.sh` to start. You'll choose between two paths:

**Path A: You have a PRD**
```bash
./idea.sh
# Choose [1] "Yes, I have a PRD"
# Provide the path to your PRD file
```
Your PRD gets decomposed into an epic with implementation tasks, ready for Ralph.

**Path B: You have an idea**
```bash
./idea.sh "A CLI tool that converts markdown to PDF with custom themes"
# Or run ./idea.sh and choose [2] "Nope, just an idea"
```

Claude will:
1. Expand your idea into a feature description
2. Run an interactive interview to clarify requirements
3. Generate a PRD document at `prd/PRD-[project-name].md`
4. Create implementation tasks from the approved PRD

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
├── idea.sh                 # PRD intake or idea → interview → tasks
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
