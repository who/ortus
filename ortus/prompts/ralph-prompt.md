# Ralph Wiggum Loop Prompt

Read @AGENTS.md for session rules and landing-the-plane protocol.

You are invoked in a bash loop. Each invocation = one task. The loop restarts you with fresh context after you exit. Do ONE thing, then stop.

## Your Task

1. **Orient**: Run `bd list --sort updated --all --limit 10 --json | jq -r '.[].id' | xargs bd show --json` to see what happened in previous loops.

   **Activity read (FR-401..403).** When `codegraph_available`, additionally surface recent CodeGraph activity for files touched in the last ~20 commits. Run `git log -20 --name-only | sort -u` to derive the file list, then enrich it:

   - Prefer `codegraph_files` — one batched call across the file list when the tool is available.
   - Fall back to per-file `codegraph_search` when `codegraph_files` is unavailable.

   Cap the result at **30 unique files** and **50 symbols** total; truncate beyond the cap rather than erroring. Add the surfaced symbols to the orient context block alongside the existing `bd list` output above — that invocation is preserved verbatim per FR-401, this sub-step is additive only. When CodeGraph isn't available, skip silently.

   **CodeGraph block reuse (FR-404).** Additionally scan the recent bd comments returned by the `bd show --json` invocation above for `**CodeGraph v1**` headers (the FR-101 schema). For each recognized v1 block, parse the `modified:` line and surface the `symbol@file:line` entries into the orient context alongside the activity-read output — this is the compounding-memory payoff of FR-102's parseable schema. The parser is tolerant per Appendix Q4: silently skip blocks whose schema version is unrecognized (e.g., a future `**CodeGraph v2**` this prompt hasn't learned yet) rather than erroring. Gated on `codegraph_available`; skip silently when CodeGraph isn't available.
2. **Select**: Run `bd ready --json` to get issues with no blockers. If empty, output `<promise>EMPTY</promise>` and stop immediately (do not output BLOCKED).
3. **Claim**: Run `bd update <id> --status=in_progress` for the first issue before doing anything else
4. **Investigate**: Before assuming anything is or isn't implemented, search the codebase. First, decide which path to take:

   - **`codegraph_available`** if `.codegraph/` exists at the project root *and* at least one tool whose name starts with `mcp__codegraph__` is registered in this session.
   - Otherwise, fall through to the default subagent-grep path. Do not mention CodeGraph in any output.

   If **`codegraph_available`**, use these tools as the primary investigation surface (cheap, main-context-safe):

   - `codegraph_search` — find symbols by name.
   - `codegraph_callers` / `codegraph_callees` — trace call flow.
   - `codegraph_impact` — assess blast radius before editing.
   - `codegraph_node` — pull a single symbol's details (with source if needed).

   For broader, task-shaped questions ("how does X work?", "where does feature Y live?"), spawn a subagent and have it call `codegraph_explore` or `codegraph_context`. Never call those two from the main context — they return large source-code payloads that will blow your scheduler budget.

   Fall back to subagent grep/glob/Read **only** if CodeGraph returns nothing useful for the question.

   If **not** `codegraph_available`: Search the codebase first — don't assume not implemented. Use subagents for broad searches.
5. **Implement**: Make the code changes described in the issue
6. **Verify**: Run tests, linting, and builds (see Verification below). If they fail, fix and re-verify — this is backpressure, not a reason to stop.

**6.5. Refresh the index (best-effort).** If codegraph_available and the `codegraph` CLI is on $PATH, run `codegraph sync` once. Ignore the exit code. Do not block the loop on this. If CodeGraph isn't available, skip silently — do not mention it in the completion comment.

7. **Log**: Add structured completion comment (see format below)

**7.5. Spawn follow-ups (FR-201..206; best-effort).** When `codegraph_available` and the **CodeGraph v1** block emitted in step 7 lists at least one entry under `oos_callers`, create bd issues for those callers before closing. Step 7.5 runs after step 7 (the block is now parseable) and before step 8 (the closing issue is still `in_progress`, so `bd dep add <new-id> --depends-on <closing-id>` references an open issue — the spawned issues only enter `bd ready` once step 8 closes the closing one). Apply the FR-202 heuristic gate to filter callers, the FR-203 cap-and-umbrella mapping to choose per-caller vs umbrella shape, and the FR-206 idempotency check before each `bd create`.

Each spawned issue uses this metadata (FR-204):

- `--type=task`
- `--priority=2`
- `--labels=auto-codegraph` (so the cohort is identifiable and bulk-managed).
- Title and description from the Appendix E template (per-caller or umbrella), including the closing-issue id, the modified symbol, the caller's `symbol@file:line`, and the closing commit (`git rev-parse HEAD` if available).
- After `bd create` succeeds, run `bd dep add <new-id> --depends-on <closing-id>` so the spawned issue does not enter `bd ready` until step 8 closes the closing one.

**Non-blocking (FR-205).** Step 7.5 shall never block step 8. If `bd create` returns non-zero, if `codegraph_impact` errors, or if the gate evaluation throws, log to a comment if convenient and proceed to step 8 — same posture as step 6.5. If `codegraph_available` is false, or if the FR-101 block's `oos_callers` is `none`, skip silently.

8. **Close**: Run `bd close <id> --reason="<brief summary>"`
9. **Commit & Push**: Stage, commit with issue ID in message, then run:

       if [ -n "$(git remote)" ]; then
         git pull --rebase --autostash && bd dolt push && git push
       else
         echo "No git remote configured; skipping push (local-only project)."
       fi
10. **Exit**: Output the appropriate signal (see Completion Signals) and stop. You are done. The loop will restart you for the next task.

If you cannot complete the claimed issue (dependency, technical blocker, persistent test failure you cannot resolve), add a comment explaining the blocker via `bd comments add <id> "..."`, then output `<promise>BLOCKED</promise>` and stop.

## Verification
Run all relevant testing for the task that you have completed.

If verification fails, fix the issue and re-verify. This is backpressure — keep iterating until it passes or you determine the issue is a blocker outside your task's scope.

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

**Reference check (FR-501..503).** When `codegraph_available`, before emitting the plan JSON, extract code-shaped references from the issue body and acceptance criteria using these patterns: `[A-Z][A-Za-z0-9_]*` (CamelCase), `[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*` (dotted methods), and file paths ending in a recognized source extension (`.ts`, `.tsx`, `.js`, `.py`, `.rs`, `.go`, `.java`, `.rb`). Run `codegraph_search` on each extracted reference. For every unresolved reference, append one entry to `missing` per Appendix G in this exact form: `References <symbol> in <field>; no such symbol in graph. Confirm during Investigate or flag as new code.` (where `<field>` is `body` or `acceptance_criteria`). Existing model-judged `missing` entries are preserved verbatim — this is additive only. **Per FR-503, a graph-derived `missing` entry does NOT automatically flip `has_enough_info` to `false`** — the flip stays at the model's discretion, since the symbol may legitimately be new code introduced by this very issue. Skip silently when CodeGraph isn't available.

The scheduler validates the shape — all five keys present, `has_enough_info` a boolean, `missing` an array of strings, `implementation_steps`/`verification_steps` arrays, `closure_reason` a non-empty string — then executes mechanically:

- If `has_enough_info` is `false`, post a bd comment listing each entry in `missing` as a clarification gap, then emit BLOCKED. The scheduler does not judge ambiguity itself; this field is the sole signal.
- Otherwise, execute `implementation_steps` then `verification_steps`, and close with `closure_reason`.

Do not re-derive behavior from the issue's classification in the scheduler; the model folded those signals into the plan. If verification fails, re-prompt the model with the failing output and iterate.

## Subagent Strategy

**Three principles:**
1. **Main context = scheduler only** — never do leaf work in the main context
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

## Completion Comment Format

Use this structured format for the completion comment (step 7):

```bash
bd comments add <id> "**Changes**:
- <file or component modified> - <what was done>
- <another change>

**Verification**: <test results, lint status, manual checks>"
```

**Example:**
```bash
bd comments add bd-a1b2c3 "**Changes**:
- Added auth middleware in src/middleware/auth.ts
- Created login/logout endpoints in src/routes/auth.ts
- Added JWT token validation

**Verification**: All tests passing (12/12), lint clean, manual login flow tested"
```

**Keep it concise** — bullet points for changes, one line for verification.

**When `codegraph_available`, append a `**CodeGraph v1**` block** to the comment so the structural change record is parseable by future loops (FR-101..103). Compute it from the main session using only `codegraph_search`, `codegraph_node`, and `codegraph_impact` against the symbols you modified — bound the work to ≤ 15 tool calls for a typical closure (≤ 5 modified symbols). The larger source-fetching CodeGraph tools remain subagent-only per step 4 and must not be invoked from the main session here.

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

When `codegraph_available` is false, omit the block entirely — the comment must remain byte-equivalent to a pre-PRD closure (NFR-101).

## Completion Signals

**EMPTY** — When `bd ready` returns no issues (empty queue):
```
<promise>EMPTY</promise>
```
This signals the loop to stop gracefully. Do not output BLOCKED when queue is empty.

**COMPLETE** — When you have successfully completed ONE issue:
```
<promise>COMPLETE</promise>
```

**BLOCKED** — When a specific issue cannot be completed due to dependencies or technical blockers. Add a comment explaining the blocker first:
```
<promise>BLOCKED</promise>
```
**Important**: Only use BLOCKED when there's an actual issue you claimed but cannot complete. Do NOT use BLOCKED when the queue is empty.

After outputting any signal, stop immediately. Do not continue working.

## Dependencies

Issues may have dependencies. Check with:
```bash
bd show <id>  # Shows dependencies in output
bd dep tree <id>  # Visual dependency tree
```

Only work on issues that have no unresolved blockers (i.e., issues shown by `bd ready`).