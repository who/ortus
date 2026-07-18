# PRD: Optional ChatGPT Codex backend for the Ortus `/goal` loop

## Metadata

- **Feature ID**: ortus-codex-backend (proposed; assign at decomposition)
- **Project Type**: Ortus core tooling (Copier template + canonical `ortus/`)
- **Created**: 2026-07-18
- **Author**: Claude (cross-reading `~/code/ortus`, the OpenAI Codex [`using_goals_in_codex` cookbook](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex), and the Codex non-interactive / security / config references at learn.chatgpt.com)
- **Interview Confidence**: Medium. Codebase audited end-to-end; Codex CLI surface grounded in official docs. Two facts still need pinning at implementation time (Codex JSON event schema exact fields; whether `bd setup codex` exists) — tracked as Open Questions.
- **Status**: Draft for review — NOT decomposed into bd issues yet.

---

## Overview

### Problem Statement

Ortus is hard-wired to a single autonomous-execution backend: Anthropic's `claude` CLI. Every orchestration path — `goal.sh`, `idea.sh`, `interview.sh`, `triage.sh` — shells out to `claude` with Claude-Code-specific flags (`-p`, `--output-format stream-json`, `--allowedTools`, `--dangerously-skip-permissions`), relies on Claude-Code-specific configuration (`.claude/settings.json`, `bd setup claude`), and assumes Claude-Code-specific runtime features (the managed `/goal` Stop hook, `AskUserQuestion`, `/compact`, Sonnet/Opus subagents).

Some operators want to run the same Ortus workflow on OpenAI's Codex CLI with its latest models instead — for cost, for model diversity, or because they already have a ChatGPT/Codex subscription rather than an Anthropic one. Today that is impossible without forking the tooling.

The good news, established during analysis: **the load-bearing abstraction survives the swap.** Codex ships a native `/goal` command suite (`/goal`, `/goal pause`, `/goal resume`, `/goal clear`) that is functionally equivalent to Claude Code's managed goal loop — after each turn Codex evaluates the objective against concrete evidence (test results, diffs, artifacts) and self-continues until success is confirmed, the budget is exhausted, or a blocker emerges. Ortus's entire design already speaks `/goal <condition>`; the condition strings in `prompts/conditions/*.txt` are provider-neutral English. So this is **not** a rewrite of the loop contract — it is a **backend seam**: a way to route the same `/goal <condition>` directive to either `claude` or `codex`, translate the flags, translate the config, and branch the log parser.

### Proposed Solution

Introduce a **pluggable agent backend** selected by a single variable, defaulting to `claude` so existing users see zero change:

- A new Copier question `agent_cli` (choices: `claude` | `codex`, default `claude`) bakes the chosen backend into a generated project.
- A runtime override `--backend claude|codex` (and env `ORTUS_BACKEND`) on `goal.sh` (and the other launchers) lets an operator flip backends without regenerating.
- The `claude` invocation in each script is refactored behind a small **backend adapter** (`ortus/lib/backend.sh`) that exposes three seams: **build the argv**, **translate the config**, and **parse the log stream**. Each script asks the adapter for the command array instead of hard-coding `claude`.

The adapter maps Ortus's existing intent onto each CLI:

| Ortus intent | Claude Code | Codex |
|---|---|---|
| Non-interactive run of a prompt | `claude -p "$prompt"` | `codex exec "$prompt"` |
| Streaming machine-readable output | `--output-format stream-json --verbose` | `--json` (JSON Lines events) |
| Autonomous, no approval prompts | `--dangerously-skip-permissions` | `--sandbox workspace-write --ask-for-approval never` (or `--dangerously-bypass-approvals-and-sandbox` when already inside Ortus's outer sandbox) |
| Select model | `--model <m>` | `-m <model>` |
| Working directory | `cd` (implicit) | `--cd/-C <dir>` (or `cd`) |
| Config source | `.claude/settings.json` (+ `.mcp.json`) auto-discovered in cwd | `$CODEX_HOME/config.toml` — Ortus sets `CODEX_HOME=$PWD/.codex` so config is project-local |
| Agent instructions | `CLAUDE.md` (auto-loaded) + `AGENTS.md` | `AGENTS.md` (native, hierarchical) |

Config and instruction files are generated per backend. For Codex, Ortus ships `.codex/config.toml` (sandbox mode, approval policy, network allowlist, `bd` exemption, MCP servers) as the peer of `.claude/settings.json`, and ensures the operational content Codex needs lives in `AGENTS.md` (which Codex reads natively) rather than only in `CLAUDE.md` (which Codex ignores).

The `/goal` termination contract, the condition files, the flock single-instance guard, the outer OS sandbox smoke test, the `--docker` tier, and the cache relocation are **backend-agnostic and preserved unchanged**. Only the leaf that spawns the agent and the leaf that reads its output become backend-aware.

Scope boundary: **`goal.sh` (the autonomous loop) is the priority and the acceptance gate.** The interactive flows (`interview.sh`, `triage.sh`) depend on `AskUserQuestion`, a Claude-Code tool with no Codex equivalent, and are addressed as a lower-priority phase (see Out of Scope / Phase 4).

### Success Metrics

- **M1 — Claude parity preserved**: with `agent_cli=claude` (the default), every script produces byte-identical argv and behavior to today. A regression test asserts no drift.
- **M2 — Codex drives the queue to zero**: on a fixed replay queue (the existing `tests/fixtures/sample-prds/tiny-3-task.md`), `goal.sh --backend codex` claims, implements, verifies, commits, and closes all issues, terminating on its own via Codex's `/goal` evaluator with no operator intervention and no `/exit`.
- **M3 — Log parity**: `tail.sh` renders a readable turn-by-turn view from Codex `--json` output (assistant text, tool/command calls, token usage) at parity with what it renders from Claude stream-json.
- **M4 — Config isolation**: a Codex-generated project contains `.codex/config.toml` and no `.claude/settings.json` (and vice-versa); the `bd` sandbox exemption is expressed correctly for the chosen backend and `bd` calls succeed inside the loop.
- **M5 — One template, two backends**: `copier copy --data agent_cli=codex` and `--data agent_cli=claude` both generate projects that pass their smoke test; shared files (`goal.sh`, condition files, `AGENTS.md` core) are not duplicated per backend.

---

## Background & Context

### Why now?

Two independent developments make this tractable and worthwhile:

1. **Codex reached feature parity on the one primitive Ortus is built around.** Ortus's 2026-05 migration (`prd/PRD-goal-directive.md`) bet the whole orchestration on the `/goal` declarative-condition contract. At the time that was Claude-Code-only. Codex now ships the same primitive — a stateful continuation loop that evaluates a declarative objective against evidence after each turn and self-terminates. Because Ortus already expresses everything as `/goal <condition>`, the port is a seam, not a redesign.
2. **AGENTS.md is the cross-agent convention Ortus already honors.** Ortus ships `AGENTS.md` (copied verbatim, not templated) and both Claude Code and Codex read it. The instruction surface is already 90% portable; only the Claude-specific slices (`CLAUDE.md`, the Sonnet/Opus subagent table, `/compact`) need backend gating.

### How Codex `/goal` and non-interactive mode actually work (from docs)

- **Goal command suite**: `/goal <objective>` sets the goal; `/goal` shows status; `/goal pause` / `/goal resume` / `/goal clear` manage it. A well-formed goal names the desired endpoint, how success is verified, and what must be protected — the exact shape of Ortus's `queue-zero.txt`.
- **Loop**: a stateful continuation system. After each turn Codex checks the objective against concrete evidence (tests, benchmarks, modified files, artifacts). If unmet and within budget, it proceeds to the next action without re-prompting. It does not continue while another turn is active or while user input is queued.
- **Termination**: halts when success is confirmed against evidence, the user pauses/clears, budget is exhausted, or an insurmountable blocker emerges. This is the same four-way contract Ortus's condition files assume.
- **Non-interactive entry point**: `codex exec "<prompt>"`. **`/goal` does NOT work here** — see the Q1 finding under Open Questions. `codex exec` forwards the prompt string to the model verbatim; slash commands are a TUI-only affordance. The Codex backend therefore uses the plain-prompt termination model: the objective goes in the prompt body (and `AGENTS.md`), and Codex's own turn loop runs to evidence-based completion.
- **Flags (verbatim from the Codex non-interactive reference)**:
  - `--sandbox read-only | workspace-write | danger-full-access`
  - `--ask-for-approval <policy>` with policies `untrusted | on-failure | on-request | never`; `--full-auto` is the workspace-write + low-friction convenience mode; `--dangerously-bypass-approvals-and-sandbox` skips both.
  - `--json` — JSON Lines event stream. Event types include `thread.started`, `turn.started` / `turn.completed` / `turn.failed`, `item.started` / `item.completed`, and `usage` (token counts).
  - `-o` / `--output-last-message <path>` — write the final assistant message to a file.
  - `--output-schema <path>` — enforce a JSON Schema on the response.
  - `-c <key>=<value>` — override any `config.toml` key inline; `-m <model>` selects the model; `-C` / `--cd <path>` sets the working directory; `--skip-git-repo-check` relaxes the repo requirement; `codex exec resume --last` / `resume <SESSION_ID>` continues a session.
- **Config**: `$CODEX_HOME/config.toml` (default `~/.codex/config.toml`). Relevant keys: `model`, `sandbox_mode`, `approval_policy`, a `[sandbox_workspace_write]` block with `network_access`, and `[mcp_servers.*]` for MCP. `--ignore-user-config` skips it.
- **Auth**: ChatGPT sign-in (subscription) or API key via `CODEX_API_KEY` / `OPENAI_API_KEY`.

### How Ortus invokes the agent today (audited)

| Site | File:line | Invocation | Backend-specific pieces |
|---|---|---|---|
| Primary loop | `ortus/goal.sh:346` | `"${CLAUDE_CMD[@]}" -p "$prompt" --output-format stream-json --verbose --dangerously-skip-permissions $FAST_MODE` | flags; stream-json schema; `check_hooks_enabled` (`goal.sh:118-159`) gates on Claude's managed-hook requirement |
| PRD decompose | `ortus/idea.sh:77` | `claude --allowedTools "Read($prd),Bash(bd:*)" --dangerously-skip-permissions -p "$full_prompt"` | `--allowedTools` (no Codex analog); flags |
| Idea expand | `ortus/idea.sh:107` | `claude --print "$prompt…"` (output captured) | `--print`; captured text |
| Interview | `ortus/interview.sh:230` | `claude --allowedTools "AskUserQuestion,Bash(bd:*),Read" -p "$full_prompt"` | **`AskUserQuestion` — no Codex equivalent** |
| Triage | `ortus/triage.sh:82` | `claude --allowedTools "AskUserQuestion,Bash(bd:*),Read" -p "$full_prompt"` | **`AskUserQuestion` — no Codex equivalent** |

Backend-agnostic and reused as-is: the flock guard (`goal.sh:196-256`), `build_condition()` and all `prompts/conditions/*.txt`, `lib/sandbox.sh` (existence checks), `lib/cache.sh` (cache relocation), `human.sh` (pure `bd`+`jq`).

### The double-sandbox question

Ortus runs the agent inside an OS-level sandbox (bwrap on Linux/WSL2, Seatbelt on macOS) and relies on the agent's own in-CLI sandbox policy for the fine-grained allow/deny + the critical `bd` exemption. Claude Code enforces that policy from `.claude/settings.json` (`sandbox.excludedCommands: ["bd","bd *"]`, `network.allowedDomains`). Codex has its **own** sandbox (`sandbox_mode` + `approval_policy` + `[sandbox_workspace_write].network_access`). The two models differ, and the `bd` exemption is the trickiest to port:

- `bd` talks to a local (embedded) Dolt store and needs writes outside a naive read-only scope and loopback where applicable. Under Claude Code this is the `excludedCommands` exemption. Under Codex, `workspace-write` already permits writes within the workspace; the risk is network/loopback and any `bd` write path outside the workspace root. The Codex config must set `sandbox_mode = "workspace-write"` and enable the network access `bd` needs (`[sandbox_workspace_write].network_access`), or run `bd`-touching turns under a scope that does not block it.
- Because Ortus already provides an **outer** OS sandbox, the Codex inner sandbox can be relaxed (`--dangerously-bypass-approvals-and-sandbox`) as defense-in-depth-by-outer-layer — mirroring how `goal.sh` already passes `--dangerously-skip-permissions` to Claude while relying on bwrap. This is the recommended default for the autonomous loop and must be documented as "safe only because the outer sandbox is enforced (smoke test gates the run)."

---

## Users & Personas

### Primary Users

1. **Ortus contributors** — maintain `ortus/` and `template/ortus/` in parity; need the backend seam to be readable and to not duplicate shared logic.
2. **Codex-first operators** — have a ChatGPT/Codex subscription (or `OPENAI_API_KEY`), want to run the Ortus autonomous loop on Codex's latest models. Never edit the scripts; run `./ortus/goal.sh` and expect the bd queue to drain.
3. **Claude-first operators (status quo)** — must see zero change. The default stays `claude`.
4. **Evaluators / cost-conscious teams** — want to A/B the same queue across backends to compare throughput, quality, and cost.

### User Goals

- **G1**: "Generate an Ortus project that drives its bd queue with Codex instead of Claude, with the same one-command UX."
- **G2**: "Flip an existing project's backend without regenerating."
- **G3 (status quo)**: "I use Claude and nothing about my setup changes."
- **G4**: "The log viewer works the same regardless of backend."

### Current Workflows (and what is painful)

- Today there is exactly one path (`claude`). A Codex user must hand-edit five scripts, the settings file, and the copier task — and would lose it on the next `copier update`. The pain is total lock-in to one vendor CLI.

---

## Requirements

### Functional Requirements

- **FR-001 — Backend selection variable.** Add a Copier question `agent_cli` with choices `claude` (default) and `codex`. It is threaded into templated files (settings/config, README, CLAUDE.md/AGENTS.md, copier `_tasks`).
- **FR-002 — Runtime backend override.** `goal.sh` accepts `--backend claude|codex`; env `ORTUS_BACKEND` is the fallback; the generated default (from `agent_cli`) is the final fallback. Precedence: flag > env > generated default. `idea.sh` inherits the same resolution.
- **FR-003 — Backend adapter library.** New `ortus/lib/backend.sh` exposes:
  - `backend_argv <role> [prompt]` — returns the command array for a role (`goal`, `prd-decompose`, `idea-expand`), translated per backend.
  - `backend_stream_flags` — the machine-readable-output flags for the current backend.
  - `backend_available` / `backend_preflight` — verify the selected CLI is on PATH and authenticated; fail fast with a targeted message otherwise.
  - The adapter is the *only* place a backend binary name or backend-specific flag appears. Scripts never name `claude`/`codex` directly.
- **FR-004 — Codex argv for the autonomous loop.** For `agent_cli=codex`, the `goal` role expands to `codex exec "<prompt>" --json --sandbox workspace-write --dangerously-bypass-approvals-and-sandbox [-m <model>]` (exact bypass-vs-approval posture per FR-010). The prompt is the same `"/goal $(build_condition)"` string used today. `$FAST_MODE`/`--fast` has no Codex analog and is a no-op under Codex (documented, not errored).
- **FR-005 — Codex config generation.** For `agent_cli=codex`, generate `.codex/config.toml` (peer of `.claude/settings.json`) with `sandbox_mode`, `approval_policy`, `[sandbox_workspace_write].network_access`, the language-profile-derived network allowlist (reusing the same `language_profile` logic that drives `allowedDomains` today), and any MCP servers. Scripts set `CODEX_HOME=$PWD/.codex` before invoking `codex` so config is project-local, not the user's global `~/.codex`.
- **FR-006 — `bd` exemption under Codex.** The generated Codex config MUST let `bd` run with the write + network access its embedded Dolt store needs, verified by a preflight that runs `bd ready --json` through the loop's sandbox posture and asserts success. Document that wrapping `bd` (pipes, `xargs`, `bash -c`) can defeat host-level allowances, consistent with the existing Claude guidance.
- **FR-007 — Log parser branch.** `tail.sh` gains a Codex `--json` decoder (`thread.started`, `turn.*`, `item.*`, `usage`) alongside the existing Claude stream-json decoder, selected by backend. Output rendering (assistant text, command/tool calls, token counts) is at parity.
- **FR-008 — Hook-gate is Claude-only.** `check_hooks_enabled` (which enforces Claude's managed-Stop-hook requirement) MUST be skipped when the backend is Codex; Codex's `/goal` is native and does not depend on Claude's `disableAllHooks` setting.
- **FR-009 — Instruction files per backend.** When `agent_cli=codex`, ensure the operational content Codex needs is reachable via `AGENTS.md` (Codex does not read `CLAUDE.md`). The Orchestrator section of `AGENTS.md` MUST describe the active backend's invocation (`codex exec "/goal …"` vs `claude -p "/goal …"`). Shared content stays in one place; only the backend-variant lines are templated.
- **FR-010 — Sandbox posture is explicit and gated.** The default Codex posture for the autonomous loop is "relaxed inner sandbox behind the enforced outer OS sandbox." The outer sandbox smoke test (`lib/sandbox.sh`) MUST still pass before launch, exactly as today. If the operator opts out of the outer sandbox, the adapter MUST fall back to a real Codex inner sandbox (`--sandbox workspace-write --ask-for-approval never`) rather than a full bypass.
- **FR-011 — Copier post-copy task per backend.** The `_tasks` step currently runs `bd setup claude`. Make it backend-conditional (`bd setup codex` if such a profile exists — Q3; otherwise a documented manual/no-op path). Do not run the Claude profile setup for a Codex project.
- **FR-012 — README prerequisites per backend.** The generated README's Requirements / Sandboxing / Auth sections reflect the chosen backend (install `codex`, `codex login` / `CODEX_API_KEY`; or install `claude`, OAuth / `ANTHROPIC_API_KEY`).
- **FR-013 — Preflight and error messages.** If the selected backend CLI is missing or unauthenticated, every launcher fails fast with a message naming the backend and the fix (install/login command), before touching the flock or sandbox.

### Non-Functional Requirements

- **NFR-001 — Zero-change default.** With `agent_cli=claude`, generated output and script behavior are identical to pre-feature (byte-diff clean on the template render; argv-identical at the call site).
- **NFR-002 — Single source of truth per concern.** No copy-paste of shared logic across backends. The adapter and templating conditionals are the only backend-aware surfaces; condition files, flock, cache, and sandbox-existence code are untouched.
- **NFR-003 — Parity discipline.** `ortus/` and `template/ortus/` stay in lockstep (existing `make parity` extended to cover `lib/backend.sh` and the Codex config template).
- **NFR-004 — Non-interactive by design.** Per the Ortus CLI convention, the backend selection and all pass-through flags default to non-interactive; the loop never blocks on an approval prompt (that would hang the autonomous subprocess).
- **NFR-005 — Readable failure, not silent degradation.** A missing/misconfigured backend, a defeated `bd` exemption, or an unparseable event stream fails loudly with a diagnostic — never silently no-ops (the same posture as today's hook gate and sandbox smoke test).

### ZFC Rubric Audit

- **No client-side branching on unstructured model output.** Termination stays owned by the backend's `/goal` evaluator; the shell does not grep transcripts to decide continue/stop. The Codex log parser reads *typed* JSON events, not free text.
- **Structured over stringly-typed.** The adapter returns a command *array*, not a string to be re-split. Config is generated from typed template variables, not string-patched.
- **Fail fast, fail loud.** Preflights (backend present, authenticated, `bd` reachable, outer sandbox present) gate the run; each has a targeted message.
- **Additive on invariants.** flock, sandbox smoke test, `--docker`, cache relocation, condition files — all preserved. Only leaf invocation + leaf parsing become backend-aware.

---

## System Architecture

### High-Level Components

1. **`ortus/lib/backend.sh` (new)** — the adapter. Resolves the active backend (flag/env/default), exposes `backend_argv`, `backend_stream_flags`, `backend_preflight`, and backend-specific env setup (`CODEX_HOME` for Codex). Pure shell, sourced by the launchers alongside `lib/sandbox.sh` and `lib/cache.sh`.
2. **`goal.sh` (modified)** — replaces the hard-coded `CLAUDE_CMD` array + inline flags at `:325-346` with `backend_argv goal "$prompt"` + `backend_stream_flags`. Gates `check_hooks_enabled` on backend==claude. Everything else (flock, condition build, sandbox test, cache, logging, cleanup trap) unchanged.
3. **`idea.sh` (modified)** — routes the PRD-decompose and idea-expand calls through the adapter. Note the `--allowedTools` scoping has no Codex analog; under Codex these calls rely on the sandbox/approval posture instead (documented tradeoff).
4. **`tail.sh` (modified)** — adds a Codex `--json` event decoder branch selected by backend (detected from the log or a sidecar marker).
5. **Config/instruction templates (modified/new)** — `template/.codex/config.toml.jinja` (new), `template/.claude/settings.json.jinja` (existing, now conditionally emitted), `CLAUDE.md.jinja` / `AGENTS.md` backend-variant lines, README backend-variant sections, `copier.yaml` new question + conditional `_tasks`.
6. **`interview.sh` / `triage.sh`** — Phase 4 only; blocked on the `AskUserQuestion` gap (see Out of Scope).

### Component Interactions

```
copier.yaml (agent_cli) ──► generated project:
                              ├─ .claude/settings.json      (if claude)
                              └─ .codex/config.toml         (if codex)

./ortus/goal.sh --backend? / ORTUS_BACKEND? / generated default
      │
      ├─ source lib/backend.sh  ─► resolve backend, backend_preflight
      ├─ source lib/cache.sh, lib/sandbox.sh   (unchanged)
      ├─ flock guard             (unchanged)
      ├─ sandbox smoke test      (unchanged)
      ├─ build_condition()       (unchanged) ─► "/goal <condition>"
      ├─ CMD=( backend_argv goal "$prompt" )
      │       claude ─► claude -p "$prompt" --output-format stream-json --verbose --dangerously-skip-permissions
      │       codex  ─► CODEX_HOME=$PWD/.codex codex exec "$prompt" --json --sandbox workspace-write --dangerously-bypass-approvals-and-sandbox
      └─ "${CMD[@]}" | tee logs/goal-*.log
                              │
                     ./ortus/tail.sh ─► decode(backend, event stream)
```

### Technical Decisions

- **Adapter in shell, not a new language.** Keeps the toolchain unchanged; the launchers are already bash.
- **Project-local `CODEX_HOME`.** Mirrors how Claude Code auto-discovers `.claude/` in cwd; keeps config in-repo and version-controlled, avoids polluting the user's global `~/.codex`.
- **Relaxed inner sandbox behind enforced outer sandbox (default).** Symmetric with the existing Claude posture (`--dangerously-skip-permissions` + bwrap). The outer smoke test is the real gate; FR-010 defines the fallback when the outer layer is absent.
- **Do not port `--allowedTools` semantics 1:1.** Codex has no equivalent per-call tool allowlist; scoping is expressed through sandbox mode + approval policy + (optionally) execpolicy rules. Accept the coarser grain for the headless roles; keep the tightest posture the role allows.
- **`/goal` string is unchanged across backends.** Both CLIs own a native `/goal`; Ortus keeps emitting the same directive and lets each backend's evaluator run it.

### Data Model

No persistent schema changes. New surfaces:
- `agent_cli` answer persisted in `.copier-answers.yml`.
- `.codex/config.toml` (TOML) for Codex projects.
- A backend marker for `tail.sh` to select its decoder (e.g. a `# backend: codex` header line written into the log, or read from `.copier-answers.yml` / the resolved backend at tail time).

---

## Milestones & Phases

### Phase 1: Backend seam (no behavior change)
Extract the hard-coded `claude` invocation in `goal.sh` and `idea.sh` behind `lib/backend.sh` with only the `claude` implementation. Prove argv-identical output (NFR-001). No Codex yet. This de-risks everything downstream by isolating the seam first.

### Phase 2: Codex autonomous loop (the core win)
Implement the `codex` branch of the adapter, `.codex/config.toml.jinja`, `CODEX_HOME` wiring, the `check_hooks_enabled` skip, the `bd`-exemption preflight (FR-006), and the `tail.sh` Codex decoder. Acceptance = M2 + M3 on the tiny-3-task replay queue.

### Phase 3: Copier integration
Add the `agent_cli` question, conditional config/instruction/README emission, and the backend-conditional `_tasks` step. Acceptance = M5 (both `--data agent_cli=claude|codex` generate passing projects).

### Phase 4: Interactive flows (deferred, capability-gapped)
Resolve `interview.sh` / `triage.sh` under Codex. This is blocked on the `AskUserQuestion` gap — Codex has no structured multiple-choice tool. Options to evaluate: fall back to plain-text Q&A in `codex exec`, keep these flows Claude-only when `agent_cli=codex`, or build a small shell-driven prompt loop. Do not block Phases 1–3 on this.

### Phase 5: Docs & parity hardening
README backend sections (FR-012), AGENTS.md orchestrator variants (FR-009), extend `make parity` to cover the new files, and a short "choosing a backend" note.

---

## Epic Breakdown (proposed; refine at decomposition)

- **E1 — Backend adapter + Claude parity (Phase 1).** `lib/backend.sh`; refactor `goal.sh`/`idea.sh` call sites; argv-parity test.
- **E2 — Codex loop (Phase 2).** Codex argv; `.codex/config.toml.jinja`; `CODEX_HOME`; hook-gate skip; `bd` exemption + preflight; sandbox posture (FR-010).
- **E3 — Codex log decoder (Phase 2).** `tail.sh` Codex `--json` branch; backend detection; render parity.
- **E4 — Copier integration (Phase 3).** `agent_cli` question; conditional file emission; conditional `_tasks` (`bd setup codex`/manual); README/CLAUDE/AGENTS variants.
- **E5 — Interactive flows under Codex (Phase 4).** `AskUserQuestion` gap resolution for `interview.sh`/`triage.sh`.
- **E6 — Docs & parity (Phase 5).** `make parity` extension; backend-choice docs.

---

## Open Questions

- **Q1 — RESOLVED (NO): `codex exec` does not honor `/goal`.** Verified against `codex-cli 0.144.6`. Method: run `codex exec --json -s read-only '/goal <condition>'` against a scratch repo with an isolated `CODEX_HOME`, then read the session rollout JSONL (`$CODEX_HOME/sessions/<date>/rollout-*.jsonl`) — the recorded `user_message` event and the corresponding `response_item` both contain the literal string `/goal <condition>`. No expansion, no goal-loop machinery, no client-side error. Repeated with a `$CODEX_HOME/prompts/goal.md` custom-prompt file present: still forwarded verbatim, confirming slash-command expansion is a TUI-only affordance and not part of `exec`. (The turn itself failed on a 401 in the test environment — no OpenAI credentials — but the prompt is recorded in the rollout before any network call, so the pass-through is established independently of auth.)

  **Caveat found while re-verifying (2026-07-18):** Codex 0.144.6 *does* ship a native goal runtime — `codex features list` shows `goals  stable  true`, the binary carries an `ext/goal` extension with a model-callable `update_goal` tool ("the concrete objective to start pursuing… starts a new active goal"), app-server methods `thread/goal/{set,get,clear}`, per-goal token/time budgets, and idle-thread auto-continuation. This does **not** change the Q1 answer: that runtime is reachable from the TUI slash dispatcher (`tui/src/chatwidget/slash_dispatch.rs`) or over the app-server protocol, never by passing a literal `/goal …` string to `codex exec`, which has no custom-prompt expansion path at all (`exec/src/lib.rs` is the whole exec surface). Two implications for Phase 2: (a) `--ephemeral` is incompatible with goals ("ephemeral thread does not support goals"), so the adapter must not pass it if this route is ever adopted; (b) a future alternative to the plain-prompt model is to drive `thread/goal/set` via `codex app-server` instead of `codex exec` — deferred, not adopted, since it trades a stable CLI for an experimental protocol.

  **Fallback termination model (now the Codex design):** put the objective in the plain prompt body plus `AGENTS.md`, and let Codex's own turn loop run to evidence-based completion. `codex exec` already terminates on the same four-way contract the condition files assume — success confirmed against evidence, budget exhausted, insurmountable blocker, or external stop — and it exits the process when the turn completes, which is the signal the Ortus loop needs. The Claude backend keeps its `/goal` + Stop-hook mechanism; the adapter must not assume a shared termination path between backends. Corollary: per-task subprocess iteration (one `codex exec` per issue) is the right loop shape for Codex, since there is no in-session goal continuation to lean on.
- **Q2 — RESOLVED: Codex `--json` event schema pinned.** Verified against `codex-cli 0.144.6`; captured stream checked in as `tests/fixtures/codex-exec-events.jsonl` (happy path) and `tests/fixtures/codex-exec-events-failed.jsonl` (turn failure). Method: the test environment has no OpenAI credentials, so the run was driven against a local stub Responses endpoint wired in via `-c 'model_providers.fake={base_url="http://127.0.0.1:PORT/v1",wire_api="responses",…}'`. Only the *upstream* side is stubbed — every event in the fixture is emitted by the real Codex CLI event pipeline, including genuine sandboxed command execution, so the field paths are authoritative.

  Envelope: one JSON object per line, always with a top-level `type`. Observed types — `thread.started` (`.thread_id`), `turn.started`, `turn.completed` (`.usage`), `turn.failed` (`.error.message`), `item.started` / `item.completed` (`.item`), and a bare `error` (`.message`) for transient retries. There is **no** standalone `usage` event: token counts arrive only on `turn.completed`. Items carry a stable `.item.id` (`item_N`) reused across `item.started` → `item.completed`, so a renderer updates in place rather than appending twice.

  Field paths `tail.sh` needs, by `.item.type`:

  | `.item.type` | Fields |
  |---|---|
  | `agent_message` | `.item.text` — assistant text (single field, not a content array) |
  | `reasoning` | `.item.text` — flattened summary text, Markdown-ish |
  | `command_execution` | `.item.command` (fully-expanded argv string, e.g. `/bin/bash -lc "…"`), `.item.aggregated_output` (stdout+stderr merged), `.item.exit_code` (`null` while in progress), `.item.status` ∈ `in_progress` \| `completed` \| `failed` |
  | `todo_list` | `.item.items[]` with `.text` and `.completed` (from the model's `update_plan` tool call) |
  | `error` | `.item.message` — non-fatal, e.g. unknown-model metadata warnings |

  Token counts (on `turn.completed.usage`): `.input_tokens`, `.cached_input_tokens`, `.output_tokens`, `.reasoning_output_tokens` — flat, cumulative across the whole turn, and **differently named from the Responses-API wire fields** (`input_tokens_details.cached_tokens` → `cached_input_tokens`, `output_tokens_details.reasoning_tokens` → `reasoning_output_tokens`); there is no `total_tokens`.

  Two gaps worth recording: (a) **no file-edit item type was observed** — Codex 0.144.6 advertises `exec_command` / `write_stdin` / `update_plan` / `request_user_input` / `view_image` as its tool surface, with no `apply_patch` tool, so file edits reach the stream as ordinary `command_execution` items and the decoder must not expect a `file_change` item; (b) `todo_list` re-emits on `item.completed` with the *original* (unmutated) item body, so completion state cannot be read from the final event.
- **Q3 — Does `bd setup codex` exist?** `copier.yaml:214` runs `bd setup claude`. If beads has no Codex profile, define the manual/no-op equivalent (FR-011) and file an upstream note (do NOT modify beads here).
- **Q4 — `bd` under the Codex workspace-write sandbox**: exact `network_access` / write-scope config that keeps the embedded Dolt store reachable without over-granting. Determine empirically in Phase 2 (FR-006).
- **Q5 — Model selection**: which Codex model is the default for Ortus projects, and how is it surfaced (a `codex_model` copier question, or config-only)? Decide during Phase 3.
- **Q6 — Does Codex have a `bd prime`-style session hook** analog to the Claude `SessionStart`/`PreCompact` hooks that run `bd prime`? If not, fold the priming into `AGENTS.md` guidance.

## Out of Scope

- **Interactive `AskUserQuestion` flows on Codex** beyond Phase 4 scoping — no commitment to a specific replacement in this PRD.
- **Porting Claude-specific prompt sections** (Sonnet/Opus subagent table, `/compact`, Compaction API references in `goal-prompt.md`) to Codex-optimal equivalents. Phase 2 gates or neutralizes them; optimizing Codex-native subagent/skills usage is a follow-up.
- **Changing Anthropic/Claude behavior.** The default path is untouched.
- **Multi-backend in a single project simultaneously** (one project, one `agent_cli`; the `--backend` override is for flipping, not concurrent use).
- **Modifying the beads (`bd`) tool** to add a Codex profile — that is upstream work; here we consume whatever `bd` offers.

## Appendix

### Appendix A: Flag translation reference (adapter contract)

| Role | Claude argv | Codex argv |
|---|---|---|
| `goal` | `claude -p "$P" --output-format stream-json --verbose --dangerously-skip-permissions [$FAST_MODE]` | `codex exec "$P" --json --sandbox workspace-write --dangerously-bypass-approvals-and-sandbox [-m $MODEL]` (FAST_MODE → no-op) |
| `prd-decompose` | `claude --allowedTools "Read($prd),Bash(bd:*)" --dangerously-skip-permissions -p "$P"` | `codex exec "$P" --sandbox workspace-write --ask-for-approval never [-m $MODEL]` (no per-tool allowlist) |
| `idea-expand` | `claude --print "$P"` (capture stdout) | `codex exec "$P" -o <tmp>` then read tmp, or capture stdout (no goal loop) |

Env prefix for Codex roles: `CODEX_HOME=$PWD/.codex`. Outer sandbox (bwrap/Seatbelt/`--docker`) wraps both backends identically.

### Appendix B: File-change map

| File | Change |
|---|---|
| `ortus/lib/backend.sh` | **new** — adapter |
| `ortus/goal.sh` | route invocation via adapter; gate `check_hooks_enabled` on claude |
| `ortus/idea.sh` | route both calls via adapter |
| `ortus/tail.sh` | add Codex `--json` decoder branch |
| `template/.codex/config.toml.jinja` | **new** — Codex config (emitted when `agent_cli=codex`) |
| `template/.claude/settings.json.jinja` | emit conditionally (`agent_cli=claude`) |
| `copier.yaml` | add `agent_cli` question; conditional `_tasks` (`bd setup codex`); conditional file emission |
| `template/CLAUDE.md.jinja`, `template/AGENTS.md` | backend-variant orchestrator lines; ensure Codex-needed content lives in AGENTS.md |
| `template/README.md.jinja`, `template/ortus/README.md.jinja` | backend-variant Requirements/Sandbox/Auth |
| (mirror all of the above into `template/ortus/…` per parity) | |

### Appendix C: Reference links

- Codex goals cookbook: https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex
- Codex non-interactive mode: https://learn.chatgpt.com/docs/non-interactive-mode
- Codex security/sandbox & approvals: https://learn.chatgpt.com/docs/security
- Codex CLI repo: https://github.com/openai/codex
- Ortus `/goal` migration precedent: `prd/PRD-goal-directive.md`

### Appendix D: Key risks

1. **Q1 resolved (risk realized)** — `codex exec` does not run `/goal`; Phase 2 must build on the plain-prompt termination model. Retired as an unknown; the remaining exposure is that the two backends now have genuinely different termination paths, so the adapter cannot share one.
2. **`bd` under Codex sandbox** — the exemption is the single most likely source of a silently broken loop; FR-006 preflight is mandatory before declaring Phase 2 done.
3. **Interactive-flow gap** — `AskUserQuestion` has no Codex peer; scoped out to Phase 4 so it doesn't block the core win.
4. **Two config schemas to maintain** — mitigated by generating only the active backend's config and keeping shared logic (network allowlist from `language_profile`) in one templating path.
