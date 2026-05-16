# Ortus

[![test](https://github.com/who/ortus/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/who/ortus/actions/workflows/test.yml)

*Ortus* (Latin: "rising, origin, birth") — the point from which something springs into being.

A global Python CLI for bd-driven Claude Code workflows. One install, one binary, eight verbs.

## Install

**Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) on PATH.** Ortus is distributed via PyPI and installed by uv; we don't auto-install uv (see PRD §NFR-004).

**One-liner (recommended):**

```bash
curl -fsSL https://github.com/who/ortus/releases/latest/download/install.sh | sh
```

**Direct PyPI:**

```bash
uv tool install ortus
ortus --version
```

**From source / pinned commit:**

```bash
uv tool install git+https://github.com/who/ortus.git
# Pin a specific tag/branch:
uv tool install 'git+https://github.com/who/ortus.git@v0.1.0'
```

**Troubleshooting:**

| Symptom | Fix |
|---|---|
| `uv: command not found` | Install uv: `curl -LsSf https://astral.sh/uv/install.sh \| sh` (see [uv docs](https://docs.astral.sh/uv/getting-started/installation/)) |
| `ortus: command not found` after install | `uv tool update-shell` then open a new shell |
| `bd: command not found` | `brew install beads` (mac) or grab a release from https://github.com/gastownhall/beads/releases |

## Quick start

```bash
# Bootstrap a fresh repo (creates .beads/, .claude/settings.json, AGENTS.md, .gitignore, .ortusrc)
ortus init ~/code/myproj

# Verify prereqs in that repo
ortus check ~/code/myproj

# Decompose a PRD into bd issues
ortus plan ~/code/myproj path/to/feature.md

# Or run the idea→interview→PRD→tasks flow with no PRD path
ortus plan ~/code/myproj

# Drive the bd queue to zero (long-lived /goal session)
ortus grind ~/code/myproj

# Bounded variants
ortus grind ~/code/myproj --tasks 5
ortus grind ~/code/myproj -c "auth migration ships cleanly"
```

## The eight verbs

| Verb | Purpose |
|---|---|
| `ortus init <repo>` | Bootstrap a fresh repo with bd + .claude/settings.json + AGENTS.md + .ortusrc + .gitignore |
| `ortus check <repo>` | Verify bd/claude/jq + sandbox prereq + hook-enabled + settings shape; strictly read-only |
| `ortus plan <repo> [<PRD>]` | Decompose a PRD into bd issues, or interview-then-PRD-then-decompose if no PRD path |
| `ortus grind <repo>` | Drive the bd queue via a long-lived `claude -p '/goal CONDITION'` session |
| `ortus interview <repo> [<feature-id>]` | Interactive PRD-building interview for an open feature |
| `ortus tail <repo>` | Follow `logs/{grind,goal,ralph}-*.log` with stream-json filtering |
| `ortus triage <repo>` | Walk the human-flagged bd queue interactively |
| `ortus human <repo>` | Render `HUMAN-TODO.md` from bd issues flagged for a human decision |

Run `ortus <verb> --help` for flags. Run `ortus --version` for the installed version.

### Platform support

| Platform | Status | Notes |
|---|---|---|
| Linux (Ubuntu/WSL2) | full | requires `bubblewrap` for `ortus grind` |
| macOS | full | Seatbelt (`sandbox-exec`) is built-in |
| Windows | partial | `init`, `check`, `tail`, `human` work; `grind` requires bwrap/sandbox-exec and is currently Linux/Mac-only |

## Prerequisites

| Tool | Why | Install |
|---|---|---|
| **uv** | install + run ortus | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |
| **bd** (beads) v1.0.0+ | issue tracking (backed by embedded Dolt) | `brew install beads` or [GH release](https://github.com/gastownhall/beads/releases) |
| **claude** | the model running inside `ortus grind` | [Claude Code](https://github.com/anthropics/claude-code) |
| **jq** | bd JSON post-processing | `brew install jq` / `apt install jq` |
| **bwrap** (Linux) or **sandbox-exec** (Mac) | OS-level sandbox for `ortus grind` | `apt install bubblewrap` / built into macOS |

Optional: **[CodeGraph](https://github.com/colbymchenry/codegraph)**. If `.codegraph/` exists in a project, `ortus grind`'s prompts use it for faster symbol/caller/callee lookups; otherwise the loop falls back to grep + Read.

## Why ortus

- **One install, all projects.** `uv tool install ortus` once; every repo uses the same canonical tooling. No more `copier update` chasing N repos.
- **`bd ready` IS the queue.** No README task lists, no TodoWrite scratchpads. The queue is data.
- **`/goal` IS the loop.** Termination is a hook decision based on observable bd state, not a sentinel grep.
- **Sandboxed by default.** `ortus grind` refuses to launch unless bwrap/Seatbelt is available; cache directories are project-local; network is allowlist-only via `.claude/settings.json`.

## Configuration

Optional `<repo>/.ortusrc` (TOML) overrides `~/.ortusrc`:

```toml
prefix = "myproj"       # bd issue-id prefix
project_type = "python" # python | typescript | go | rust | polyglot
```

Per-repo or user-wide prompt overrides live at `<repo>/.ortus/prompts/<name>.md` or `~/.ortus/prompts/<name>.md`; the bundled defaults under `src/ortus/prompts/` are the fallback (FR-025).

## Session-close protocol

When ending a work session, push your work:

```bash
bd close <id> --reason "..."
git add -A && git commit -m "..."
bd dolt push
git push
```

Work is not done until pushed. The generated `AGENTS.md` repeats this in every project.

## Development

```bash
# Local editable install
uv pip install -e '.[dev]'

# Tests
pytest                              # unit + integration (fast)
pytest -m smoke                     # end-to-end smoke
pytest --slow                       # everything, including real-claude smoke
```

## License

MIT
