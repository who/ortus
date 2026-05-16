# PRD: Migrate the Ortus Ralph loop to the `/goal` directive

## Metadata

- **Feature ID**: ortus-goal-migration (proposed; assign at decomposition)
- **Project Type**: Ortus core tooling (Copier template + canonical `ortus/`)
- **Created**: 2026-05-16
- **Author**: Claude (cross-reading `~/code/ortus`, `~/code/beads-v1.0.4`, `code.claude.com/docs/en/goal`, Ghuntley's [how-to-ralph-wiggum](https://github.com/ghuntley/how-to-ralph-wiggum), and the [who/ralph-beads](https://github.com/who/ralph-beads) PoC)
- **Interview Confidence**: High (artifacts read end-to-end; three direct decision points confirmed with the user: replace ralph.sh, full PRD shape, strictly additive on invariants)
- **Revision**: 2026-05-16 (b): shared-dolt-lifecycle invariant dropped — `ralph.sh` no longer orchestrates a sql-server because bd 1.0.3 embedded mode is the default. All references to `start_dolt`/`stop_dolt`/`recover-dolt.sh`/`.beads/dolt-server.*` removed. Invariants `flock guard`, `sandbox smoke test`, `--docker`, and `cache relocation` are unchanged. FR-006 reserved (was: shared dolt lifecycle preservation). Q1 (shared-dolt × `--resume`) deleted as moot.

---

## Overview

### Problem Statement

Ortus's `ortus/ralph.sh` orchestrates autonomous execution by spawning a fresh `claude -p` subprocess for every iteration and parsing a stringly-typed `<promise>COMPLETE|EMPTY|BLOCKED</promise>` sentinel from stream-json output to decide whether to loop, exit, or sleep. This shape is load-bearing in important ways — process isolation, fresh-context-per-task, atomic git+bd commits between iterations, the flock invariant — but it also pays meaningful costs that have grown since Ortus was first scaffolded:

1. **Per-iteration boot overhead.** Each iteration restarts `claude`, re-establishes MCP connections (CodeGraph, beads-adjacent), re-bootstraps caches under `XDG_CACHE_HOME`, and re-loads `AGENTS.md` + the orient block. On a 100-issue queue this is real wall-clock waste.
2. **Sentinel parsing is heuristic-shaped.** `ralph.sh` greps stream-json for a literal `<promise>X</promise>` string. If the model omits the sentinel, mangles it, or produces it in a tool-call payload rather than assistant text, the loop misroutes (treats `BLOCKED` as `EMPTY`, sleeps when it should exit, exits when it should sleep). This is exactly the shape `ZFC.md` cautions against under "decision trees on unstructured input."
3. **No declarative completion contract.** The "we're done" condition is encoded as a fixed-point of three independent branches in shell (`--tasks` cap, sentinel string match, error fallthrough → sleep). A reader cannot point to a single line that says "Ralph is finished when X."
4. **Non-Ralph flows have no terminator at all.** `idea.sh`, `interview.sh`, and `prd-decompose-prompt.md` end with `tell the user to type /exit` — there is no autonomous "PRD is approved; the feature is decomposed; you may stop now" handoff. The user is the loop terminator.

Anthropic's new [`/goal` directive](https://code.claude.com/docs/en/goal) (Claude Code ≥ v2.1.139) provides a primitive that maps cleanly onto items 2 and 3 — a small fast model evaluates a declarative completion condition after every turn and decides whether to keep going — and that *can* address item 1 if we are willing to graduate from subprocess-per-iter to session-per-run. Doing so is the user-confirmed direction of this PRD.

### Proposed Solution

Introduce a new orchestrator (`ortus/goal.sh`, with `ralph.sh` graduating into a thin compatibility shim that delegates to it) that runs Ortus's autonomous loop as a **single long-lived `claude -p "/goal <condition>"` session** while preserving every load-bearing invariant of today's `ralph.sh`:

- **Same flock single-instance guard** wraps the `claude -p "/goal ..."` invocation. Re-exec semantics under `flock(1)` are unchanged.
- **`bd` sandbox exemption preserved**. The session sees `bd` exactly as it does today via `sandbox.excludedCommands: ["bd", "bd *"]`. bd 1.0.3 embedded mode means no separate sql-server lifecycle to manage; bd reads/writes the embedded DB directly within the workspace.
- **Same sandbox smoke test** and **same `--docker` Tier-2 routing**.
- **Same cache relocation** (`XDG_CACHE_HOME`, `UV_CACHE_DIR`, etc.).
- **Fresh-per-task semantics preserved** inside the session via three mechanisms: (a) the main session is a pure scheduler that delegates every read, write, and validation to subagents (already Ortus policy — see ralph-prompt.md "Subagent Strategy"); (b) the goal-prompt instructs an explicit context-discard ritual between tasks; (c) `/compact` is invoked deterministically after each `bd close` so accumulated tool output never crosses the 60% smart-zone line.

Termination becomes a single declarative sentence: `/goal Drive the bd queue to zero — bd ready returns [] and no issues are in_progress, or N tasks have been completed, or T wall-clock minutes have elapsed.` The Haiku evaluator (Claude Code's default small fast model) judges this against the conversation each turn; the brittle `<promise>X</promise>` sentinel can be retired.

In parallel, `/goal` is introduced into the upstream flows where it is a strict capability gain rather than a replacement:

- `interview.sh` gains `/goal feature {{FEATURE_ID}} has label "approved" and at least one child task exists in bd` — the AskUserQuestion-driven conversation self-terminates instead of waiting for the human to type `/exit`.
- `idea.sh --prd` gains `/goal every work item in {{PRD_PATH}} has a corresponding bd issue with type and dependencies set` — `prd-decompose-prompt.md` stops asking the user to type `/exit`.
- Developers gain `ortus/goal.sh -c "<arbitrary condition>"` as a first-class "scope an autonomous run to this end state" primitive (e.g., `-c "all children of bd-auth-epic are closed"`).

This is a **replacement at the orchestrator level**, **strictly additive at the invariant level**. The user-confirmed risk posture (q3) explicitly forbids disturbing the flock guard, sandbox exemptions, or fresh-per-iter semantics. The architecture below reproduces every one of those invariants around the new `/goal` engine; nothing is dropped, only moved. (q3's original list included the shared-dolt lifecycle; that invariant was retired in the 2026-05-16 (b) revision when bd embedded mode became the default and the orchestration was removed from `ralph.sh`.)

### Success Metrics

- **M1 — Boot-cost reduction**: median wall-clock per closed bd issue under `goal.sh` is ≤ 70% of the same workload under `ralph.sh`, measured on a fixed 20-issue replay queue (acceptance threshold; design target is ~50%).
- **M2 — Sentinel-class regressions eliminated**: zero "missed termination" or "spurious sleep" incidents in 30 days of continuous use across at least three generated projects, where the legacy run logged ≥ 1 such incident per 100 iterations during the same period.
- **M3 — Smart-zone discipline**: across a 50-issue session, the main-context token utilization measured at each turn-N stays ≤ 60% in p95 and ≤ 80% in p100, demonstrating that fresh-per-task is preserved despite the shared session. Metric collected via a lightweight hook that logs the per-turn `usage.input_tokens` from stream-json.
- **M4 — Upstream-flow autonomy**: `interview.sh` and `idea.sh --prd` complete end-to-end (PRD generated, label transitions advanced, child tasks created) without the user ever typing `/exit` to terminate the Claude session. Measured by absence of a `bash exit_code == 130` (Ctrl+C / interactive abort) signal in the trailing 10 invocations.
- **M5 — Invariant parity**: a parity test (extending the existing `make parity`) asserts that `goal.sh` and `ralph.sh` declare identical flock paths, sandbox smoke-test calls, `--docker` precondition checks, and cache env-var exports. The test fails on any drift.

---

## Background & Context

### Why now?

`/goal` shipped in Claude Code v2.1.139. It is the first first-party primitive that lets Anthropic's small-fast-model judge a stop condition *inside* a session, against the conversation as written. Before this, Ortus had two practical choices: a shell-level loop (Ralph) or a Stop hook. Ralph won at scaffold time because it was the only option with the shape Ortus needed (fresh context per task, atomic commits, restartable). Stop hooks were too coarse — they fire after every turn in every session, not just inside a scoped autonomous run.

`/goal` is a third option that splits the difference: a *session-scoped, condition-evaluated, declarative loop terminator*. It is functionally a managed Stop hook with a UI, a status indicator, and an automatic clear-on-success, but its real contribution is the **declarative condition contract** — a single sentence that says what "done" means, evaluated by a separate model that has no incentive to claim premature completion. That contract is something `ralph.sh` cannot express today.

### Prior art and alternatives considered

| Pattern | What it is | Why not it (alone) |
|---|---|---|
| Status quo (`ralph.sh`) | Subprocess-per-iter, sentinel-string termination | Brittle on sentinel emission; per-iter boot cost; no declarative end state |
| `/loop INTERVAL` | Time-based re-firing of a prompt in the same session | Wrong contract: Ralph fires when the *previous turn finishes*, not on a clock |
| Stop hook (script-based) | Custom hook that decides after every turn | Bypasses Ortus's "command-shaped" UX; harder for downstream Copier-generated projects to discover and override |
| Stop hook (prompt-based) | Same shape as `/goal`, hand-rolled | `/goal` *is* the managed version of this — reinventing it is pure cost |
| Headless `claude -p "/goal ..."` | The proposal here | Adopted |
| External agent runner (Tier 3) | Cloud routine via `claude` scheduled tasks | Out of scope for this PRD; complementary, not substitutive |

### How `/goal` actually works (verbatim from docs)

- One goal can be active per session. `/goal CONDITION` sets it; `/goal` shows status; `/goal clear` cancels.
- Setting a goal **starts a turn immediately** with the condition as the directive.
- After every turn, the [configured small fast model](https://code.claude.com/docs/en/model-config) (Haiku by default) sees the condition and the conversation, returns yes/no + a short reason. The reason is surfaced as guidance for the next turn.
- The evaluator **does not call tools** — it can only judge what the main session has already surfaced in the conversation. *This is a load-bearing constraint for prompt design (FR-005).*
- Goals are restored on `--resume`/`--continue` but **turn count, timer, and token baseline reset**.
- `/goal` works in non-interactive mode: `claude -p "/goal CONDITION"`.
- Requires the workspace trust dialog accepted; unavailable when `disableAllHooks` is set or when `allowManagedHooksOnly` is set in managed settings (`/goal` is implemented as a managed Stop hook).
- Condition limit: **4,000 characters**. Comfortable for our condition strings.

### How Ortus's Ralph actually works (audited from `ortus/ralph.sh` and `ortus/prompts/ralph-prompt.md`)

Mechanics that must survive any replacement:

| Invariant | Where it lives today | Why it is load-bearing |
|---|---|---|
| Single-instance per repo | `ralph.sh:50-93` — pre-flight `flock -n -x .beads/ralph.flock true`, then `exec flock -n -E 1 ... "$0" "$@"` | Two concurrent ralphs would race on bd writes (embedded-DB write contention), collide on per-task log files, and double-claim issues. The guard scopes one autonomous loop to one workspace at a time. (Historical note: the original justification was orphan `dolt sql-server` pile-up under server mode; embedded mode eliminates that failure class but the per-workspace concurrency concern stands.) |
| `bd` sandbox exemption | Generated `.claude/settings.json` — `sandbox.excludedCommands: ["bd", "bd *"]`; **both entries required** | The OS sandbox blocks loopback TCP and arbitrary file writes outside the project root; bd needs both for its embedded DB access. Exemption only fires when the harness sees `bd` as the directly-invoked bash command. Wrapping (`bd ... | jq`, `xargs bd`, `bash -c "bd ..."`) breaks the exemption. |
| Sandbox smoke test | `ralph.sh:156-183` — fails fast if `bwrap` (Linux) or `sandbox-exec` (macOS) is missing; not skippable | Without it, `--dangerously-skip-permissions` runs unsandboxed and the "silent degradation" failure mode reopens. |
| Tier-2 `--docker` routing | `ralph.sh:190-217, 259-263` — `docker sandbox run claude --name ortus-ralph --` | Container isolation for CI runners without native sandbox prereqs. |
| Cache relocation | `ralph.sh:225-237` — `XDG_CACHE_HOME=$PWD/.cache` and per-tool env vars | The sandbox profile mounts `~/.cache` read-only; tools need a writable cache surface. |
| Fresh-per-iter execution | `ralph.sh:273` — `claude -p "$(cat ralph-prompt.md)" ... | tee` then loop restarts | The Ghuntley manifesto and Ortus's own `ralph-prompt.md` ("Context Management" section) treat this as the source of determinism. |
| One task per invocation | `ralph-prompt.md` "Important Rules" — `Do not run bd ready a second time. Do not claim a second issue.` | Forces atomic commit boundaries; prevents cross-task state bleed in main context. |
| JSON-validated plan | `ralph-prompt.md` "Issue Plan" — `{has_enough_info, missing, implementation_steps, verification_steps, closure_reason}` | The ZFC-aligned alternative to client-side branching on issue type. Already in place. |
| CodeGraph integration (optional) | `ralph-prompt.md` steps 1, 4, 6.5, 7, 7.5 — gated on `codegraph_available` | Cheap structural lookup; falls back silently. |
| Completion-comment schema | `ralph-prompt.md` "Completion Comment Format" + `**CodeGraph v1**` block | The structured comments are the durable cross-iteration memory; the v1 block is parsed by the next iteration's step 1.5. |

The user-confirmed scope (q1: "Replace ralph.sh") and risk posture (q3: "Strictly additive" on invariants) together mean: replace the *orchestration shape*, reproduce *every invariant above* in the new shape, ship side-by-side until parity is demonstrated.

---

## Users & Personas

### Primary Users

1. **Ortus contributors** — work in `~/code/ortus`, edit `ortus/` and `template/ortus/` in parity. Need the canonical scripts to be debuggable, readable, and to compose cleanly with `make parity`.
2. **Downstream project users** — generated projects via `copier copy gh:who/ortus`. They never read `ralph.sh`; they run `./ortus/ralph.sh` and expect it to drive the bd queue to empty.
3. **CI / scheduled runs** — automation that invokes `./ortus/ralph.sh --tasks N` or `./ortus/ralph.sh --iterations N` headlessly.

### User Goals

- **G1**: "I want the autonomous loop to finish when there is nothing left to do, without me having to babysit it or type `/exit`."
- **G2**: "I want a single readable line that says when Ralph stops."
- **G3**: "When I scope an autonomous run ('finish the auth epic'), I want Ortus to honor that scope and stop the moment it is satisfied."
- **G4**: "I do not want to give up the determinism I get from fresh context per task."
- **G5**: "I do not want to lose the safety I get from the flock, the sandbox, or the bd exemption."

### Current Workflows (and what is painful)

- **Workflow A — Drive the queue to empty.** Today: `./ortus/ralph.sh`. Loop iterates until sentinel-`EMPTY` or no-signal-with-completed > 0. Pain: when the model omits the sentinel, the loop sleeps for 60 s and retries instead of exiting; when the model emits BLOCKED for an empty queue (the prompt has a guardrail against this but it is a guardrail, not a guarantee), the loop logs "Task blocked" and re-attempts.
- **Workflow B — Decompose a PRD.** Today: `./ortus/idea.sh --prd path/to/PRD.md` → `prd-decompose-prompt.md` runs to completion, then prints "tell the user to type /exit to continue." Pain: the user is the loop terminator; no autonomous handoff to ralph.sh.
- **Workflow C — Interview a feature.** Today: `./ortus/interview.sh` drives a Claude session through AskUserQuestion. Session ends only when the model decides to stop or the user Ctrl+C's. Pain: no declarative "the feature is approved → end the session" contract.
- **Workflow D — Scope an autonomous run.** Today: not supported. Closest equivalent is to manually create a bd query that returns only the desired issues, then run `ralph.sh --tasks N` and hope.

---

## Requirements

### Functional Requirements

**Replacement orchestrator (the main work):**

- **[P0] FR-001** — The system shall provide `ortus/goal.sh`, an autonomous-execution orchestrator that runs a single long-lived `claude -p "/goal <condition>"` session with `--dangerously-skip-permissions`, `--output-format stream-json`, `--verbose`, and (when `--fast` is passed) the `--fast` flag.
- **[P0] FR-002** — `goal.sh` shall accept the same flag surface as today's `ralph.sh`: `--fast`, `--idle-sleep N`, `--tasks N`, `--iterations N`, `--docker`. Where a flag does not map onto `/goal`'s shape (notably `--iterations`, which is per-subprocess in ralph.sh), the flag shall fold into the goal condition string (e.g., `... or stop after N turns`) per the `/goal` doc's "include a turn or time clause" guidance.
- **[P0] FR-003** — `goal.sh` shall additionally accept `-c, --condition "<string>"` to set an arbitrary completion condition, defaulting to the canonical "drive the queue to zero" condition specified in FR-004.
- **[P0] FR-004** — The default condition shall be the literal string defined in Appendix A (Canonical Condition). It shall encode: (a) `bd ready --json` returns `[]`, AND (b) no issues are `in_progress`, OR (c) `--tasks N` cap reached, OR (d) `--iterations N` turn cap reached. The condition shall be ≤ 4000 characters and shall reference fields the main session has surfaced to the transcript (per the `/goal` evaluator constraint).

**Invariant preservation (strict additivity per q3):**

- **[P0] FR-005** — `goal.sh` shall execute the same `flock(1)` re-exec dance as `ralph.sh:50-93`, using the same lock file (`.beads/ralph.flock`) so the two scripts mutually exclude each other during the migration window.
- **FR-006 — *Reserved.*** *(Was: shared dolt lifecycle preservation. Removed in 2026-05-16 (b) revision; bd 1.0.3 embedded mode is the default, `ralph.sh` no longer orchestrates a sql-server, and there is nothing to preserve. The `cleanup_children` trap that survives in `ralph.sh` handles claude-child reaping on graceful EXIT/INT/TERM; `goal.sh` shall install the same trap (this becomes part of FR-001's scope rather than its own FR). Number retained to avoid renumbering downstream cross-references.)*
- **[P0] FR-007** — `goal.sh` shall run the same `sandbox_smoke_test` and `docker_precondition_check` as `ralph.sh:156-223`, extracted to `ortus/lib/sandbox.sh`. Behavior unchanged; non-skippable; same install hints.
- **[P0] FR-008** — `goal.sh` shall export the same cache env vars as `ralph.sh:225-237`. Extracted to `ortus/lib/cache.sh`.
- **[P0] FR-009** — The generated project's `.claude/settings.json` `sandbox.excludedCommands` shall remain `["bd", "bd *"]` unchanged. `goal.sh` shall not introduce any wrapping (no `bd | jq`, no `xargs bd`, no `bash -c "bd ..."`) into its prompts.
- **[P0] FR-010** — `goal.sh` shall preserve per-run log files under `logs/goal-<timestamp>.log` analogous to today's `logs/ralph-<timestamp>.log`. `ortus/tail.sh` shall continue to follow both prefixes.

**Goal-prompt (the new `ortus/prompts/goal-prompt.md`):**

- **[P0] FR-011** — The goal-prompt shall reproduce the per-task loop currently encoded in `ralph-prompt.md` (orient → select → claim → investigate → implement → verify → log → close → commit/push) as the **body** the model executes between turns, but shall replace step 10 ("Exit. Output `<promise>X</promise>` and stop.") with: "End the turn. Do not output any sentinel. The /goal evaluator will judge whether the queue is empty from this turn's `bd ready` output."
- **[P0] FR-012** — The goal-prompt shall include an explicit "context-discard ritual" between tasks: after `bd close`, the main session runs `/compact` and then re-establishes scheduler state by re-reading `AGENTS.md`. This preserves the smart-zone discipline that fresh-per-iter gave us for free.
- **[P0] FR-013** — The goal-prompt shall preserve the JSON `Issue Plan` schema (`has_enough_info`, `missing`, `implementation_steps`, `verification_steps`, `closure_reason`) verbatim. The schema is the existing ZFC-aligned contract between scheduler and model; it does not change.
- **[P0] FR-014** — The goal-prompt shall preserve the structured completion-comment format (`**Changes**:` + `**Verification**:` + optional `**CodeGraph v1**:` block) verbatim. Cross-iteration memory is the comment trail; it must not break.
- **[P0] FR-015** — The goal-prompt shall preserve the Subagent Strategy table (Reads/Writes/Validation/Reasoning) verbatim. The "main = scheduler only" rule is what preserves fresh-per-task semantics inside a long-lived session.
- **[P1] FR-016** — The goal-prompt shall preserve the CodeGraph integration (steps 1.5, 4, 6.5, 7, 7.5) verbatim, gated on `codegraph_available`. CodeGraph behavior is byte-equivalent regardless of orchestrator.
- **[P1] FR-017** — The goal-prompt shall retain a fallback `<promise>BLOCKED</promise>` sentinel emission rule for the specific case where the model claims an issue, cannot complete it, and adds a bd comment with the blocker. The orchestrator does not parse this; it survives as a transcript marker that the next-turn evaluator can read. (The scheduler-relevant sentinels — `EMPTY` and `COMPLETE` — are retired; `BLOCKED` is retained for transcript clarity, not for control flow.)

**Upstream-flow integration (the additive wins):**

- **[P1] FR-018** — `interview.sh` shall invoke `claude --allowedTools "AskUserQuestion,Bash(bd:*),Read" "/goal <condition>"`, where the condition asserts that feature `{{FEATURE_ID}}` has label `approved` and at least one child task exists in bd. The interview-prompt's existing Step 6 (which adds the `approved` label) becomes the natural goal-met signal. The user no longer types `/exit`.
- **[P1] FR-019** — `idea.sh --prd` shall invoke `claude ... "/goal every work item in {{PRD_PATH}} is reflected by a bd issue (type set, dependencies set per the PRD's sequence)"`. `prd-decompose-prompt.md`'s final "tell the user to type /exit" instruction shall be removed.
- **[P2] FR-020** — Provide `ortus/goal.sh -c "<scope>"` for developer-facing one-shot scoped runs (Workflow D in §Users). Examples documented in the script header.

**Compatibility & rollout:**

- **[P0] FR-021** — `ralph.sh` shall remain functional and unchanged in semantics through the rollout window. After parity is demonstrated (Phase 3), `ralph.sh` shall become a deprecation shim that prints a one-line notice and `exec`s `goal.sh "$@"`. The shim shall remain for at least one minor version after the swap.
- **[P0] FR-022** — `make parity` shall be extended to assert (a) canonical `ortus/` and templated `template/ortus/` agree on `goal.sh`, the new `lib/*.sh`, and `prompts/goal-prompt.md`; (b) `goal.sh` and `ralph.sh` agree on flock path, sandbox smoke-test calls, `--docker` precondition checks, and cache env-var exports. The test fails on any drift.
- **[P1] FR-023** — A one-shot replay harness (`scripts/replay-queue.sh`) shall load a fixed 20-issue queue against both `ralph.sh` and `goal.sh` in clean copies of a generated project, recording per-issue wall-clock and total-input-token counts to support M1 and M3.

### Non-Functional Requirements

- **[P0] NFR-001** — `goal.sh` shall not regress sandbox safety. Specifically: the smoke test remains non-skippable; `--dangerously-skip-permissions` is gated on smoke-test pass; no env var (e.g., `RALPH_LOCK_HELD` analog) introduces a skip path. Audit acceptance: a code reviewer can read `goal.sh` top-to-bottom and confirm no escape hatch exists.
- **[P0] NFR-002** — `goal.sh` shall not regress concurrency safety. A second `goal.sh` invocation against the same repo shall print the same actionable diagnostic that `ralph.sh:55-83` prints today (with `goal.sh` paths substituted), then exit 1. A `goal.sh` invocation while `ralph.sh` is running (or vice-versa) shall also exit 1 with a diagnostic that references both names.
- **[P0] NFR-003** — `goal.sh` shall not regress determinism. The Subagent Strategy table (FR-015) and `/compact` ritual (FR-012) together must keep main-context utilization in the smart zone (M3). A replay run showing > 60% p95 utilization is a release blocker.
- **[P1] NFR-004** — `goal.sh` shall preserve ZFC compliance. The orchestrator shall not branch on the semantic content of bd output, label names, or issue descriptions. The Haiku evaluator's yes/no decision is the only client-side branch; everything else is mechanical execution of model-emitted plans.
- **[P1] NFR-005** — `goal.sh` shall produce log files compatible with `ortus/tail.sh`. The tail script shall be updated to glob both prefixes; the change shall be backwards-compatible with `logs/ralph-*.log`.
- **[P2] NFR-006** — `goal.sh` shall be cross-platform parity with `ralph.sh` (Linux/WSL2 with bwrap+socat; macOS with Seatbelt; unsupported elsewhere — exit 1 with the same hints).
- **[P2] NFR-007** — `goal.sh -h` shall print usage analogous to `ralph.sh -h`, including the new `-c CONDITION` flag and the canonical condition default.

### ZFC Rubric Audit

| Decision point in `goal.sh` / goal-prompt | Allowed (dumb pipe) | Forbidden (local intelligence) | Verdict |
|---|---|---|---|
| "Is the queue empty?" | Delegated to Haiku evaluator with a condition referencing `bd ready` output in the transcript | (Old): grep `<promise>EMPTY</promise>` in stream-json | **Allowed — improvement over status quo** |
| "Is the issue ambiguous?" | Model emits `has_enough_info: false` with `missing[]`; scheduler reads schema | Scheduler infers ambiguity from description text | **Allowed — unchanged from today** |
| "What's the next task?" | `bd ready --json` returns ordered list (beads' job); model picks first | Scheduler scores issues by description content | **Allowed — unchanged from today** |
| "Should we keep iterating?" | Goal condition is the sole signal; evaluator decides | Shell counts completed sentinels and infers | **Allowed — improvement over status quo** |
| "Has the user 'approved' the PRD?" (interview.sh) | Condition references `label="approved"` (state schema per LABELS.md) | Shell greps interview transcript for "approved" keyword | **Allowed** |
| "Cap iterations at N" | Condition contains literal "or stop after N turns"; evaluator counts | Shell parses turn count from stream-json | **Allowed** |
| Per-task plan execution | Mechanical execution of JSON `implementation_steps` then `verification_steps` | Shell branches on plan content | **Allowed — unchanged from today** |

Net effect: `goal.sh` is **strictly more ZFC-aligned** than `ralph.sh`. The one remaining client-side heuristic (sentinel grep) is removed; the only client-side branch becomes the evaluator's yes/no, which is itself a model call against a schema-validated condition.

---

## System Architecture

### High-Level Components

```
┌──────────────────────────────────────────────────────────────────────┐
│ Developer / CI                                                       │
│   $ ./ortus/goal.sh [--fast --tasks N --iterations N --docker -c C]  │
└────────────────────────────┬─────────────────────────────────────────┘
                             │
                  ┌──────────▼──────────┐
                  │  ortus/goal.sh      │  (orchestrator; ~250 LOC)
                  │  - flock guard      │
                  │  - smoke test       │  ◄── ortus/lib/sandbox.sh
                  │  - cache exports    │  ◄── ortus/lib/cache.sh
                  │  - claude -p "/goal CONDITION"                     │
                  └──────────┬──────────┘
                             │
            ┌────────────────▼─────────────────┐
            │  long-lived `claude` session     │
            │  - reads goal-prompt.md          │
            │  - loops turns under /goal       │
            │  - delegates to subagents (sched)│
            │  - /compact between tasks        │
            │  - emits bd writes via host bd   │
            └────────┬────────────┬────────────┘
                     │            │
              ┌──────▼─────┐  ┌───▼──────────┐
              │ Subagents  │  │ bd (host)    │──► embedded DB
              │ (writes,   │  │ via sandbox  │    (bd 1.0.3+;
              │  reads,    │  │ excludedCmds │     no sql-server,
              │  validate) │  │              │     no PID, no port)
              └────────────┘  └──────────────┘
                                      ▲
                                      │ sandbox exemption fires because
                                      │ the harness sees `bd` directly
                                      │ (any wrapping breaks it)
                                      │
                       ┌──────────────┴────────┐
                       │ Haiku evaluator       │
                       │  (Claude Code Stop    │
                       │   hook under /goal)   │
                       │  - judges condition   │
                       │  - returns yes/no +   │
                       │    short reason       │
                       └───────────────────────┘
```

### Component Interactions

1. **`goal.sh` → flock → smoke test → cache exports → `claude -p "/goal CONDITION"`** — the start-up sequence is byte-equivalent to `ralph.sh` up to the `claude` invocation (FR-005, FR-007, FR-008). The only difference is that `claude` is launched **once**, not once per task.
2. **Claude session → goal-prompt → per-task loop body** — the prompt drives orient/select/claim/investigate/implement/verify/log/close/commit/push per turn (FR-011). Each turn closes exactly one bd issue.
3. **Between turns: `/compact`** — the prompt instructs the main session to run `/compact` after `bd close` succeeds (FR-012). The compaction summarizes the just-finished task into a one-line "task X closed, files Y modified, verification Z" turn, discarding tool output. This is the in-session analog of fresh-per-iter.
4. **`/goal` evaluator (Haiku) → yes/no + reason** — after every turn, the evaluator reads the conversation, judges the canonical condition, and either keeps the session alive or clears the goal and ends it. The "reason" surfaces in the status indicator.
5. **`cleanup_children` trap on session exit** — when the goal clears (or the user Ctrl+C's, or `--tasks N` cap triggers via the condition), the session exits, the trap fires, any direct `claude -p` children are SIGKILL'd, and the flock is released. (No dolt server to stop in embedded mode.)

### Technical Decisions

| Decision | Rationale |
|---|---|
| Long-lived session, not subprocess-per-iter | Direct user instruction (q1); enables boot-cost amortization (M1) |
| Preserve `ralph.sh` invariants byte-for-byte via shared `lib/*.sh` | Direct user instruction (q3); parity asserted by `make parity` (FR-022) |
| Retire scheduler-relevant sentinels (`EMPTY`, `COMPLETE`); keep `BLOCKED` as transcript marker | Sentinel parsing is exactly the heuristic shape `ZFC.md` forbids; `/goal` evaluator is the ZFC-aligned replacement |
| Use `/compact` for inter-task isolation rather than `/clear` | `/clear` also clears the active goal (per docs); `/compact` preserves the goal and summarizes prior turns |
| Default condition references `bd ready` output, not internal state | The `/goal` evaluator cannot call tools (per docs); it judges from transcript. The condition must reference something the model has surfaced. |
| Default to Haiku for the evaluator | Claude Code default; condition checks are short; Haiku is the cheapest correct choice |
| Ship `goal.sh` side-by-side with `ralph.sh` for one phase, then deprecate | Reduces blast radius; lets users (and parity tests) validate before swap |
| Extract `sandbox`/`cache` setup into `ortus/lib/*.sh` | Required to make parity structural (FR-022) without code duplication. Note: this is a behavior-preserving refactor; q3 prohibits touching the *behavior*, not the file layout. (The originally planned `lib/dolt.sh` was dropped in the 2026-05-16 (b) revision; nothing to extract once embedded mode eliminated the lifecycle.) |
| Do NOT introduce `RALPH_GOAL_SKIP_SMOKE_TEST=1` or any analogous env var | NFR-001; same reasoning as `ralph.sh` ("skippability re-introduces the silent-degradation failure mode that sandbox hardening is designed to eliminate") |
| Do NOT replace `<promise>BLOCKED</promise>` (FR-017) | The evaluator reads transcript; `BLOCKED` is the model's structured way of saying "I claimed this and couldn't finish." Removing it would lose information the evaluator needs to judge the condition. |

### Data Model

No new persistent state. Existing state stores are preserved:

- **bd**: issues, comments, dependencies, labels — unchanged. (Backed by Dolt embedded mode since bd 1.0.3; no separate sql-server, no `.beads/dolt-server.*` state files.)
- **Git**: per-task commits with issue ID in message — unchanged.
- **`logs/`**: new prefix `logs/goal-<timestamp>.log` alongside existing `logs/ralph-<timestamp>.log`.
- **`.beads/ralph.flock`**: shared lock file; both scripts use it (FR-005).

---

## Milestones & Phases

### Phase 1: Foundation (extract, do not change)

**Goal**: Land the structural refactor that makes parity testable without touching behavior. Nothing in this phase changes runtime semantics of `ralph.sh`.

**Deliverables**:

- `ortus/lib/sandbox.sh` — `sandbox_smoke_test`, `docker_precondition_check`. Byte-equivalent extraction from `ralph.sh`'s sandbox section.
- `ortus/lib/cache.sh` — cache `mkdir` + env-var exports. Byte-equivalent extraction from `ralph.sh`'s cache section.
- `ralph.sh` refactored to source `lib/*.sh`. Behavior identical.
- `template/ortus/lib/*.sh` parity mirrors.
- `make parity` extended to assert canonical/template agreement on `lib/*.sh`.

**Dependencies**: none.

**Acceptance**: `./ortus/ralph.sh --tasks 1` against a generated project succeeds identically before and after the refactor; `git diff` of stream-json output between pre- and post-refactor runs is empty modulo timestamps.

### Phase 2: Goal-prompt and goal.sh (additive land)

**Goal**: Ship `goal.sh` and `goal-prompt.md` as opt-in alternates to `ralph.sh`/`ralph-prompt.md`. Both keep working in parallel.

**Deliverables**:

- `ortus/prompts/goal-prompt.md` — per-task body (FR-011), `/compact` ritual (FR-012), preserved Subagent Strategy / Issue Plan / Completion Comment / CodeGraph blocks (FR-013…FR-016).
- `ortus/goal.sh` — flag parsing (FR-002), `-c CONDITION` (FR-003), canonical condition (FR-004), invariant reproduction (FR-005…FR-010), logging (FR-010).
- `template/ortus/goal.sh` and `template/ortus/prompts/goal-prompt.md` parity mirrors.
- `ortus/tail.sh` updated to glob both `ralph-*.log` and `goal-*.log` (NFR-005).
- `make parity` extended to assert canonical/template agreement on `goal.sh` and `goal-prompt.md`, plus structural invariant parity between `goal.sh` and `ralph.sh` (FR-022).
- README + CLAUDE.md updated to mention `goal.sh` as the opt-in alternative; `ralph.sh` remains the default. (Documentation only; the work-execution-policy language about "All implementation work MUST go through Ralph loops" is broadened to "All implementation work MUST go through Ralph or Goal loops.")

**Dependencies**: Phase 1.

**Acceptance**: `./ortus/goal.sh --tasks 1` against the same generated project completes one bd issue end-to-end (claim → implement → verify → close → commit/push). The completion-comment block (including optional `**CodeGraph v1**`) is byte-equivalent to a `ralph.sh`-produced closure on the same issue.

### Phase 3: Parity and Measurement

**Goal**: Demonstrate that `goal.sh` meets M1–M5 on a real workload, on a real generated project, before recommending the swap.

**Deliverables**:

- `scripts/replay-queue.sh` (FR-023) — replays a fixed 20-issue queue against both orchestrators in clean copies; emits per-issue wall-clock + token counts.
- A run report (`reports/goal-vs-ralph-<date>.md`) capturing the metrics for M1, M3 against that workload.
- A 7-day continuous-use log (one or more contributors run `goal.sh` instead of `ralph.sh` on day-to-day work) capturing M2 (sentinel-class regressions) and M4 (upstream-flow autonomy after FR-018/FR-019 land).
- A go/no-go decision recorded in `reports/goal-vs-ralph-<date>.md` based on whether M1–M5 thresholds are met.

**Dependencies**: Phase 2 + Phase 4 (the upstream-flow integration is needed for M4).

**Acceptance**: All five metrics meet their thresholds. A failure to meet a threshold does not block the upstream-flow integration (Phase 4), but does block Phase 5 (deprecation of `ralph.sh`).

### Phase 4: Upstream-flow integration (the additive wins)

**Goal**: Wire `/goal` into `interview.sh` and `idea.sh --prd` so the user no longer types `/exit` to terminate.

**Deliverables**:

- `interview.sh` modified to pass `/goal` with the feature-approved condition (FR-018). The prompt's existing "advance label to approved" step becomes the natural goal-met signal.
- `idea.sh --prd` modified to pass `/goal` with the decomposition-complete condition (FR-019). `prd-decompose-prompt.md`'s final "tell the user to type /exit" instruction removed.
- `template/ortus/idea.sh` and `template/ortus/interview.sh` parity mirrors.
- README updated.

**Dependencies**: none on Phase 2/3 (this is independent); can run in parallel with Phase 2.

**Acceptance**: `./ortus/idea.sh "test idea"` and `./ortus/interview.sh <id>` both complete end-to-end without a `Ctrl+C` (exit 130) or a user-typed `/exit` in the trailing 10 invocations across 2+ contributors.

### Phase 5: Deprecation of `ralph.sh`

**Goal**: Make `goal.sh` the default; reduce `ralph.sh` to a deprecation shim.

**Deliverables**:

- `ralph.sh` becomes:
  ```bash
  #!/usr/bin/env bash
  echo "[ralph.sh] deprecated; delegating to goal.sh. See README." >&2
  exec "$(dirname "$0")/goal.sh" "$@"
  ```
- README + CLAUDE.md + AGENTS.md updated to point at `goal.sh` as primary; `ralph.sh` documented as the deprecation shim.
- LABELS.md unchanged (the schema is orthogonal).
- ZFC.md gains a one-paragraph note in "Worked example" pointing at the sentinel-parsing → `/goal` migration as a textbook ZFC win.

**Dependencies**: Phase 3 (go/no-go decision must be "go").

**Acceptance**: `./ortus/ralph.sh` continues to work end-to-end (via the shim) on a generated project; the deprecation notice fires exactly once per invocation.

---

## Epic Breakdown

### Epic E1: Foundation refactor (Phase 1)
- **Description**: Behavior-preserving extraction of sandbox/cache helpers into `ortus/lib/*.sh`.
- **Requirements Covered**: FR-005, FR-007, FR-008, FR-022 (partial).
- **Tasks**:
  - [ ] Extract `sandbox_smoke_test`/`docker_precondition_check` to `ortus/lib/sandbox.sh`; `ralph.sh` sources it.
  - [ ] Extract cache `mkdir` + env exports to `ortus/lib/cache.sh`; `ralph.sh` sources it.
  - [ ] Mirror `lib/*.sh` into `template/ortus/lib/`.
  - [ ] Extend `make parity` to cover `lib/*.sh`.
  - [ ] Smoke test: `./ortus/ralph.sh --tasks 1` against a generated project still closes one issue.

### Epic E2: `goal.sh` orchestrator (Phase 2)
- **Description**: New autonomous-execution orchestrator that runs `claude -p "/goal CONDITION"` once with all `ralph.sh` invariants reproduced.
- **Requirements Covered**: FR-001, FR-002, FR-003, FR-004, FR-005, FR-009, FR-010, FR-022 (full).
- **Tasks**:
  - [ ] Implement flag parsing, including `-c CONDITION`.
  - [ ] Implement canonical condition string per Appendix A and inject `--tasks`/`--iterations` clauses.
  - [ ] Reuse `lib/*.sh` from E1.
  - [ ] Invoke `claude -p "/goal CONDITION" --output-format stream-json --verbose --dangerously-skip-permissions [$FAST_MODE]`.
  - [ ] Tee output to `logs/goal-<timestamp>.log`.
  - [ ] Mirror to `template/ortus/goal.sh`.
  - [ ] Extend `make parity` to assert `goal.sh`/`ralph.sh` structural invariant parity (flock path, smoke-test calls, docker check, cache exports).

### Epic E3: `goal-prompt.md` (Phase 2)
- **Description**: New per-task prompt that drives the long-lived session.
- **Requirements Covered**: FR-011, FR-012, FR-013, FR-014, FR-015, FR-016, FR-017.
- **Tasks**:
  - [ ] Author per-task body (orient/select/claim/investigate/implement/verify/log/close/commit/push) from `ralph-prompt.md`, with the step-10 change per FR-011.
  - [ ] Add the `/compact` between-task ritual.
  - [ ] Preserve JSON Issue Plan schema and validator contract.
  - [ ] Preserve completion-comment format including `**CodeGraph v1**` block.
  - [ ] Preserve Subagent Strategy table verbatim.
  - [ ] Preserve CodeGraph integration steps (1.5, 4, 6.5, 7, 7.5).
  - [ ] Retain `<promise>BLOCKED</promise>` as transcript marker (no shell parser depends on it).
  - [ ] Mirror to `template/ortus/prompts/goal-prompt.md`.

### Epic E4: Upstream-flow `/goal` integration (Phase 4)
- **Description**: Wire `/goal` into `interview.sh` and `idea.sh --prd` so the user no longer manually exits.
- **Requirements Covered**: FR-018, FR-019, M4.
- **Tasks**:
  - [ ] Modify `interview.sh` to pass `/goal "feature {{FEATURE_ID}} has label approved and at least one child task exists"` to `claude`.
  - [ ] Modify `idea.sh --prd` to pass `/goal "every work item in {{PRD_PATH}} is reflected by a bd issue with type and dependencies set per the PRD's sequence"` to `claude`.
  - [ ] Remove "tell the user to type /exit to continue" from `prd-decompose-prompt.md`.
  - [ ] Mirror to `template/ortus/idea.sh`, `template/ortus/interview.sh`, `template/ortus/prompts/prd-decompose-prompt.md`.
  - [ ] Update README's "Step 2: Kickstart Your Feature" section to drop the implied "you must press /exit."

### Epic E5: Replay harness and measurement (Phase 3)
- **Description**: Build the replay harness and produce the go/no-go report.
- **Requirements Covered**: FR-023, M1, M2, M3, M5.
- **Tasks**:
  - [ ] `scripts/replay-queue.sh` — clones a generated project, seeds a fixed 20-issue queue, runs both orchestrators, captures wall-clock and `usage.input_tokens` from stream-json.
  - [ ] `reports/goal-vs-ralph-<date>.md` — M1/M3 metrics.
  - [ ] 7-day continuous-use protocol — at least one contributor uses `goal.sh` exclusively; log captured for M2/M4.
  - [ ] Go/no-go decision recorded.

### Epic E6: Deprecation (Phase 5)
- **Description**: Make `goal.sh` the default; reduce `ralph.sh` to a shim.
- **Requirements Covered**: FR-021.
- **Tasks**:
  - [ ] Replace `ralph.sh` body with the shim.
  - [ ] Update README, CLAUDE.md, AGENTS.md to point at `goal.sh`.
  - [ ] Add a paragraph to ZFC.md's "Worked example" pointing at this migration as a ZFC win.

---

## Open Questions

- **Q1 — *Withdrawn in 2026-05-16 (b) revision.*** *(Was: `--resume` × shared dolt server ownership. Moot since embedded mode eliminated the shared-server lifecycle.)*
- **Q2 — Should the canonical condition include "and the working tree is clean and pushed"?** Pros: matches the AGENTS.md "session completion" checklist. Cons: makes the condition harder for Haiku to judge from transcript (push success is in the transcript only if the model logged it). Recommend: yes, but worded as "the latest turn surfaced a successful `git push` (or confirmed no remote is configured)." Validate phrasing in Phase 2 dry-runs.
- **Q3 — Cost of running Haiku evaluator on every turn for a long queue.** The `/goal` docs say evaluator cost is "typically negligible compared to main-turn spend." For a 100-issue queue at ~5 turns per issue, that is ~500 evaluator calls — still cheap, but quantify in Phase 3's replay harness so we have numbers, not handwaves.
- **Q4 — `disableAllHooks` / `allowManagedHooksOnly` gotchas.** Some downstream Ortus users may have managed settings that disable hooks; `/goal` won't run there. We should detect this case in `goal.sh` and (a) print a friendly error referencing the docs, (b) suggest falling back to `ralph.sh --legacy` (the shim could grow a `--legacy` flag during Phase 5 that bypasses the `goal.sh` exec). Decide in Phase 5.
- **Q5 — Should the upstream-flow `/goal` conditions live in the prompts or in the shell scripts?** Per LABELS.md the label vocabulary is a schema; per ZFC.md prompt text in shell scripts is a drift risk. Recommend: condition strings live in `ortus/prompts/conditions/*.txt` (one file per condition), and shell scripts read them. Decide before Phase 4 lands.
- **Q6 — Does `/goal` work under `--docker` Tier-2 sandbox?** The docker subcommand is `docker sandbox run claude --name ortus-ralph --` today. The `/goal` command is a slash command; passing it via `-p "/goal ..."` should work identically inside the docker image because `/goal` is a Claude Code feature, not a flag. Validate in Phase 2 dry-run on a CI runner with docker.
- **Q7 — Backpressure when the evaluator says "no" repeatedly.** Today, a failing test forces the model to re-iterate within a single subprocess; the failure is in-context. Under `/goal`, the model's "I'm still working on this task" turn ends, the evaluator says "no, queue isn't empty yet" with a reason, and the next turn starts. The reason becomes guidance — useful — but the within-turn backpressure (test fails, fix immediately) still lives entirely in the per-task body of `goal-prompt.md`. Confirm in Phase 2 that this layering does not cause turn-thrashing where the model declares done, the evaluator disagrees, and the next turn redoes work.

---

## Out of Scope

- Cloud routines / scheduled remote agents (the `/schedule` family). Complementary; orthogonal to `/goal`. A future PRD may wrap `goal.sh` invocations in a routine, but this PRD does not.
- Replacing `ralph-prompt.md` with `goal-prompt.md` outright. The legacy prompt stays for the deprecation window so `ralph.sh` (which still execs at the start of Phase 1–4) keeps working.
- Restructuring `.beads/` directory layout or bd's `sandbox.excludedCommands` contract. Out of scope per q3. (The shared-dolt lifecycle was originally listed here; it's no longer present to restructure.)
- Changing the JSON Issue Plan schema, the completion-comment format, or the `**CodeGraph v1**` block schema. Out of scope; the schemas are durable cross-iteration memory and changing them would break parity with closed issues.
- Multi-tenant `goal.sh` (multiple concurrent runs on the same repo via separate locks). Out of scope; the flock pattern matches `ralph.sh` exactly to keep one autonomous loop per workspace and avoid bd embedded-DB write contention.
- A web UI / status dashboard for `/goal`'s "active" indicator. The CLI's built-in `◎ /goal active` is sufficient.
- Cross-session memory ("yesterday's `goal.sh` session learned X; today's should remember"). Out of scope and likely undesirable — Ortus's durable cross-task memory is bd comments, not the model's recall.
- Migrating away from `--dangerously-skip-permissions` toward managed `auto mode` configuration. Worth a future PRD, but orthogonal to `/goal`. Note from the auto-mode docs: auto mode is Max/Team/Enterprise/API only, not Pro — so a migration would need to address the tier gap before it could land in the generic Copier template.

---

## Appendix

### Appendix A: Canonical Condition (FR-004)

This is the literal string `goal.sh` passes when no `-c CONDITION` is given (with `<NTASKS>` and `<NITERS>` substituted from `--tasks` and `--iterations` respectively; empty clauses dropped when the flag is absent):

```text
Drive the bd queue to zero. You are done when ALL of the following hold:

(1) the most recent `bd ready --json` invocation surfaced in this conversation returned an empty array `[]`; AND
(2) no issue is in the `in_progress` status (verify with `bd list --status in_progress --json` if not already surfaced this turn); AND
(3) the latest turn surfaced either a successful `git push` (when a remote is configured) or a transcript-visible confirmation that no remote is configured (`git remote` empty).

You may stop early if EITHER:
(a) you have closed <NTASKS> issues in this session (count only `bd close` calls that returned success), OR
(b) you have used <NITERS> turns since this goal was set.

When this condition is met, end the session. Do not start another task. Do not output a sentinel; the evaluator reads this transcript.
```

Notes:
- Each predicate references information that the main session surfaces to the transcript through normal tool calls. The evaluator cannot call tools — it can only judge from the conversation. The condition is written so a Haiku reader can answer yes/no from the transcript alone.
- The 4000-character limit is well in hand (≈ 800 chars at full substitution).
- The "do not output a sentinel" instruction prevents the model from emitting `<promise>EMPTY</promise>` out of muscle memory and being treated as a control signal anywhere.

### Appendix B: Glossary

- **Backpressure** — Per Ghuntley: downstream signals (tests, lint, builds) that reject invalid work and force iteration. Per Ortus: the model fixes failures within the per-task body and only closes when verification passes.
- **bd / beads** — local-first issue tracker; v1.0.0+ required; backed by Dolt embedded mode by default since v1.0.3 (no sql-server, no PID/port files).
- **CodeGraph** — optional MCP-indexed semantic graph; Ortus integrates via `codegraph_*` tools when `.codegraph/` exists.
- **Dumb pipe** (per ZFC.md) — code that does pure I/O, schema validation, or lifecycle tracking, with no inferred meaning from unstructured input.
- **flock(1)** — the `flock` *binary* (not the syscall); used by `ralph.sh` to scope the lock to a process that does not leak the FD to children.
- **Fresh-per-iter** — Ralph's defining property: each iteration is a new `claude -p` subprocess with empty context. `/goal` does not provide this natively; FR-012's `/compact` ritual approximates it.
- **`/goal` evaluator** — the Claude Code small-fast-model (Haiku by default) that judges the active goal's condition after every turn.
- **Sentinel** — the literal `<promise>X</promise>` strings (`EMPTY`/`COMPLETE`/`BLOCKED`) that `ralph.sh` greps for to decide control flow. Phase 5 retires the first two; `BLOCKED` survives as a transcript marker.
- **Smart zone** — Ortus's 40-60% main-context utilization band; past 60% quality degrades, past 80% the loop is in trouble.
- **ZFC** — Zero-Framework Cognition; reasoning lives in the model, plumbing is mechanical (see `ZFC.md`).

### Appendix C: Reference Links

- `/goal` directive — https://code.claude.com/docs/en/goal
- `/loop` and scheduling comparison — https://code.claude.com/docs/en/scheduled-tasks
- Prompt-based Stop hooks — https://code.claude.com/docs/en/hooks-guide#prompt-based-hooks
- Auto-mode configuration — https://code.claude.com/docs/en/auto-mode-config
- Ralph Wiggum manifesto — https://github.com/ghuntley/how-to-ralph-wiggum
- ralph-beads PoC — https://github.com/who/ralph-beads
- Ortus repo — `~/code/ortus`
- Ortus canonical Ralph orchestrator — `~/code/ortus/ortus/ralph.sh`
- Ortus canonical Ralph prompt — `~/code/ortus/ortus/prompts/ralph-prompt.md`
- Ortus ZFC rubric — `~/code/ortus/ZFC.md`
- Ortus label state machine — `~/code/ortus/LABELS.md`
- beads upstream — `~/code/beads-v1.0.4`

### Appendix D: Interview Notes Summary

Three direct decisions captured at the start of this PRD (q1/q2/q3 in the elicitation):

1. **Scope** — Replace `ralph.sh`. Aggressively migrate the main loop to a single long-lived `/goal` session. (Not "augment," not "hybrid," not "lay it out and let me choose.")
2. **Depth** — Full Ortus-style PRD (matching the shape of `prd/PRD-zero-framework-cognition.md` referenced from `ZFC.md`).
3. **Risk** — Strictly additive on the load-bearing invariants. Nothing the PRD proposes is allowed to touch `ralph.sh`'s flock guard, sandbox `excludedCommands`, or fresh-per-iter semantics. `/goal` lands only in new or non-Ralph code paths. *(q3's original list included the shared dolt sql-server lifecycle; that invariant was retired in the 2026-05-16 (b) revision when bd embedded mode became the default and the orchestration was removed from `ralph.sh`. The user reaffirmed the rip-out explicitly: "I just want it to simply frickin' work.")*

The architectural reconciliation of (1) and (3) — replace the orchestrator while preserving every surviving invariant — is the central design constraint of this PRD and is enforced by FR-005, FR-007, FR-008, FR-009, FR-010 (invariant preservation; FR-006 reserved), FR-022 (parity test), and the `/compact` between-task ritual (FR-012) that approximates fresh-per-iter inside a long-lived session.
