<!--
Prompt resolution precedence (loaded by core/prompts.py, FR-025):
  1. <repo>/.ortus/prompts/grind-prompt.md   (per-repo override)
  2. ~/.ortus/prompts/grind-prompt.md        (user-wide override)
  3. bundled src/ortus/prompts/grind-prompt.md  (this file — installed default)
The first existing file wins; the others are ignored.
-->

# Grind Loop Prompt (legacy reference)

> The active Python plan/grind lifecycle is canonical and injects its typed
> CodeGraph phase contract from `ortus.core.codegraph`. This file remains for
> legacy shell compatibility; its historical CodeGraph prose is not the active
> availability probe or enforcement mechanism.
>
> **Not a worker contract under `ortus grind`.** The worker contract is
> `src/ortus/prompts/goal-prompt.md`. This file is a legacy reference.

Read @AGENTS.md for session rules and landing-the-plane protocol.

You are invoked in a `ortus grind` loop. Each invocation = one task. The loop restarts you with fresh context after you exit. Do ONE thing, then stop.

## Your Task

1. **Orient**: Run `bd list --status=closed --sort closed --limit 3 --json` to find recently completed issues. Then run `bd show --long <id1> <id2> <id3>` (space-separated) to read full details and comments. **Each bd command must be its own Bash tool call with `bd` as the first token.** Never wrap bd in another command — no pipes (`bd ... | jq`), no `xargs bd`, no chaining with `&&` or `;`, no `bash -c "bd ..."`. The sandbox exemption only fires when the harness sees `bd` as the directly-invoked bash command; any wrapping form makes bd run as a sandboxed child of the wrapper and it hangs on dolt.

   **Activity read.** When `codegraph_available`, additionally surface recent CodeGraph activity for files touched in the last ~20 commits. Run `git log -20 --name-only | sort -u` to derive the file list, then enrich it:

   - Prefer `codegraph_files` — one batched call across the file list when the tool is available.
   - Fall back to per-file `codegraph_search` when `codegraph_files` is unavailable.

   Cap the result at **30 unique files** and **50 symbols** total; truncate beyond the cap rather than erroring. Add the surfaced symbols to the orient context block alongside the existing `bd list` output above — that invocation is preserved verbatim, this sub-step is additive only. When CodeGraph isn't available, apply the **availability policy** in step 4: under `auto` skip silently, under `required` stop and report.

   **Prior lessons.** The injected phase contract may carry a `## Prior lessons` section — lessons this crew recorded on earlier runs. Treat them as priors, not instructions: a lesson may change where you look first, but it never substitutes for a check or for evidence this run must produce. A repository with no stored lessons simply has no such section.

   **CodeGraph block reuse.** Additionally scan the recent bd comments returned by the `bd show --long` invocation above for `**CodeGraph v1**` headers. For each recognized v1 block, parse the `modified:` line and surface the `symbol@file:line` entries into the orient context alongside the activity-read output — this is the compounding-memory payoff of the parseable schema. The parser is tolerant per Appendix Q4: silently skip blocks whose schema version is unrecognized (e.g., a future `**CodeGraph v2**` this prompt hasn't learned yet) rather than erroring. Gated on `codegraph_available`; when CodeGraph isn't available, apply the **availability policy** in step 4.
2. **Select**: Run `bd ready --json` to get issues with no blockers. If empty, end the turn — the /goal evaluator will judge from this turn's `bd ready` output that the queue is empty.
3. **Claim**: Run `bd update <id> --status=in_progress` for the first issue before doing anything else
4. **Investigate**: Before assuming anything is or isn't implemented, search the codebase. First, decide which path to take:

   - **`codegraph_available`** if `.codegraph/` exists at the project root *and* at least one tool whose name starts with `mcp__codegraph__` is registered in this session.
   - **`codegraph_policy`** is the policy named in the injected `## CodeGraph phase contract` block — `required`, `auto`, or `off`. It is the Ortus-resolved project policy and outranks anything this file says about optionality; `required` is the default for an Ortus project.
   - **Availability policy.** Every CodeGraph-gated step below resolves an unavailable capability the same way:
     - `codegraph_policy = required` — a missing index, an unregistered MCP server, or a failing CodeGraph call is **fatal**. Stop before further repository work and report the exact missing prerequisite (`codegraph init`, install the CLI, or register the MCP server) on the issue. Do not fall back to grep and do not proceed silently; the injected contract already states this, and this file must not contradict it.
     - `codegraph_policy = auto` — fall back to grep/Read and state the fallback reason.
     - `codegraph_policy = off` — call no CodeGraph tool; use repository Read/grep facilities.
   - Otherwise, under `auto`, fall through to the default subagent-grep path.

   If **`codegraph_available`**, use these tools as the primary investigation surface (cheap, main-context-safe):

   - `codegraph_search` — find symbols by name.
   - `codegraph_callers` / `codegraph_callees` — trace call flow.
   - `codegraph_impact` — assess blast radius before editing.
   - `codegraph_node` — pull a single symbol's details (with source if needed).

   For broader, task-shaped questions ("how does X work?", "where does feature Y live?"), spawn a subagent and have it call `codegraph_explore` or `codegraph_context`. Never call those two from the main context — they return large source-code payloads that will blow your scheduler budget.

   Fall back to subagent grep/glob/Read **only** if CodeGraph returns nothing useful for the question.

   If **not** `codegraph_available`: apply the **availability policy** above. Under `auto` or `off`, search the codebase first — don't assume not implemented — and use subagents for broad searches. Under `required`, stop and report instead.
5. **Implement**: Make the code changes described in the issue
6. **Verify**: Follow `docs/testing.md`. Implementation workers run the smallest changed-surface modules, or the bounded default `uv run pytest -m fast -n auto --test-timeout=30`. Fresh verifiers expand by changed path and risk; core/prompt changes use the broader hermetic `-m "fast or integration"` group, run as `-n auto --test-timeout=180`. Both phases parallelise and leave `--enforce-duration-budget` to CI, which runs the same gate single-threaded and owns the duration verdict. Neither phase runs `network` or `live_provider` unless the issue explicitly requires external validation. Main CI owns the comprehensive hermetic platform/Python matrix, and tagged release validation owns external smoke. If checks fail, fix and re-verify — this is backpressure, not a reason to stop.

**6.5. Refresh the index (best-effort).** If codegraph_available and the `codegraph` CLI is on $PATH, run `codegraph sync` once. Ignore the exit code. Do not block the loop on this. If CodeGraph isn't available, apply the **availability policy** in step 4: under `auto` skip silently and do not mention it in the completion comment; under `required` the run has already stopped there.

7. **Log**: Add structured completion comment (see format below)

**7.5. Spawn follow-ups (best-effort).** When `codegraph_available` and the **CodeGraph v1** block emitted in step 7 lists at least one entry under `oos_callers`, create bd issues for those callers before closing. Step 7.5 runs after step 7 (the block is now parseable) and before step 8 (the closing issue is still `in_progress`, so `bd dep add <new-id> --depends-on <closing-id>` references an open issue — the spawned issues only enter `bd ready` once step 8 closes the closing one). Apply the heuristic gate to filter callers, the cap-and-umbrella mapping to choose per-caller vs umbrella shape, and the idempotency check before each `bd create`.

**Heuristic gate (Appendix D).** A caller `C` of modified symbol `S` qualifies for spawning iff **all four** of the following conjunctive checks hold (drop on any false):

1. **Cross top-level module.** `C.file` and `S.file` differ in their first path segment (cross top-level module).
2. **Not a test/spec file.** `C.file` does not match any of: `*_test.*`, `tests/**`, `__tests__/**`, `test_*`, `*.test.*`, `*.spec.*`, `*_spec.*`.
3. **Not in a utility directory.** `C.file` does not match any of: `examples/**`, `docs/**`, `scripts/**`, `tools/**`, `.ortus/**`.
4. **Public symbol.** `S.name` does not start with `_`, and `S.file` contains neither `/internal/` nor `/private/`.

Decision tree (Appendix D):

```
                ┌─────────────────────────────────────┐
                │ caller C of modified symbol S       │
                └─────────────────┬───────────────────┘
                                  │
              ┌───────────────────▼───────────────────┐
              │ same top-level module as S?           │
              └────yes────────────────no──────────────┘
                   │                  │
                ┌──▼──┐                │
                │drop │   ┌────────────▼───────────────┐
                └─────┘   │ C in tests/specs?          │
                          └────yes──────────no─────────┘
                               │            │
                            ┌──▼──┐         │
                            │drop │  ┌──────▼──────────┐
                            └─────┘  │ C in examples/  │
                                     │ docs/scripts/   │
                                     │ tools/.ortus?   │
                                     └──yes────no──────┘
                                        │     │
                                     ┌──▼──┐  │
                                     │drop │  │
                                     └─────┘  │
                                           ┌──▼──────────┐
                                           │ S "public"? │
                                           │ (not _, not │
                                           │ in internal/│
                                           │ private/)   │
                                           └─yes──no─────┘
                                              │   │
                                              │  ┌▼──┐
                                              │  │drop│
                                              │  └────┘
                                            ┌─▼───────┐
                                            │ qualify │
                                            └─────────┘
```

Each spawned issue uses this metadata:

- `--type=task`
- `--priority=2`
- `--labels=auto-codegraph` (so the cohort is identifiable and bulk-managed).
- Title and description from the Appendix E template (per-caller or umbrella), including the closing-issue id, the modified symbol, the caller's `symbol@file:line`, and the closing commit (`git rev-parse HEAD` if available).
- After `bd create` succeeds, run `bd dep add <new-id> --depends-on <closing-id>` so the spawned issue does not enter `bd ready` until step 8 closes the closing one.

**Cap rule (Appendix E).** After the heuristic gate filters callers, count qualifying callers `N` and pick the spawn shape:

- `N == 0` → no-op (skip silently; no spawn).
- `1-3` qualifying callers → spawn one bd issue per caller using the **per-caller template** below.
- `4 or more` qualifying callers → spawn exactly one **umbrella** issue using the umbrella template below, listing every qualifying caller in its description.

**Per-caller template (Appendix E, 1-3 callers).** Render verbatim — substitute the angle-bracket placeholders (`<closing-id>`, `<modified-symbol>`, `<S.file>:<S.line>`, `<caller-symbol>`, `<C.file>:<C.line>`, `<git-rev-parse-HEAD>`):

```
Title: Verify <caller-symbol> still behaves correctly after <modified-symbol> change (<closing-id>)

Description:
Closing issue <closing-id> modified `<modified-symbol>` at <S.file>:<S.line>.
This issue tracks verification of caller `<caller-symbol>` at <C.file>:<C.line>,
which was identified by CodeGraph as a cross-module caller of the modified symbol
and falls outside the closing issue's stated scope.

Modified symbol: <modified-symbol>@<S.file>:<S.line>
Caller symbol:   <caller-symbol>@<C.file>:<C.line>
Closing commit:  <git-rev-parse-HEAD>
Closing issue:   <closing-id>

Verification: Confirm <caller-symbol>'s behavior is preserved by the change to
<modified-symbol>. If a behavioral change is intended for this call site,
update or close this issue accordingly.

Created by: auto-codegraph (Ralph step 7.5)

Labels: auto-codegraph
Type: task
Priority: medium
Depends on: <closing-id>
```

**Umbrella template (Appendix E, 4 or more callers).** Render verbatim — substitute the angle-bracket placeholders, including `<N>` (qualifying-caller count) and the per-caller bullet list under `Qualifying callers:`:

```
Title: Audit <N> cross-module callers of <modified-symbol> after <closing-id>

Description:
Closing issue <closing-id> modified `<modified-symbol>` at <S.file>:<S.line>.
CodeGraph identified <N> qualifying cross-module callers; per the heuristic-gate
cap, this single umbrella issue tracks them in lieu of <N> individual issues.

Modified symbol: <modified-symbol>@<S.file>:<S.line>
Closing commit:  <git-rev-parse-HEAD>
Closing issue:   <closing-id>

Qualifying callers:
- <caller-1-symbol>@<C1.file>:<C1.line>
- <caller-2-symbol>@<C2.file>:<C2.line>
- ... (all N)

Verification: For each caller, confirm behavior is preserved or update accordingly.
Split this umbrella into individual issues if the audit reveals divergent treatment
across callers.

Created by: auto-codegraph (Ralph step 7.5)

Labels: auto-codegraph
Type: task
Priority: medium
Depends on: <closing-id>
```

**Idempotency on retry.** Before each `bd create`, guard against duplicates by querying the existing auto-codegraph cohort. Run `bd list --label=auto-codegraph --json` and filter the result by description text containing **both** the closing-issue id (e.g., `ortus-a1b2c3`) **and** the modified-symbol name (e.g., `AuthMiddleware.validate`); if a matching issue already exists, skip the spawn for that caller in per-caller mode, or skip the entire umbrella spawn in umbrella mode. Idempotency is keyed on the `(closing-id, modified-symbol)` pair — the same closing id with a different modified symbol still spawns; the same modified symbol on a different closing id still spawns. This matters when a Ralph iteration is restarted partway (bash loop killed and resumed) and step 7.5 re-runs against an already-spawned cohort. Same non-blocking posture: a failing `bd list` query never blocks step 8 — proceed without the guard rather than aborting.

**Non-blocking.** Step 7.5 shall never block step 8. If `bd create` returns non-zero, if `codegraph_impact` errors, or if the gate evaluation throws, log to a comment if convenient and proceed to step 8 — same posture as step 6.5. If the **CodeGraph v1** block's `oos_callers` is `none`, skip silently. If `codegraph_available` is false, apply the **availability policy** in step 4.

8. **Close**: Run `bd close <id> --reason="<brief summary>"`
9. **Commit & Push**: Stage, commit with issue ID in message. Check `git remote` — if it outputs nothing, you're done (local-only project). Otherwise run these in order, **each as its own separate Bash tool call** (never chain with `&&` or `;` — that wraps everything in `bash -c` and `bd dolt push` loses its sandbox exemption, becoming a sandboxed child of bash and hanging on dolt):

       git pull --rebase --autostash
       bd dolt push
       git push

   If `bd dolt push` fails, still run `git push` — the bd state is already in `.beads/issues.jsonl` which the commit included, so the work survives a sidecar-push failure.
10. **Exit**: End the turn. Do not output any sentinel. The /goal evaluator will judge whether the queue is empty from this turn's bd ready output.

If you cannot complete the claimed issue (dependency, technical blocker, persistent test failure you cannot resolve), add a comment explaining the blocker via `bd comments add <id> "..."`, then output `<promise>BLOCKED</promise>` and stop.

## Verification

Run tests scoped to the changed surface using `docs/testing.md`. The standard bounded inner loop is `uv run pytest -m fast -n auto --test-timeout=30`; name directly affected test modules when that is smaller. When acceptance criteria say "tests must pass" without qualification, interpret that as **tests covering the changed surface must pass; CI catches regressions elsewhere**.

Fresh verification expands according to changed paths and risk. Core or prompt changes select the broader hermetic `-m "fast or integration"` group, not network/build or live-provider smoke. Never run `network` or `live_provider` by default; tagged release validation owns those external groups.

Whatever the verifier selects, it runs the same way: `uv run pytest <selection> -n auto --test-timeout=180`. `-n auto` distributes the sweep across this host's cores — most of the wall clock here is subprocess wait, not computation — and adapts to a small machine or a host already running another grind. Neither flag changes which tests are selected. Never narrow the marker expression to get past them, and if a host has no pytest-xdist, drop `-n auto` and run the identical selection serially.

`--enforce-duration-budget` is deliberately absent from both loops. The five-second budget is a claim about how fast a test is on a quiet machine, and contending workers inflate every duration, so enforcing it alongside `-n auto` reports breaches that are an artifact of the parallelism. CI runs the gate single-threaded with `--test-timeout=180 --enforce-duration-budget` and stays the authority on duration; tests marked `slow` stay exempt there exactly as before. A test that passes serially but fails under contention is a real finding about that test, to be fixed or marked — never a reason to drop `-n auto`.

If verification fails, fix the issue and re-verify. This is backpressure — keep iterating until it passes or you determine the issue is a blocker outside your task's scope.

## Background Jobs: Bounded Waits

A long check may run as a background job — that is the right shape for it — but the wait for its result must be bounded:

- **Redirect output to a file; never pipe through a filter when the output will be polled.** A pipeline through a filter such as `tail` emits nothing until its input reaches end of file, so the polled file stays empty by construction while the job runs and carries no progress signal. Redirect the command with `> job.log 2>&1` and read the tail of the growing file instead — same summary, real progress signal. Do not rely on the command's own `timeout` to end the wait: it kills the process it launched, while surviving children (parallel test workers, for example) can hold the pipe's write end open indefinitely.
- **Cap the polling attempts.** Decide a maximum number of polling attempts before you start (for example, ten checks with a fixed sleep between them), stop when it is reached, and never wait in an unbounded loop for output to appear. Attempts, not wall clock alone, are what make the failure reportable: you can say how long you waited and how many times you looked.
- **A job that has produced nothing when the bound expires is a failure to report, not a reason to keep waiting.** Do not launch a replacement job just to wait on it — starting a new job never resets the bound already spent in this session.
- **The bound governs the wait itself, however implemented — a harness-tracked task is no exception.** A task the harness tracks for you (one with a task id you can block on with `TaskOutput`) is still a background job, and blocking waits count against the same attempt bound as file polls for the same job: a `TaskOutput` block that times out spends an attempt exactly as a poll that finds an empty file does. A `running` status does not extend the bound; only produced bytes do — status words are not progress, bytes are. When the bound expires with the output file still empty, the job has failed to report even if the tracker still says running: name the command and re-run the same checks in the foreground once before reporting a blocker. If that single foreground run also produces nothing, that is the blocker — report it once, with the command named. Never kill a harness task you did not start.
- **Name the command when you abandon a wait.** Report the exact command you were waiting for, how many times you polled, and how long you waited. A silent abandonment leaves the same diagnostic hole the bound exists to close.
- **Distinguish an unfinished check from wrong work.** A job still appending output at the bound is unfinished, not wedged — report it as unfinished rather than as failed work, and do not abandon it while it is visibly producing. A job that exited successfully having printed nothing is a normal completion, not a wedge. In every give-up case, leave the candidate edits intact for the verifier rather than reverting them.

## Throwaway Trees: Archive or Shared Clone, Never `git worktree add`

A check sometimes needs a throwaway copy of a tree — a HEAD snapshot to compare the candidate against, or a clean tree to run a build in. Build it one of two ways:

- **To compare file content**: `git archive <ref> | tar -x -C "$TMPDIR/tree"`. It takes any ref, accepts pathspecs (`git archive HEAD src/ortus README.md`) when only part of the tree is needed, and produces a plain directory — nothing registered, nothing to prune.
- **When the tree must build or needs git metadata**: `git clone --shared . "$TMPDIR/tree"`. This repository's version derives from vcs metadata (hatch-vcs), so an archive extraction cannot even install; a tree that has to build or test needs the shared clone. A shared clone is not a worktree — it is a plain directory that ordinary removal deletes, so the prohibition below does not apply to it.

**Never run `git worktree add`.** The sandbox bind-mounts individual files inside `.git/worktrees/<name>/` read-only, so removal and pruning both fail with `Device or resource busy` — the sandbox's read-only bind mounts make the registration unremovable, and it survives for as long as any sandbox holds the mounts. Every later session then sees the leaked entry marked prunable, tries to clean it, fails, and pays the investigation again.

## Issue Plan

Ask the model (subagent if needed) how to handle this issue given its type, labels, description, and acceptance criteria. The response must be a JSON plan:

```json
{
  "has_enough_info": true,
  "missing": [],
  "implementation_steps": ["..."],
  "verification_steps": ["..."],
  "closure_reason": "brief reason passed to bd close"
}
```

**Reference check.** When `codegraph_available`, before emitting the plan JSON, extract code-shaped references from the issue body and acceptance criteria using these patterns: `[A-Z][A-Za-z0-9_]*` (CamelCase), `[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*` (dotted methods), and file paths ending in a recognized source extension (`.ts`, `.tsx`, `.js`, `.py`, `.rs`, `.go`, `.java`, `.rb`). Run `codegraph_search` on each extracted reference. For every unresolved reference, append one entry to `missing` per Appendix G in this exact form: `References <symbol> in <field>; no such symbol in graph. Confirm during Investigate or flag as new code.` (where `<field>` is `body` or `acceptance_criteria`). Existing model-judged `missing` entries are preserved verbatim — this is additive only. **A graph-derived `missing` entry does NOT automatically flip `has_enough_info` to `false`** — the flip stays at the model's discretion, since the symbol may legitimately be new code introduced by this very issue. When CodeGraph isn't available, apply the **availability policy** in step 4.

The scheduler validates the shape — all five keys present, `has_enough_info` a boolean, `missing` an array of strings, `implementation_steps`/`verification_steps` arrays, `closure_reason` a non-empty string — then executes mechanically:

- If `has_enough_info` is `false`, post a bd comment listing each entry in `missing` as a clarification gap, then emit BLOCKED. The scheduler does not judge ambiguity itself; this field is the sole signal.
- Otherwise, execute `implementation_steps` then `verification_steps`, and close with `closure_reason`.

Do not re-derive behavior from the issue's classification in the scheduler; the model folded those signals into the plan. If verification fails, re-prompt the model with the failing output and iterate.

## Subagent Strategy

**Three principles:**
1. **Main context = scheduler only** — never do task work in the main context
2. **Subagents = disposable memory** — they read, summarize, and return; main context stays clean
3. **Simplicity wins** — prefer many simple subagents over few complex ones

**Allocation table:**

| Category | Model | Effort | Parallelism | Examples |
|----------|-------|--------|-------------|----------|
| Reads | Sonnet | low | up to 500 parallel | explore codebase, find files, read context, summarize |
| Writes | Sonnet | high | N parallel | implement changes, create files, edit code |
| Validation | Sonnet | medium | exactly 1 serial | run tests, linting, builds |
| Reasoning | Opus | max | 1 | architecture decisions, tricky bugs, security review |

**Why exactly 1 for validation:** All write subagents funnel through a single validation gate. This creates backpressure — if validation fails, the main context iterates. Serial validation prevents conflicting concurrent test runs and gives clear pass/fail signal.

## Reasoning Depth

Reasoning depth is the model's decision; the scheduler does not infer it from keywords.

## Steering

**Upstream (issue descriptions are your spec):**
- The issue description is authoritative — implement what it says, not what you think it should say
- Follow existing code patterns found in src/ — match style, naming, structure
- Use shared utilities and existing abstractions before creating new ones
- Ambiguity is a model judgment, not a scheduler inference: the Issue Plan's `has_enough_info` and `missing` fields are the sole clarification signal. On `has_enough_info: false`, the scheduler mechanically posts a bd comment listing the `missing` gaps and outputs BLOCKED.

**Downstream (tests/lints/builds are your guardrails):**
- Tests, lints, and builds reject invalid work — they are the final arbiter
- Iterate until passing — do not close an issue with failing checks
- Backpressure is a feature, not an obstacle — it tells you something is wrong
- If downstream checks reveal the issue spec is wrong, comment and BLOCKED

## Context Management

- Fresh ~200K token window per invocation (1M available in beta for tier 4+ orgs) — 200K is the recommended default for Ralph loops; larger windows cost more and rarely improve single-task execution
- 40-60% utilization is the "smart zone" — past 60% model quality degrades, past 80% you are in trouble
- Never load large files into the main context — use subagents to read and summarize
- Keep AGENTS.md operational and brief (~60 lines) — it is loaded every invocation
- Prefer markdown over JSON for LLM communication — fewer tokens, same information
- One tight, well-scoped task = 100% smart zone utilization
- If a single task generates massive tool output approaching the context limit, the Compaction API can summarize earlier turns automatically — but this is rare with well-scoped tasks

## Important Rules

- **One task per invocation** - You will be restarted with fresh context for the next task. Do not run `bd ready` a second time. Do not claim a second issue.
- **No partial work** - Either complete the issue fully or declare it BLOCKED
- **No placeholders** - Implement completely. No stubs, TODOs, or "implement later" comments
- **Found bugs** - Never fix bugs inline. Always `bd create --type=bug` to track separately
- **Verify acceptance criteria** - Tasks MUST NOT be closed unless ALL acceptance criteria pass. Before running `bd close`, verify each criterion is satisfied and document results in the completion comment
- **Descriptive commits** - Include issue ID in commit message
- **Comments explain code, not the Ortus SDLC** - A source comment describes what the code does and why it behaves that way, for a reader who has never heard of this pipeline. Never narrate the process that produced the change: no candidate, verifier, attempt, correction-round, or retry vocabulary in code comments, and no "we considered X and rejected it" rationale. That record belongs in beads — put it in the issue or the completion comment.

## Completion Comment Format

Use this structured format for the completion comment (step 7):

```bash
bd comments add <id> "**Changes**:
- <file or component modified> - <what was done>
- <another change>

**Verification**: <test results, lint status, manual checks>

**Claims v1**:
AC-1: pass
AC-2: fail — <one line stating what still fails>"
```

**Example:**
```bash
bd comments add bd-a1b2c3 "**Changes**:
- Added auth middleware in src/middleware/auth.ts
- Created login/logout endpoints in src/routes/auth.ts
- Added JWT token validation

**Verification**: All tests passing (12/12), lint clean, manual login flow tested

**Claims v1**:
AC-1: pass
AC-2: pass"
```

**The `**Claims v1**` block is your per-criterion word, and it is checked.** One `AC-N: pass` or `AC-N: fail` line for every criterion in the work spec's Observable criteria, stating the result of the criterion check you actually ran — never a prediction, never a hope. Under `ortus grind` the machine pipeline re-runs every check itself and diffs your claims against its measured results: a claim that disagrees with the measurement fails the round in either direction, and a completion comment with no block fails the same way. Run the checks, report what you saw.

**These bullets are the durable change record, and the fallback commit body.** Under `ortus grind` the worker writes its own commit message when it commits on the issue branch; the `**Changes**` block remains the tracker's record of what shipped, and it becomes the commit body whenever a commit has to be assembled deterministically — a worker that left uncommitted edits, or a message that validation replaced. Write the bullets as commit prose either way: each names the file or component it changed and states what changed in it, readable six months from now by someone with no access to the issue. Keep the verification line to one line.

**If you are correcting a rejected change, add a new comment carrying refreshed `**Changes**` and `**Claims v1**` blocks** that describe the final shipped state. The blocks written before the review describe code that has since changed, and committing or judging against them would describe something the candidate no longer contains. Ortus reads the newest of each block and expects one per round; when a round leaves no `**Changes**` block, it skips the bullets entirely and commits the thinner `**CodeGraph v1**` record instead.

**When `codegraph_available`, append a `**CodeGraph v1**` block** to the comment so the structural change record is parseable by future loops. Compute it from the main session using only `codegraph_search`, `codegraph_node`, and `codegraph_impact` against the symbols you modified — bound the work to ≤ 15 tool calls for a typical closure (≤ 5 modified symbols). The larger source-fetching CodeGraph tools remain subagent-only per step 4 and must not be invoked from the main session here.

Schema (Appendix C):

```
**CodeGraph v1**:
modified: <symbol>@<file>:<line> (<N> callers, <M> cross-module) [, ...]
new: <symbol>@<file>:<line> (<kind>) [, ...]
oos_callers: <caller-symbol>@<file>:<line> -> <modified-symbol> [, ...]
```

Each list field is comma-separated; emit `none` when empty. For docs- or test-only closures, all three lists may be `none` (e.g., `modified: none (test-only change)`).

**Example with the block:**
```bash
bd comments add bd-a1b2c3 "**Changes**:
- Added auth middleware in src/middleware/auth.ts
- Created login/logout endpoints in src/routes/auth.ts
- Added JWT token validation

**Verification**: All tests passing (12/12), lint clean, manual login flow tested

**CodeGraph v1**:
modified: AuthMiddleware.validate@src/middleware/auth.ts:42 (3 callers, 1 cross-module), TokenStore.refresh@src/lib/token.ts:18 (1 caller, 0 cross-module)
new: TokenStore@src/lib/token.ts:7 (class)
oos_callers: ApiRouter.login@src/api/auth/login.ts:23 -> AuthMiddleware.validate"
```

When `codegraph_available` is false under `auto` or `off`, omit the block entirely — the comment must remain byte-equivalent to a pre-PRD closure. Under `required` there is no such comment to write: the run stopped at step 4 per the **availability policy**.

### Lesson proposal (optional)

**If this run paid to learn a durable hazard, you may propose it as a crew lesson** by appending a `**Lesson proposal v1**` block to the completion comment. Proposing nothing is the normal case: most runs learn nothing worth every future worker's context, and an empty proposal costs nothing. A run that ends BLOCKED may still propose — put the block in the blocker comment.

Schema:

```
**Lesson proposal v1**:
key: <kebab-case-slug, at most 64 characters>
lesson: <one sentence stating the hazard>
date: <today, YYYY-MM-DD>
```

Rules for what qualifies:

- **A lesson must be falsifiable and dated.** State it so a later reader can check it against the code and delete it when it stops being true; a lesson that cannot be checked cannot be pruned when it goes stale. The date is required — it is how a stale lesson gets found.
- **Never restate what the code already says.** What a function does and who calls it is answered fresh by the index; a cached copy is a defect waiting to mislead. Propose only what the code cannot show — an environmental hazard, a costly surprise, a constraint that lives outside the repository.
- **A lesson true of every Ortus project belongs in this prompt, not in one repository's lessons.** If what you learned is project-general, file a bd issue proposing the prompt change instead of a lesson proposal.

**Every proposal is pending until a human curates it.** Proposals are recorded but not active: `ortus curate` accepts, edits or rejects them, and only accepted lessons are injected into later workers. Nothing you propose reaches another worker without passing that step.

## Completion Signals

**BLOCKED** — When a specific issue cannot be completed due to dependencies or technical blockers. Add a comment explaining the blocker first:
```
<promise>BLOCKED</promise>
```
**Important**: Only use BLOCKED when there's an actual issue you claimed but cannot complete. Do NOT use BLOCKED when the queue is empty.

**Note**: `<promise>BLOCKED</promise>` is a transcript marker only — no shell parser depends on it. A backend may use it as context, but `ortus grind` does not grep for the sentinel; the outer scheduler trusts bd state.

After outputting any signal, stop immediately. Do not continue working.

## Dependencies

Issues may have dependencies. Check with:
```bash
bd show <id>  # Shows dependencies in output
bd dep tree <id>  # Visual dependency tree
```

Only work on issues that have no unresolved blockers (i.e., issues shown by `bd ready`).
