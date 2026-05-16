# Ortus

*Ortus* (Latin: "rising, origin, birth") — the point from which something springs into being.

An opinionated [Copier](https://github.com/copier-org/copier) template for scaffolding new projects with AI-assisted development workflows.

## Quick Start

### Prerequisites

```bash
# Install Copier
uv tool install copier --with copier-template-extensions
# or: pipx install copier
```

Copier is all you need to generate a project. To run any `ortus/*.sh` script — either inside this repo or a generated project — you also need the tools listed under [Requirements](#requirements) (notably `beads`, `dolt`, `claude`, `jq`, `rg`, `fd`).

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

Run `./ortus/idea.sh` to start. You'll choose between two paths:

**Path A: You have a PRD**
```bash
./ortus/idea.sh
# Choose [1] "Yes, I have a PRD"
# Provide the path to your PRD file
```
Your PRD gets decomposed into an epic with implementation tasks, ready for Ralph.

**Path B: You have an idea**
```bash
./ortus/idea.sh "A CLI tool that converts markdown to PDF with custom themes"
# Or run ./ortus/idea.sh and choose [2] "Nope, just an idea"
```

Claude will:
1. Expand your idea into a feature description
2. Run an interactive interview to clarify requirements
3. Generate a PRD document at `prd/PRD-[project-name].md`
4. Create implementation tasks from the approved PRD

Both paths auto-terminate via Claude Code's `/goal` directive — the interview ends when the feature is labeled `approved` with at least one child task in beads, and PRD decomposition ends when every work item in the PRD has a corresponding bd issue. You don't type anything to exit.

### Step 3: Run Goal

Then start the task implementation loop:

```bash
# Run until all ready work is drained (canonical /goal condition)
./ortus/goal.sh

# Complete exactly 1 task then exit
./ortus/goal.sh --tasks 1

# Run in background
./ortus/goal.sh &
```

`goal.sh` runs a single long-lived `claude -p "/goal CONDITION"` session against the queue. The `/goal` evaluator (Claude Haiku) judges the completion condition against the running transcript each turn and exits the loop when it answers yes:
1. Find the next ready task (`bd ready`)
2. Claim and implement it
3. Run verification (tests, linting)
4. Commit and push changes
5. Mark the task complete

#### Legacy: `ralph.sh` (deprecation shim)

`./ortus/ralph.sh` is a one-line deprecation shim that prints a notice to stderr and `exec`s `./ortus/goal.sh` with the same arguments — kept for at least one minor version so downstream Copier users have time to update muscle memory and any scripts that invoke it. New invocations should call `goal.sh` directly. See `ortus-dcr4` in beads for the Phase 5 cut-over notes.

##### Scoped runs

Pass `-c|--condition STR` to `goal.sh` to drive the queue until a specific milestone is reached instead of the default queue-drain condition. The evaluator (Claude Haiku) judges your condition against the running transcript each turn and exits the loop when it answers yes. Useful for finishing one epic, producing a single artifact, or smoke-testing a small change end-to-end without committing to "drain everything":

```bash
# Finish one epic and stop:
./ortus/goal.sh -c 'all children of bd-auth-epic are closed'

# Run until a specific report has been produced:
./ortus/goal.sh -c 'reports/goal-vs-ralph-2026-05-16.md exists and contains M1 PASS and M3 PASS'

# Combine with --tasks as a hard upper bound on work attempted:
./ortus/goal.sh --tasks 5 -c 'the auth middleware migration ships cleanly'
```

The condition is a free-form natural-language sentence; phrase it so a third party reading the transcript can decide pass/fail unambiguously. Keep it under the FR-004 4000-character ceiling.

## What You Get

```
my-project/
├── .beads/                 # Issue tracking database
├── .claude/                # Claude Code permissions
├── .github/workflows/      # CI pipeline
├── prd/
│   └── PRD-PROMPT.md       # PRD generation template
├── ortus/                  # Ortus automation scripts
│   ├── idea.sh             # PRD intake or idea → interview → tasks
│   ├── interview.sh        # Interactive interview → PRD → task creation
│   ├── goal.sh             # /goal-directive orchestrator (primary)
│   ├── ralph.sh            # Deprecation shim — execs goal.sh
│   └── tail.sh             # Log file watcher (ralph-*.log + goal-*.log)
├── src/                    # Your code goes here
├── CLAUDE.md               # AI guidance
└── prompt.md               # Ralph loop instructions
```

## Work Execution Policy

> **All implementation work MUST go through a Goal loop** (`./ortus/goal.sh`). `ralph.sh` is a deprecation shim that execs `goal.sh`.

- Direct coding is not allowed in interactive Claude sessions
- Create beads issues instead of implementing directly
- A Goal loop executes the actual work
- Research and planning are allowed without an orchestrator

## Requirements

Install these tools before using generated projects:

| Tool | Purpose |
|------|---------|
| [copier](https://github.com/copier-org/copier) | Project generator |
| [beads](https://github.com/steveyegge/beads) **v1.0.0+** | Issue tracking |
| [dolt](https://docs.dolthub.com/introduction/installation) | SQL server backing beads |
| [claude](https://github.com/anthropics/claude-code) | Claude CLI |
| [jq](https://jqlang.github.io/jq/) | JSON processing |
| [rg](https://github.com/BurntSushi/ripgrep) | Fast search (ripgrep) |
| [fd](https://github.com/sharkdp/fd) | Fast file finder |

**Optional: [CodeGraph](https://github.com/colbymchenry/codegraph).** The investigation step runs faster when CodeGraph is installed in the project — it provides a pre-indexed semantic graph of the codebase, so investigation can resolve symbols, callers, and call graphs in one MCP call instead of dozens of grep/glob/Read calls. **Not required.** The orchestrator detects CodeGraph at runtime: if `.codegraph/` exists and the MCP server is reachable, it gets used; otherwise the loop falls back silently to the default search behavior. When CodeGraph is present, closure comments and PRD decomposition outputs also include CodeGraph-derived structural data (a parseable change record on closures; reference checks and likely-touched files on decompositions); when absent, both remain byte-equivalent to the pre-CodeGraph baseline.

### beads v1.0.0+ required

Ortus requires **beads v1.0.0** (released 2026-04-03) or later. The v0.55.0 → v1.0.0 arc completed beads' migration to Dolt as the sole storage backend; earlier versions used pre-Dolt SQLite/noms/JSONL modes that this workflow no longer supports. Ortus configures beads in Dolt server mode so concurrent orchestrator loops (and parallel sessions) do not contend on an embedded flock; the `dolt` binary must therefore be available on `PATH`. Install via `brew install beads` or from [the v1.0.0 release](https://github.com/gastownhall/beads/releases/tag/v1.0.0) (or later).

Remote sync uses `bd dolt push` / `bd dolt pull`. The v0.55-era `bd sync` command was removed in v1.0.0. See [AGENTS.md](AGENTS.md) for the full session-close workflow.

### Sandbox + bd setup

Generated projects run the orchestrator inside the OS sandbox, which blocks loopback TCP by default. `bd` (beads) needs loopback to reach its Dolt SQL server, so the generated `.claude/settings.json` exempts `bd` from sandbox network containment:

```json
{
  "sandbox": {
    "excludedCommands": ["bd", "bd *"]
  }
}
```

Both entries are required: `bd` matches bare invocations, `bd *` matches subcommands. If you customize `.claude/settings.json`, preserve both `bd` and `bd *` entries in `sandbox.excludedCommands`.

**Never pipe bd into other commands or use `xargs bd`.** The exemption only applies when `bd` is directly invoked. Inside `bd list | jq ... | xargs bd show`, the inner `bd` is a child of `xargs` and inherits the sandboxed network namespace, where it hangs on the Dolt connection. Use multi-id `bd show <id1> <id2> ...` (accepts space-separated ids natively) instead of piping. Full context lives in the generated project's README under "Sandbox + bd setup".

### Development

Run `make parity` before committing changes to `ortus/` or `template/ortus/`. It detects drift between the canonical tree and the Jinja mirror shipped in the template.

### Language-Specific Tools

- **Python:** [uv](https://github.com/astral-sh/uv), [ruff](https://github.com/astral-sh/ruff)
- **TypeScript:** Node.js 20+, npm/pnpm/yarn/bun
- **Go:** Go 1.22+, [golangci-lint](https://github.com/golangci/golangci-lint)
- **Rust:** [rustup](https://rustup.rs/), clippy, rustfmt

## License

MIT
