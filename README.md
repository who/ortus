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

### Step 2: Generate Your PRD

Define what you're building by creating a PRD (Product Requirements Document). Use the included prompt template with your seed idea:

```bash
# Open a Claude session and paste the PRD prompt with your idea
claude

# In Claude, use the PRD-PROMPT template:
# "I want to build [YOUR IDEA HERE]..."
```

Or use the lisa.sh automation:

```bash
# Submit an idea for Lisa to process
bd create --title="A CLI tool that converts markdown to PDF with custom themes" --type=idea --assignee=lisa

# Start the Lisa loop (runs in background, processes ideas)
./lisa.sh
```

Lisa will:
1. Generate interview questions as beads (answer them with `bd comments add <id> "your answer"`)
2. Collect your answers when you close the question beads
3. Generate a structured PRD document at `prd/PRD-[project-name].md`
4. Wait for your approval (add 'approved' label to continue)
5. Create implementation tasks assigned to Ralph

See prd/PRD-PROMPT.md in the generated project for the full prompt template.

### Step 3: Import PRD into Beads

Convert your PRD into executable work items. Open a Claude session and use the Phase 4 prompt from `prd/PRD-PROMPT.md` to generate a beads setup script. Then run it:

```bash
chmod +x prd/beads-setup-*.sh
./prd/beads-setup-*.sh
```

Verify your work queue:

```bash
bd list                    # See all issues
bd ready                   # See what's ready to work on
bd dep tree <epic-id>      # Visualize dependencies
```

### Step 4: Run Ralph

Execute work through the Ralph automation loop:

```bash
# Run until all tasks complete (default)
./ralph.sh

# Complete exactly 1 task then exit
./ralph.sh --tasks 1

# Complete up to 5 tasks then exit
./ralph.sh --tasks 5
```

Ralph will:
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
├── PROMPT.md               # Ralph loop instructions
├── activity.md             # Work log
├── lisa.sh                 # PRD interview and generation loop
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
