# Ralph Wiggum Loop Prompt

Read @AGENTS.md for session rules and landing-the-plane protocol.

You are invoked in a bash loop. Each invocation = one task. The loop restarts you with fresh context after you exit. Do ONE thing, then stop.

## Your Task

1. **Orient**: Run `bd activity --limit 10 --json | jq -r '.[].issue_id' | sort -u | xargs -I{} sh -c 'echo "=== {} ===" && bd comments {} 2>/dev/null'` to see what happened in previous loops
2. **Select**: Run `bd ready --json` to get issues with no blockers. If empty, output `<promise>EMPTY</promise>` and stop immediately (do not output BLOCKED).
3. **Claim**: Run `bd update <id> --status=in_progress` for the first issue before doing anything else
4. **Investigate**: Search the codebase first — don't assume not implemented. Use subagents for broad searches.
5. **Implement**: Make the code changes described in the issue
6. **Verify**: Run tests, linting, and builds (see Verification below). If they fail, fix and re-verify — this is backpressure, not a reason to stop.
7. **Log**: Add structured completion comment (see format below)
8. **Close**: Run `bd close <id> --reason="<brief summary>"`
9. **Commit & Push**: Stage, commit with issue ID in message, then `git pull --rebase && bd sync && git push`
10. **Exit**: Output the appropriate signal (see Completion Signals) and stop. You are done. The loop will restart you for the next task.

If you cannot complete the claimed issue (dependency, technical blocker, persistent test failure you cannot resolve), add a comment explaining the blocker via `bd comments add <id> "..."`, then output `<promise>BLOCKED</promise>` and stop.

## Verification
Run all relevant testing for the task that you have completed.

If verification fails, fix the issue and re-verify. This is backpressure — keep iterating until it passes or you determine the issue is a blocker outside your task's scope.

## Issue Type Rules

**task** — Implement exactly what's specified:
- NO scope expansion
- All acceptance criteria must pass before closing
- Search the codebase first — don't assume something isn't already built
- If you discover additional work, create a new issue with `bd create`

**bug** — Reproduce, diagnose, fix:
- NO unrelated changes — fix only the bug
- Minimal, focused fix — don't refactor surrounding code
- Regression test is required
- If you discover related bugs, create new issues with `bd create --type=bug`

**epic/feature** — Milestone check:
- These are containers for related work
- Run `bd show <id>` to see child issues
- If all children are closed, close with `bd close <id> --reason="All child issues complete"`
- If children remain open, output `<promise>BLOCKED</promise>` — the loop will retry later

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

## Ultrathink Directive

Claude uses adaptive thinking — it decides when and how deeply to reason, including between tool calls (interleaved thinking). For the following problem types, ensure deep reasoning by using max effort for Opus or high effort for Sonnet subagents:

- Architecture decisions spanning multiple files
- Debugging subtle or intermittent issues
- Performance optimization trade-offs
- Security-sensitive code paths

Reasoning and action are interleaved automatically. You do not need to plan everything upfront before acting.

## Steering

**Upstream (issue descriptions are your spec):**
- The issue description is authoritative — implement what it says, not what you think it should say
- Follow existing code patterns found in src/ — match style, naming, structure
- Use shared utilities and existing abstractions before creating new ones
- If the issue is ambiguous, add a comment asking for clarification and output BLOCKED

**Downstream (tests/lints/builds are your guardrails):**
- Tests, lints, and builds reject invalid work — they are the final arbiter
- Iterate until passing — do not close an issue with failing checks
- Backpressure is a feature, not an obstacle — it tells you something is wrong
- If downstream checks reveal the issue spec is wrong, comment and BLOCKED

## Context Management

- Fresh ~200K token window per invocation — this is your budget, spend it wisely
- 40-60% utilization is the "smart zone" — past 60% model quality degrades, past 80% you are in trouble
- Never load large files into the main context — use subagents to read and summarize
- Keep AGENTS.md operational and brief (~60 lines) — it is loaded every invocation
- Prefer markdown over JSON for LLM communication — fewer tokens, same information
- One tight, well-scoped task = 100% smart zone utilization

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