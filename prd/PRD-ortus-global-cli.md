# PRD: Ortus as a global Python+uv CLI

## Metadata

- **Feature ID**: ortus-global-cli (proposed; assign at decomposition)
- **Project Type**: Ortus core tooling (canonical `ortus/` repo)
- **Created**: 2026-05-16
- **Author**: Claude (cross-reading current ortus codebase, this session's decision history, PRD-goal-directive.md as structural reference, uv/typer/rich docs)
- **Interview Confidence**: High (user actively pinned each major design decision in-session: greenfield assumption, umbrella `ortus <verb>` command shape, 8 verbs + main verb `grind`, Python+uv as language+distribution, copier/template dies entirely)
- **Successor To**: prd/PRD-goal-directive.md (sentinel-grep → /goal migration). That PRD completed the orchestrator rewrite; this PRD completes the distribution rewrite. Together they take ortus from "vendored bash, per-project copy" to "global Python CLI, one install across all projects."

---

## Overview

### Problem Statement

Today's ortus is a vendored bash toolkit distributed via copier. Every new project gets its own copy of `ortus/` (scripts, prompts, lib helpers, ~14KB of files). This shape was a fine v0 but compounds three frictions as the number of generated projects grows:

1. **N×M update problem.** Every change to canonical ortus (e.g., the dolt-orchestration rip-out we shipped earlier today, the /goal migration we just landed) must be replayed into N downstream projects via `copier update`. Each update can conflict if the downstream project customized anything. With ~20 projects and a roughly monthly ortus release cadence, that's ~240 update-conflicts-or-cleanup events per year.

2. **Per-project cognitive overhead.** Every generated project carries ~14KB of bash scripts that the user shouldn't be reading or maintaining — they're *library code*, not project code. But they live in the project tree, are visible to grep, show up in code reviews, and silently get stale relative to canonical.

3. **Parity discipline as load-bearing dev tax.** Inside canonical ortus, every change to `ortus/X.sh` must mirror to `template/ortus/X.sh`; `make parity` enforces this. Failure cases observed in-session: the `ortus-c8xa` issue ralph filed mid-run was *exactly* this — Phase 1 refactored canonical `ralph.sh` to source `lib/sandbox.sh` + `lib/cache.sh` but forgot to mirror the same refactor into `template/ortus/ralph.sh`, breaking `make parity`. The discipline is correct but the mechanism is fragile.

4. **Customization friction.** Editing a vendored prompt (e.g., tweaking `ralph-prompt.md` for one project) means the next `copier update` will conflict on the user's customization. There's no clean "system default + per-project override" layering.

### Proposed Solution

Reframe ortus as a **global Python CLI distributed via uv**. Each repo holds only repo-specific state (`.beads/`, `.claude/settings.json`, optional `.ortusrc`); the *tooling* lives once on the user's machine and is invoked from anywhere.

User-facing shape:

```bash
# One-time install (works on Mac/Linux/Windows):
curl -fsSL https://github.com/who/ortus/releases/latest/download/install.sh | sh
# (Requires uv to be on PATH; errors out with install hint if missing.
#  Internally: verifies uv, runs `uv tool install ortus`, prints next steps.)

# Bootstrap a new repo:
ortus init ~/code/myproj

# Decompose a PRD into bd issues:
ortus plan ~/code/myproj ~/Documents/feature-x.md

# Drive the autonomous loop:
ortus grind ~/code/myproj    # or just `ortus grind` if cwd is the repo
```

Eight verbs total, all under one umbrella: **`init`, `plan`, `grind`, `interview`, `tail`, `triage`, `human`, `check`**.

### Success Metrics

- **M1 — Install UX**: A user with `uv` and `curl` available can install ortus in a single command in ≤ 15 seconds wall-clock (download install.sh + `uv tool install ortus`). Measured on a fresh VM with uv pre-installed. (Users without uv get a clear error pointing at https://docs.astral.sh/uv/getting-started/installation/.)
- **M2 — Bootstrap UX**: `ortus init <empty-repo>` completes in ≤ 5 seconds, producing a working `.beads/`, `.claude/settings.json`, and starter `AGENTS.md`.
- **M3 — Update propagation**: Upgrading ortus to a new version requires exactly one command (`uv tool upgrade ortus`) and immediately benefits all repos on the next invocation — vs. today's N copier-updates.
- **M4 — Parity discipline retired**: The canonical ortus repo no longer maintains `template/` or `make parity`. Single source of truth for every file. Verified by absence of those paths in the new repo structure.
- **M5 — Verb coverage**: All 8 verbs functional end-to-end on a fresh `ortus init` repo. Each verb passes a smoke test (specified per-verb in §Functional Requirements).
- **M6 — Greenfield assumption verified**: No `ortus migrate` verb is built or shipped. The 20 existing copier'd projects either keep running on a frozen bash-era ortus or are manually rebuilt via `ortus init` at the operator's discretion; PRD does not handle them.

---

## Background & Context

### Why now?

Three forcing functions converged this session:

1. **The /goal migration just shipped** (PRD-goal-directive.md, commits ending at `884c85b feat(goal): precheck disableAllHooks ...`). The orchestrator is now a long-lived `claude -p "/goal CONDITION"` session, not a sentinel-grep loop. With the orchestrator rewrite complete, the *distribution* rewrite is the next natural improvement.

2. **The parity discipline became actively painful in-session.** Watching ralph file `ortus-c8xa` and stall on a parity gap was a concrete reminder that canonical-vs-template duplication is structural debt, not just aesthetic.

3. **uv has matured** (Astral, Rust-based Python package manager) to the point where the install UX for a Python CLI rivals Go's single-binary model. Specifically: `uv tool install ortus` is one command; uv handles Python installation; `uvx ortus grind` enables trial-without-install (npx-equivalent). The historic case against Python CLIs (pip UX, virtualenv friction, version variance) is largely retired by uv.

### Prior art and alternatives considered

| Approach | Why not |
|---|---|
| **Status quo (copier vendor copy)** | The thing we're moving off of. N×M update pain documented above. |
| **Git submodule** | Trades `copier update` for `git submodule update` — same N×M problem with worse UX (submodule recurse forgetfulness, CI breakage). |
| **System install + per-project shim** | Hybrid where each project keeps tiny shim files pointing at a system install. Preserves muscle memory but still N copies of shim files; less clean than a global CLI. |
| **Go single binary** | Strong contender on install UX (single static file, no runtime). Rejected on user-stated preference for Python familiarity. Ecosystem coherence with bd (also Go) was the strongest non-preference argument; user explicitly waived it. |
| **Bash restructured into global form** | Cheapest path (existing code is bash); rejected because it caps Windows reach at zero (bash native on Windows is essentially unsupported) and locks ortus into bash's testing/maintenance pain for the long term. |
| **Python + plain pip** | Without uv, the install story is messy: `pip install --user` vs venv vs `--break-system-packages` on newer systems. uv collapses this into a single clean primitive. |
| **Python + pipx** | Pipx solves the isolation problem but has a smaller install base than uv and lacks uv's Python-install management. Strict subset of uv's value. |
| **NPM global package** | Forces a Node dep on backend devs; bash-via-`bin:` feels wrong; weaker than uv for Python-native code. |

### How uv actually works (verbatim from docs, summarized)

- `uv tool install <pkg>` — installs a Python CLI tool in an isolated environment managed by uv; binary on PATH; analogous to pipx but faster and with auto-Python-install.
- `uv tool upgrade <pkg>` — clean upgrade path.
- `uvx <pkg> <args>` — run a tool without permanent install; equivalent to `npx`.
- `uv python install <version>` — uv manages Python interpreters; users don't need to have a specific Python beforehand.
- Installs are fast (Rust-based resolver and installer).
- Lockfile support (`uv.lock`) for reproducible installs.
- Astral maintains uv; well-funded, actively developed, growing install base (~25-40% of Python devs as of 2026).

### How current ortus actually works (audited from in-session reads)

Mechanics that the global-CLI version must preserve:

| Invariant | Where it lives today | Why it matters |
|---|---|---|
| Single-instance per repo | `ortus/ralph.sh:50-93` (flock guard); `ortus/goal.sh:155-211` (same pattern) | Prevents two autonomous loops racing on the same bd workspace and double-claiming. |
| Sandbox smoke test | `ortus/lib/sandbox.sh` (post-/goal-migration); sourced by ralph.sh + goal.sh | Fails fast if `bwrap` (Linux) or `sandbox-exec` (Mac) is missing; not skippable. |
| Cache relocation | `ortus/lib/cache.sh` (post-/goal-migration); sourced by both orchestrators | Points XDG/per-tool caches at project-local `.cache/` for sandbox-writable surface. |
| Hook precheck (`disableAllHooks`) | `ortus/goal.sh:118-160` (post-ortus-sooj) | Detects when /goal would silently degrade and exits 1 with a friendly error. |
| `bd` sandbox exemption | Generated `.claude/settings.json` — `sandbox.excludedCommands: ["bd", "bd *"]` | Required for bd to reach its embedded DB / loopback under the OS sandbox. |
| `cleanup_children` trap | `ortus/ralph.sh:114-120` and equivalent in goal.sh | SIGKILLs leftover `claude -p` children on graceful EXIT/INT/TERM. |
| Per-run log files | `logs/ralph-<timestamp>.log`, `logs/goal-<timestamp>.log` | tail.sh follows both prefixes. |

Mechanics that the global-CLI version retires or replaces:

| Today | Replaced by |
|---|---|
| `template/` directory + `make parity` | Single source of truth in `src/ortus/`; templates ship as package data. |
| Vendored `ortus/*.sh` scripts per project | Python `src/ortus/commands/*.py` shipped once globally. |
| `copier copy` for new projects | `ortus init` (Python CLI verb). |
| `copier update` for existing projects | Not supported (greenfield assumption). |
| `copier.yaml` config | `ortus init` CLI flags. |
| Jinja `*.jinja` template files | Templates rendered by `jinja2` library inside `ortus init`, reading bundled `src/ortus/templates/`. |

---

## Users & Personas

### Primary persona

You (the operator). Solo developer with ~20 ortus-generated projects today. Wants:

- One install, used everywhere.
- Familiar Python syntax (locked in-session as the primary language preference).
- Fast install via curl-installer or `uv tool install` (uv assumed present).
- Per-project override surface for prompts and config without forking ortus.
- Same `./ortus/grind` mental model retired in favor of `ortus grind` (single PATH entry instead of per-repo wrappers).

### Secondary persona (deferred)

Hypothetical other developers using ortus. Out of scope for v1. Distribution choices (uv, optional Homebrew tap, optional `curl … | sh`) keep this audience addressable later without architecture changes.

### User Goals

- **G1** — "I want to install ortus once and have it work across all my repos."
- **G2** — "I never want to mirror a code change across N project copies again."
- **G3** — "I want to invoke ortus from anywhere with a clean command surface, like git."
- **G4** — "I want to customize prompts or config per-project without forking ortus or fighting copier-update."
- **G5** — "I want the install to be a single command on any platform I use."
- **G6** — "I don't want to maintain bash scripts long-term; I prefer Python syntax."

### Current Workflows (and what is painful today)

- **Workflow A — Bootstrap a new project.** Today: `copier copy github.com/who/ortus my-new-proj` → vendored 14KB of ortus scripts land in `my-new-proj/ortus/`. **Pain:** project carries library code as project code; user can't grep their own code without seeing ortus internals.

- **Workflow B — Update ortus across projects.** Today: `cd ~/code/proj-1 && copier update` × 20. **Pain:** O(N) per update; merge conflicts on any per-project customization; easy to forget a project and let it drift.

- **Workflow C — Run autonomous loop.** Today: `cd ~/code/proj && ./ortus/goal.sh`. **Pain:** must `cd` first; relative-path invocation is unusual for modern CLIs; the `ortus/` subfolder pollutes the project tree.

- **Workflow D — Decompose a PRD into bd issues.** Today: `cd ~/code/proj && ./ortus/idea.sh --prd ~/some-prd.md` — but idea.sh `cd`s into the PRD's git toplevel, creating issues in the wrong workspace if the PRD lives outside the project (the exact bug we hit earlier in this session). **Pain:** path-handling fragility.

- **Workflow E — Customize a prompt for one project.** Today: edit the vendored `ortus/prompts/ralph-prompt.md`; next `copier update` conflicts. **Pain:** no clean override layer.

---

## Requirements

### Functional Requirements

**CLI shape and routing:**

- **[P0] FR-001** — The system shall provide a global executable named `ortus` (installed by uv to a directory on PATH) that dispatches to subcommand verbs.
- **[P0] FR-002** — The CLI shall expose exactly 8 verbs: `init`, `plan`, `grind`, `interview`, `tail`, `triage`, `human`, `check`. No flat top-level commands besides `ortus` itself.
- **[P0] FR-003** — Each verb shall accept `<repo>` as its first positional argument; if omitted, `<repo>` defaults to `$PWD` (the current working directory) **with no walk-up to parent directories**. If `$PWD` does not contain a `.beads/` directory, the verb shall exit 1 with: `"no .beads/ in current directory; cd to your project root or pass <repo> explicitly (e.g., ortus grind ~/code/myproj)"`. The explicit `<repo>` arg, when provided, is always used as-is and is not subject to walk-up either. Rationale: walk-up semantics have known footguns (stale ancestor `.beads/`, `~/.beads/` accidentally targeted from anywhere in $HOME, nested workspaces, symlink traversal, CI runner confusion); the slight UX cost of requiring an exact location is the right trade.
- **[P0] FR-004** — `ortus --help` shall list all 8 verbs with one-line descriptions. `ortus <verb> --help` shall show verb-specific usage and flags.
- **[P0] FR-005** — `ortus --version` shall print the installed ortus version (from package metadata).

**Bootstrap verb:**

- **[P0] FR-006** — `ortus init <repo>` shall:
  - Create `<repo>/.beads/` via `bd init` subprocess call (using the bd binary on PATH).
  - Write `<repo>/.claude/settings.json` from a bundled template, with `sandbox.excludedCommands: ["bd", "bd *"]` and the bd-prime hooks correctly configured.
  - Write `<repo>/.ortusrc` (YAML or TOML; format pinned in §Technical Decisions) with project prefix and other defaults.
  - Write a starter `<repo>/AGENTS.md` (small, project-typeable) from a bundled template.
  - Refuse to run if `<repo>/.beads/` already exists, unless `--force` is passed.
- **[P0] FR-007** — `ortus init` shall accept `--prefix <str>` to override the bd issue prefix; default is the repo directory's basename (e.g., `myproj` from `~/code/myproj`).
- **[P1] FR-008** — `ortus init` shall accept `--project-type <type>` (`other`, `python`, `typescript`, etc.) to select project-type-specific templates if defined.
- **[P1] FR-009** — `ortus init` shall be idempotent under `--force`: re-running on an existing repo preserves user customizations in `.ortusrc` and overwrites only what's explicitly changed by ortus updates.

**Autonomous-loop verb:**

- **[P0] FR-010** — `ortus grind <repo>` shall be functionally equivalent to today's `goal.sh` invocation: spawns a long-lived `claude -p "/goal CONDITION"` session with `--dangerously-skip-permissions`, `--output-format stream-json`, `--verbose`, and (when `--fast` is passed) `--fast`.
- **[P0] FR-011** — `ortus grind` shall accept the same flag surface as today's goal.sh: `--fast`, `--idle-sleep N`, `--tasks N`, `--iterations N`, `--docker`, `-c/--condition`.
- **[P0] FR-012** — `ortus grind` shall preserve all current goal.sh invariants: flock guard at `<repo>/.beads/ortus.flock`, sandbox smoke test, `--docker` precondition check, cache env-var exports, hook precheck (`check_hooks_enabled`), `cleanup_children` trap, tee output to `<repo>/logs/grind-<timestamp>.log`.
- **[P0] FR-013** — `ortus grind` shall avoid the goal.sh terminal-leak bug (`ortus-6q8v`): claude's stream-json must go to the log file, not to the launching terminal.

**Plan verb (replaces idea.sh):**

- **[P0] FR-014** — `ortus plan <repo> <PRD>` shall decompose the PRD at the given path into bd issues in `<repo>/.beads/`. Equivalent to today's `idea.sh --prd <PRD>` but with explicit `<repo>` arg to eliminate the cd-to-PRD-dir bug.
- **[P0] FR-015** — `ortus plan <repo>` (no PRD arg) shall launch the interactive idea-expansion flow (equivalent to today's `idea.sh` no-args path): prompts user for an idea, expands it via claude, creates a feature bead, launches the interview flow.
- **[P1] FR-016** — `ortus plan` shall emit a clear summary of created issues (count, ids, top-level epic structure) and print copy-pasteable next-step commands (`ortus grind <repo>`).

**Interview verb:**

- **[P0] FR-017** — `ortus interview <repo>` shall replicate today's `interview.sh` behavior: launches a claude session with `AskUserQuestion + Bash(bd:*) + Read` allowedTools and the interview prompt, walking the user through feature requirements gathering.
- **[P1] FR-018** — `ortus interview <repo> <feature-id>` shall jump directly to interviewing the specified bd feature; default (no feature-id) prompts the user to pick from open features.

**Tail verb:**

- **[P0] FR-019** — `ortus tail <repo>` shall follow `<repo>/logs/grind-*.log` and `<repo>/logs/ralph-*.log` (legacy) in human-readable form, equivalent to today's `tail.sh`.
- **[P1] FR-020** — `ortus tail` shall support `--raw` to emit the underlying stream-json without filtering.

**Triage verb:**

- **[P0] FR-021** — `ortus triage <repo>` shall replicate today's `triage.sh` behavior (interactive walk-through of the bd human queue via AskUserQuestion + Bash(bd:*) + Read).

**Human verb:**

- **[P0] FR-022** — `ortus human <repo>` shall write `<repo>/HUMAN-TODO.md` summarizing all bd issues currently flagged for human decision, with the pros/cons rendering when claude's blocker comments contain structured option markers. Equivalent to today's `human.sh` + the pros/cons enhancement.

**Check verb:**

- **[P0] FR-023** — `ortus check <repo>` shall verify prerequisites and print a health diagnostic:
  - `bd` on PATH (with version)
  - `claude` on PATH (with version)
  - `jq` on PATH (for log parsing helpers)
  - Sandbox prereqs (`bwrap` on Linux; `sandbox-exec` on Mac)
  - `<repo>/.beads/` exists and is bd-readable
  - `<repo>/.claude/settings.json` exists; `disableAllHooks` is not set; bd is in `sandbox.excludedCommands`
  - `<repo>/.ortusrc` parses cleanly
  - Optional: `<repo>/.ortus/prompts/` (if exists) shadows known package prompts
- **[P1] FR-024** — `ortus check` shall exit 0 if all green; exit 1 with a per-check summary if any fail.

**Layered resolution:**

- **[P0] FR-025** — Prompts shall resolve in precedence order:
  1. `<repo>/.ortus/prompts/<name>.md` (project override; highest)
  2. `~/.ortus/prompts/<name>.md` (user-global override)
  3. Package-bundled `src/ortus/prompts/<name>.md` (default; lowest)
- **[P0] FR-026** — Config shall resolve via `.ortusrc` files in the same three-layer precedence (project → user → package defaults).

**Distribution and install:**

- **[P0] FR-027** — The package shall be installable via `uv tool install ortus` (assuming uv is present).
- **[P0] FR-028** — A shell installer script (`install.sh`) shall be published as a release asset on GitHub on every tagged release. The canonical install URL is `https://github.com/who/ortus/releases/latest/download/install.sh`, which GitHub redirects to the most recent release's asset. The script shall:
  - **Require `uv` to be on PATH.** If `uv` is missing, exit 1 with a clear error message pointing at the official uv install docs (`https://docs.astral.sh/uv/getting-started/installation/`) and a copy-pasteable hint (`curl -LsSf https://astral.sh/uv/install.sh | sh`). **The installer does NOT install uv itself** — uv is taken as a precondition. This keeps install.sh small, makes the dependency on uv explicit to the user, and avoids the maintenance cost of tracking changes to Astral's installer.
  - Install ortus via `uv tool install ortus`.
  - Verify install with `ortus --version`.
  - Print clear next-step guidance ("Try `ortus init ~/code/your-project`").
  - The CI release workflow (FR-029) shall upload `install.sh` as an asset on every release so `releases/latest/download/install.sh` always resolves.
- **[P1] FR-029** — Ortus shall be publishable to PyPI under the name `ortus` (or an alternative if `ortus` is taken — see Q1).
- **[P2] FR-030** — A Homebrew tap shall ship `brew install who/ortus/ortus` (which wraps `uv tool install ortus` or downloads a built binary; specific approach pinned in Q5).

**Sunset of bash + copier:**

- **[P0] FR-031** — The canonical ortus repo shall be restructured to remove `template/`, `copier.yaml`, `make parity`, and per-script bash files under `ortus/*.sh` once the Python equivalents land. No deprecation shim for the bash scripts (greenfield assumption — existing copier'd projects either freeze or rebuild).
- **[P1] FR-032** — The canonical repo's git history shall preserve the bash era (no force-push, no rewrite); the final bash version shall be tagged (e.g., `v0.x-final-bash`) before the Python rewrite is merged.

### Non-Functional Requirements

- **[P0] NFR-001** — `ortus init <fresh-repo>` shall complete in ≤ 5 seconds on a typical dev laptop (modulo `bd init`'s own time).
- **[P0] NFR-002** — `ortus grind` startup overhead (before the first claude turn) shall be ≤ 500 ms (includes config resolution, prompt resolution, flock, hook precheck, sandbox check). Python interpreter startup dominates; budget accounts for it.
- **[P0] NFR-003** — Install via the shell script shall complete in ≤ 15 seconds on a fast network when uv is already present (downloads: ortus package + any Python interpreter uv needs to fetch on first tool install).
- **[P0] NFR-004** — The installer shall require `uv` as a precondition and exit 1 with a clear error message if missing. It shall NOT auto-install uv. This is a deliberate choice (see FR-028) to keep the installer small and the dependency explicit.
- **[P0] NFR-005** — All verbs shall produce consistent error formatting via `rich` (stderr for errors; clear cause and suggested fix; never bare Python tracebacks for expected error paths).
- **[P0] NFR-006** — The CLI shall not mutate bd state in `ortus check` or `ortus tail` or `ortus human` (read-only verbs are strictly read-only; verified by tests).
- **[P1] NFR-007** — The package shall declare a minimum Python version (`>=3.10` recommended) and uv shall handle Python install if missing.
- **[P1] NFR-008** — The package shall be cross-platform: Linux, macOS, Windows. CI tests on all three.
- **[P1] NFR-009** — All verbs shall be safe to run with `--quiet` (suppresses non-essential output to stderr only) and `--verbose` (adds debug context); resolved via standard logging.

### ZFC Rubric Audit

| Decision point | Allowed (dumb pipe) | Forbidden (local intelligence) | Verdict |
|---|---|---|---|
| "Where's the bd workspace?" | Walk up from `<repo>` looking for `.beads/`; standard tree-search | Heuristic about "is this directory ortus-related" | Allowed |
| "Which prompt to use?" | Three-layer file resolution per FR-025 | Pick based on prompt content / fuzzy matching | Allowed |
| "Should we proceed despite missing prereq?" | Hard fail (exit 1) from `ortus check` and verb-internal prereq checks | Skip-on-missing escape hatch | Allowed; no skip env vars (per NFR-001-style discipline from PRD-goal-directive) |
| "How to format error output?" | `rich`-formatted, consistent shape | Per-verb ad-hoc format | Allowed |
| "When is `ortus grind` done?" | Delegated to /goal evaluator (Haiku) per existing PRD-goal-directive | grep sentinels client-side | Allowed; same retirement as PRD-goal-directive |
| Config / prompt resolution | Mechanical file-existence checks in layered order | Try to "merge" prompts intelligently | Allowed |

Net effect: the global-CLI version is **at least as ZFC-aligned** as today's ortus; the verb dispatch + config resolution layer is pure plumbing.

---

## System Architecture

### High-Level Components

```
┌───────────────────────────────────────────────────────────────────────┐
│ Developer / CI                                                        │
│   $ ortus grind ~/code/myproj                                         │
│   $ ortus init ~/code/new-thing                                       │
│   $ ortus plan ~/code/myproj ~/Documents/feature.md                   │
└────────────────────────────┬──────────────────────────────────────────┘
                             │
                  ┌──────────▼──────────┐
                  │  ortus (Python)     │  (installed via `uv tool install`)
                  │  - typer CLI router │
                  │  - 8 verb modules   │
                  │  - bundled prompts/ │
                  │  - bundled templates│
                  └──────────┬──────────┘
                             │
            ┌────────────────▼──────────────────────┐
            │  config + prompt resolution           │
            │  - <repo>/.ortusrc → ~/.ortusrc → defaults
            │  - <repo>/.ortus/prompts/X.md → ~/.ortus/prompts/X.md → bundled
            └────────────────┬──────────────────────┘
                             │
        ┌────────────────────┼─────────────────────┐
        │                    │                     │
   ┌────▼────┐         ┌─────▼─────┐          ┌────▼─────┐
   │ bd      │         │  claude   │          │  git     │
   │ (host)  │         │  (host)   │          │  (host)  │
   └─────────┘         └───────────┘          └──────────┘
        │                    │                     │
   ┌────▼─────────────────────────────────────────────┐
   │ <repo>/.beads/, <repo>/.claude/settings.json,    │
   │ <repo>/logs/grind-*.log, <repo>/HUMAN-TODO.md    │
   └──────────────────────────────────────────────────┘
```

### Component Interactions

1. **User invokes `ortus <verb> <repo> [args]`** — typer dispatches to the verb module.
2. **Verb module resolves `<repo>`** — defaults to `$PWD`; verifies it's a directory; locates `.beads/` and `.ortusrc`.
3. **Verb resolves prompts and config** — three-layer lookup per FR-025/026.
4. **Verb runs precheck** — for verbs that need claude (`grind`, `plan`, `interview`, `triage`): hook precheck (`disableAllHooks` not set), sandbox smoke test, claude binary on PATH.
5. **Verb orchestrates subprocesses** — `subprocess.run(["claude", "-p", ...], ...)` for claude; `subprocess.run(["bd", ...])` for bd; tee output to log files via Python file handles.
6. **Verb emits result** — `rich`-formatted to stdout/stderr; exits with appropriate code.

### Technical Decisions

| Decision | Rationale |
|---|---|
| **Python (not Go)** | User-stated language preference; uv collapses install UX gap with Go. |
| **uv as primary install mechanism** | Auto-Python-install, isolation, speed, Rust-based reliability; Astral momentum. |
| **typer for CLI router** | Best-in-class Python CLI library; decorator-based verb definitions; auto-generates `--help`; type-checked argument parsing. |
| **rich for terminal output** | Beautiful tables, syntax highlighting, progress bars, cross-platform color; standard in modern Python CLI tools. |
| **subprocess (stdlib) for shelling out** | No extra dep; well-understood; sufficient for bd/claude/git invocation. |
| **jinja2 for `ortus init` templates** | Mature; what copier used internally; expressive enough for the few templates we need. |
| **importlib.resources for bundled assets** | Standard Python pattern for shipping data files inside a package; works with wheels and editable installs. |
| **pyproject.toml + hatchling (or uv's native build backend)** | Modern Python packaging; declarative; uv-friendly. |
| **`.ortusrc` in TOML format** | Familiar (pyproject.toml is TOML), well-supported (`tomllib` in stdlib since 3.11), unambiguous parsing. |
| **Greenfield (no `ortus migrate`)** | User decision: don't carry forward the 20 existing copier'd projects. They either freeze on the final bash-era ortus tag or get manually rebuilt. |
| **Umbrella `ortus <verb>` (no flat shorthands)** | Discoverability via `ortus --help`; only one PATH entry; predictable for users and CI. |
| **Three-layer prompt/config resolution** | Standard pattern (git, bd, npm, etc.); supports per-project + per-user customization without forking. |
| **Templates as Python package data** | Single source of truth; no parity discipline; templates always match the installed ortus version. |
| **`copier` and `template/` removed entirely** | Greenfield assumption + package-data templating render them obsolete; biggest simplification of the rewrite. |
| **No `--legacy` bash fallback** | If a user is in a restricted-hooks environment, `ortus check` and `ortus grind` will detect and error out via the existing `disableAllHooks` precheck path; no second code path to maintain. |
| **No deprecation shim for bash scripts** | Greenfield; existing copier'd projects don't need a shim because they're not getting Python ortus — they keep their vendored bash forever or rebuild from scratch. |

### Data Model

No new persistent state. Existing state stores are preserved:

- **bd workspace** at `<repo>/.beads/` — unchanged; ortus only reads/writes via `bd` subprocess calls.
- **claude settings** at `<repo>/.claude/settings.json` — bootstrapped by `ortus init`; otherwise read-only from ortus's perspective.
- **logs** at `<repo>/logs/grind-*.log` — written by `ortus grind`; followed by `ortus tail`; new prefix `grind-` replaces `goal-`.
- **HUMAN-TODO.md** at `<repo>/HUMAN-TODO.md` — written by `ortus human`; should be `.gitignore`'d in the template.
- **`.ortusrc`** at `<repo>/.ortusrc` (TOML) — created by `ortus init`; read by every verb for project-specific overrides.
- **Per-project prompt overrides** at `<repo>/.ortus/prompts/*.md` — optional; created by user manually when they want to customize.

### Package Layout (canonical ortus repo, post-rewrite)

```
ortus/                                # canonical ortus repo (the one you maintain)
├── pyproject.toml                    # uv tool-installable; declares typer, rich, jinja2, etc.
├── README.md                         # install + quick-start
├── install.sh                        # shell installer (requires uv as precondition)
├── src/ortus/
│   ├── __init__.py
│   ├── __main__.py                   # entry: typer app dispatch
│   ├── cli.py                        # top-level typer app + subcommand registration
│   ├── commands/
│   │   ├── __init__.py
│   │   ├── init.py                   # ortus init <repo>
│   │   ├── plan.py                   # ortus plan <repo> [<PRD>]
│   │   ├── grind.py                  # ortus grind <repo>
│   │   ├── interview.py              # ortus interview <repo>
│   │   ├── tail.py                   # ortus tail <repo>
│   │   ├── triage.py                 # ortus triage <repo>
│   │   ├── human.py                  # ortus human <repo>
│   │   └── check.py                  # ortus check <repo>
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py                 # .ortusrc layered resolution
│   │   ├── repo.py                   # repo discovery + validation
│   │   ├── prompts.py                # three-layer prompt resolution
│   │   ├── bd.py                     # bd subprocess wrappers
│   │   ├── claude.py                 # claude subprocess wrappers (incl. /goal)
│   │   ├── sandbox.py                # smoke test + docker precondition
│   │   ├── hooks.py                  # disableAllHooks precheck
│   │   ├── logging.py                # log file management + rotation
│   │   └── output.py                 # rich-based formatting helpers
│   ├── prompts/                      # bundled as package data
│   │   ├── grind-prompt.md
│   │   ├── plan-prompt.md
│   │   ├── interview-prompt.md
│   │   ├── triage-prompt.md
│   │   └── ...
│   └── templates/                    # bundled as package data; used by `ortus init`
│       ├── .claude/settings.json.jinja
│       ├── .ortusrc.jinja
│       ├── AGENTS.md.jinja
│       └── .gitignore.jinja
├── tests/
│   ├── test_cli.py                   # typer integration tests
│   ├── test_commands/                # per-verb unit tests
│   └── fixtures/                     # synthetic repos for end-to-end smoke
└── .github/workflows/
    ├── test.yml                      # pytest on Linux/Mac/Windows
    └── release.yml                   # publish to PyPI + bump install.sh URL
```

Key absences vs. today: **no `template/` directory, no `copier.yaml`, no `Makefile` with parity targets, no `ortus/*.sh` scripts, no `scripts/check-ortus-parity.sh`.** All retired.

---

## Milestones & Phases

### Phase 1: Foundation — Python repo + CLI skeleton + `ortus init` + `ortus check`

**Goal**: Establish the new repo structure, the typer CLI router, and the two simplest verbs (`init` and `check`) end-to-end. Validates the architecture; no claude-shelling-out yet.

**Deliverables**:
- New `ortus/` repo structure per §Package Layout (existing repo restructured or new branch).
- `pyproject.toml` with `typer`, `rich`, `jinja2`, `tomllib`-or-`tomli` (depending on Python version target), pytest dev dep.
- `src/ortus/cli.py` with typer app + all 8 verb stubs (returning "not implemented" placeholders) so the routing surface is verifiable end-to-end.
- `src/ortus/commands/init.py` — full implementation. Tests: fresh repo → bd workspace + settings + .ortusrc + AGENTS.md created; `--force` idempotency; `--prefix` override.
- `src/ortus/commands/check.py` — full implementation. Tests: green path; missing-bd-binary path; missing-sandbox-prereq path; disableAllHooks-set path.
- `src/ortus/core/{config,repo,output,hooks,sandbox}.py` — supporting modules used by init and check.
- `src/ortus/templates/*.jinja` — bundled templates rendered by init.
- CI on Linux + Mac (Windows in Phase 4).

**Dependencies**: none.

**Acceptance**:
- `uv tool install --editable .` from a fresh checkout produces a working `ortus` binary on PATH.
- `ortus --help` lists all 8 verbs (6 returning "not implemented", 2 functional).
- `ortus init /tmp/fresh && ortus check /tmp/fresh` exits 0 with all green.

### Phase 2: Core orchestration verbs — `ortus grind` + `ortus plan`

**Goal**: Port the two highest-value bash orchestrators (goal.sh, idea.sh) to Python. After this phase, you can do the full PRD→bd issues→autonomous-loop flow with the new CLI.

**Deliverables**:
- `src/ortus/commands/grind.py` — full implementation matching goal.sh's behavior + invariants per FR-010–013. Port: flock, sandbox check, cache exports, hook precheck, cleanup_children, claude invocation with `/goal`, tee-to-log-not-terminal.
- `src/ortus/commands/plan.py` — full implementation matching idea.sh (both `--prd` and no-args paths) per FR-014–016. Explicit `<repo>` arg eliminates the cd-to-PRD-dir bug.
- `src/ortus/core/claude.py` — central wrapper for `subprocess` calls to `claude -p` with the standard flag set (`--dangerously-skip-permissions`, `--output-format stream-json`, `--verbose`, `--fast`).
- `src/ortus/core/bd.py` — central wrapper for `subprocess` calls to `bd` with JSON-output parsing.
- `src/ortus/prompts/grind-prompt.md` — ported from current `goal-prompt.md` (likely identical or near-identical).
- `src/ortus/prompts/plan-prompt.md` — ported from current `prd-decompose-prompt.md`.
- Tests: end-to-end smoke runs `ortus grind /tmp/fixture --tasks 1` against a seeded fixture repo, closes 1 issue. `ortus plan /tmp/fixture /tmp/test-prd.md` decomposes a 3-issue PRD into 3 bd issues.

**Dependencies**: Phase 1.

**Acceptance**:
- `ortus grind /tmp/fixture --tasks 1` against a generated project closes one bd issue end-to-end (claim → implement → verify → close → commit/push if remote configured).
- `ortus plan /tmp/fixture /tmp/test-prd.md` decomposes the PRD into the expected bd graph; `bd ready` shows the unblocked entry points.
- Both verbs respect the terminal-quiet UX (claude stream-json goes to log only, not to launching terminal).

### Phase 3: Interactive verbs — `ortus interview` + `ortus triage` + `ortus human` + `ortus tail`

**Goal**: Port the remaining four verbs. After this phase, all 8 verbs are functional.

**Deliverables**:
- `src/ortus/commands/interview.py` — ports interview.sh; uses claude with `AskUserQuestion + Bash(bd:*) + Read` allowedTools.
- `src/ortus/commands/triage.py` — ports triage.sh; same shape as interview but driving the human queue.
- `src/ortus/commands/human.py` — ports human.sh + the pros/cons enhancement; writes HUMAN-TODO.md.
- `src/ortus/commands/tail.py` — ports tail.sh; follows `grind-*.log` and (legacy) `ralph-*.log` and `goal-*.log` log files.
- Tests: `ortus human /tmp/fixture` produces a HUMAN-TODO.md with expected structure; `ortus tail /tmp/fixture` runs without crashing on a fixture log directory; `ortus interview` and `ortus triage` smoke-tested with mocked claude responses.

**Dependencies**: Phase 1 + Phase 2 (uses claude wrapper).

**Acceptance**:
- All 4 verbs return real output (not "not implemented"); each smoke-tested end-to-end on a fixture repo.

### Phase 4: Distribution — installer + PyPI + cross-platform CI

**Goal**: Make ortus installable by anyone with a single command. Validate Windows support.

**Deliverables**:
- `install.sh` (the installer script) uploaded as a release asset on every tagged release — requires uv (errors out with hint if missing), runs `uv tool install ortus`, verifies with `ortus --version`, prints next steps.
- PyPI publishing pipeline: GitHub Action that on tag push publishes to PyPI (via API token).
- Windows CI added to test matrix (pytest on Windows via GitHub Actions).
- README updated with install instructions (uv install prereq + curl-installer, or direct `uv tool install ortus` from PyPI, or `uv tool install git+...` for source).
- (Optional) Homebrew tap repo created + formula written — punt to Phase 5 or skip per Q5.

**Dependencies**: Phase 1 + 2 + 3.

**Acceptance**:
- `curl -fsSL https://github.com/who/ortus/releases/latest/download/install.sh | sh` on a fresh Mac/Linux VM installs ortus in ≤ 30 seconds and `ortus --version` works.
- `uv tool install ortus` from PyPI directly works.
- Windows CI passes for all 8 verbs (smoke tests).

### Phase 5: Sunset — retire bash + copier in the canonical repo

**Goal**: Remove the old bash scripts and the copier-template apparatus from the canonical ortus repo. The Python ortus is now the only ortus.

**Deliverables**:
- Tag the final pre-Python commit (e.g., `v0.x-final-bash`) for forensic / rollback access.
- Delete `template/`, `copier.yaml`, `Makefile`'s parity targets, `scripts/check-ortus-parity.sh`, `ortus/*.sh`, `ortus/lib/*.sh`, `ortus/prompts/*.md` (now lives under `src/ortus/prompts/`).
- Update root `README.md` to describe Python-CLI install + use; remove all copier references.
- Update `CLAUDE.md` and `AGENTS.md` to reference `ortus <verb>` instead of `./ortus/*.sh`.
- Final smoke: clone canonical repo, `uv tool install --editable .`, run a full ortus flow against a fresh fixture; verify nothing references the deleted paths.

**Dependencies**: Phase 4 (must be shippable before retiring).

**Acceptance**:
- `grep -rE 'template/|copier|make parity|ortus/.*\.sh' src/ docs/ README.md CLAUDE.md AGENTS.md` returns zero hits.
- `make parity` no longer exists; `make help` (or equivalent) lists only Python-era targets.
- The repo's `pyproject.toml` is the only build-system declaration.

---

## Epic Breakdown

Each phase becomes one epic; each deliverable bullet becomes 1-3 child tasks. The exact decomposition happens via `idea.sh --prd <this file>` (or `ortus plan ortus <this file>` once Phase 1 ships), but a rough preview:

### Epic E1: Phase 1 — Foundation
- Set up new Python repo structure (pyproject.toml, src/ortus/ layout)
- Write typer CLI skeleton with 8 verb stubs
- Implement `ortus init` (full)
- Implement `ortus check` (full)
- Write core modules (config, repo, output, hooks, sandbox)
- Bundle templates as package data + jinja rendering
- CI: Linux + Mac pytest matrix
- Bash-era end-of-life prep: tag `v0.x-final-bash`

### Epic E2: Phase 2 — Core orchestration
- Implement `ortus grind` (port goal.sh, preserve all invariants)
- Implement `ortus plan` (port idea.sh, both modes)
- Implement core/claude.py wrapper
- Implement core/bd.py wrapper
- Port grind-prompt.md and plan-prompt.md
- E2E smoke tests for grind and plan
- Verify terminal-quiet UX (no stream-json leak)

### Epic E3: Phase 3 — Interactive verbs
- Implement `ortus interview` (port interview.sh)
- Implement `ortus triage` (port triage.sh)
- Implement `ortus human` (port human.sh + pros/cons)
- Implement `ortus tail` (port tail.sh)
- E2E smoke tests for all four

### Epic E4: Phase 4 — Distribution
- Write `install.sh` installer (uv-precondition check + `uv tool install ortus`)
- Set up PyPI publish workflow
- Add Windows CI
- Write README install section
- (Opt) Homebrew tap

### Epic E5: Phase 5 — Sunset
- Tag final bash version
- Delete template/, copier.yaml, parity targets
- Delete ortus/*.sh, ortus/lib/*.sh, ortus/prompts/*.md
- Update root docs (README, CLAUDE.md, AGENTS.md)
- Final smoke against fresh fixture

---

## Open Questions

- **Q1 — PyPI name availability.** Is `ortus` available on PyPI? If not, candidates: `ortus-cli`, `ortusctl`, `who-ortus` (org-prefixed). Decide before Phase 4. Quick check: `pip search ortus` (or browse pypi.org/project/ortus).

- **Q2 — Python version target.** Minimum 3.10? 3.11 (for native `tomllib`)? 3.12? Recommend 3.10 for broadest compat; uv handles install anyway, so users don't need to have it pre-installed. Decide before Phase 1.

- **Q3 — *Resolved 2026-05-16.*** *(Was: bootstrap install failure modes if uv install fails.) The installer no longer attempts to install uv (FR-028 revised). Users without uv get a clear error pointing at the official uv install docs. No fallback needed; no failure modes to handle around uv installation.*

- **Q4 — Per-project ortus version pinning.** Should `.ortusrc` declare `ortus_version: "^2.0"` so a project can require a specific ortus version? Recommend: not in v1. Single global version is fine for solo dev. Add later if it becomes painful.

- **Q5 — Homebrew tap or rely on `uv tool install`?** Tap is more discoverable for Mac users (`brew search ortus`) but adds tap-maintenance burden. Recommend: skip tap in v1; rely on `curl ... | sh` + `uv tool install` documented in README. Add tap in Phase 5+ if community grows.

- **Q6 — `.ortusrc` vs `pyproject.toml [tool.ortus]`?** Some users may prefer ortus config to live in pyproject.toml (for Python projects) instead of a separate `.ortusrc`. Recommend: support both, with `pyproject.toml [tool.ortus]` overriding `.ortusrc` if both present. Or pick one and stick with it for v1 simplicity (likely `.ortusrc` for non-Python project support).

- **Q7 — `ortus init` idempotency semantics.** What exactly does re-running `ortus init` on a configured repo do? Recommend: refuse without `--force`; with `--force`, overwrite ortus-owned files (settings.json, .ortusrc) but never touch user-written content (AGENTS.md if user modified it). Tricky to define "user modified" without a checksum / state file.

- **Q8 — Migration helper for power users?** Strict greenfield means no `ortus migrate`. But a documented manual process ("here's how to convert a copier'd repo to Python-ortus") would be cheap to write and could prevent the existing 20 repos from being abandoned. Recommend: write a docs/migration.md (not a CLI verb); ship in Phase 5.

- **Q9 — `ortus init` on non-empty repo (e.g., existing project the user wants to start using ortus on).** Should ortus init detect existing `.beads/` and refuse? Or merge intelligently? Recommend: refuse without `--force`; with `--force`, skip any file that already exists; print a summary of what was created vs. skipped.

- **Q10 — `goal.sh` log prefix migration.** Current logs are `logs/goal-*.log`. New CLI writes `logs/grind-*.log`. Should `ortus tail` follow both for back-compat? Recommend: yes, follow `grind-*`, `goal-*`, and `ralph-*` prefixes (cheap, helps any user with an old log dir).

---

## Out of Scope

- **Migration tool for existing vendored projects** (greenfield assumption is binding).
- **Multi-version ortus install** (one global version per machine; pin via PyPI version constraint at install time if needed).
- **Plugin / extension system** for adding new verbs (the 8 verbs are the surface; extensions can wait for a real user need).
- **Web UI / status dashboard** for monitoring grind sessions.
- **Cross-session memory beyond bd comments** (existing pattern is sufficient).
- **Per-user multi-tenancy on a shared install** (uv tool installs are per-user; no need).
- **Rewriting bd or claude** (ortus is purely an orchestrator over those tools; they stay as-is).
- **Async / non-blocking claude invocation** (subprocess.run is fine; no need for asyncio).
- **GUI prompts via AskUserQuestion outside claude** (AskUserQuestion is a claude tool; ortus only invokes it via claude sessions).
- **Bundled prompts in multiple human languages** (English only; defer i18n indefinitely).

---

## Appendix

### Appendix A: Glossary

- **ortus** — The toolkit; this PRD redefines it as a global Python CLI.
- **uv** — Astral's Rust-based Python package manager; replaces pip + virtualenv + pyenv functionally.
- **typer** — Python CLI framework based on Click; uses type hints for arg parsing.
- **rich** — Python library for beautiful terminal output (tables, syntax highlighting, etc.).
- **bd / beads** — Local-first issue tracker; embedded Dolt backend since v1.0.3; unchanged.
- **`/goal` evaluator** — Claude Code small-fast-model (Haiku by default) that judges the active goal's condition after every turn. Used by `ortus grind`.
- **Greenfield assumption** — This PRD does not handle migration of existing copier'd projects. They either freeze on the final bash-era ortus or get manually rebuilt.
- **Parity discipline** — Today's pattern of mirroring every `ortus/X` change into `template/ortus/X`; retired by this PRD.

### Appendix B: Reference Links

- uv documentation: https://docs.astral.sh/uv/
- typer documentation: https://typer.tiangolo.com/
- rich documentation: https://rich.readthedocs.io/
- Predecessor PRD: `prd/PRD-goal-directive.md` (in the ortus repo)
- Claude Code `/goal` docs: https://code.claude.com/docs/en/goal
- Original ortus canonical repo (final bash version): https://github.com/who/ortus @ tag `v0.x-final-bash` (to be created in Phase 5)

### Appendix C: Interview Notes Summary

Five direct decisions captured in this session before drafting:

1. **Greenfield assumption** — User decided not to build `ortus migrate`. Existing 20 copier'd projects are out of scope.
2. **Umbrella command surface** — User chose `ortus <verb>` (8 verbs) over flat top-level commands; over umbrella with shorthand aliases.
3. **Main verb naming** — User picked `grind` over `implement`, `iterate`, `drive`. Distinctive, evocative, captures the "steady forward through the backlog" semantic.
4. **Language: Python (not Go)** — User cited familiarity with Python syntax as the tiebreaker. Ecosystem coherence with bd (which is Go) was explicitly waived.
5. **Distribution: uv** — User has positive experience with uv; building on it as the assumed distribution mechanism.

Each of these is locked; this PRD does not re-open them. Subsequent decomposition into bd issues should treat them as constraints, not choices.
