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

Or use the helper script:

```bash
./generate-prd.sh "A CLI tool that converts markdown to PDF with custom themes"
```

This starts an interactive session where Claude will:
1. Interview you about your idea (3-5 questions at a time)
2. Generate a structured PRD document
3. Iterate up to 5 times to refine it
4. Save the result to `prd/PRD-[project-name].md`

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
# Single task execution
./ralph.sh

# Continuous execution until queue is empty
./mega-ralph.sh
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
├── generate-prd.sh         # PRD generation helper
├── ralph.sh                # Single task runner
└── mega-ralph.sh           # Continuous task runner
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
| [beads](https://github.com/Dicklesworthstone/beads) | Issue tracking |
| [bv](https://github.com/Dicklesworthstone/beads_viewer) | Beads TUI viewer |
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
