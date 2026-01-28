# Ralph Wiggum Loop Prompt

Read @AGENTS.md for session rules and landing-the-plane protocol.

## Your Task

1. **Check Recent Activity**: Run `bd activity --limit 10 --json | jq -r '.[].issue_id' | sort -u | xargs -I{} sh -c 'echo "=== {} ===" && bd comments {} 2>/dev/null'` to see what happened in previous loops
2. **Get Ready Work**: Run `bd ready --json` to get issues with no blockers. If empty, output `<promise>EMPTY</promise>` and stop immediately (do not output BLOCKED).
3. **Claim Immediately**: Run `bd update <id> --status=in_progress` for the first issue before doing anything else
4. **Implement**: Make the code changes described in the issue
5. **Verify**: Run tests, linting, or other verification appropriate to your project
6. **Log Completion**: Add structured comment (see format below)
7. **Complete**: Run `bd close <id> --reason="<brief summary>"`
8. **Commit**: Stage and commit your changes with descriptive message
9. **Push**: Run `git pull --rebase && bd sync && git push` to preserve work

## Verification
Run all relevant testing for the task that you have completed.

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

Use the Task tool to delegate work efficiently:
- **Search/Read tasks** (exploring codebase, finding files, reading context): Launch up to 500 Sonnet subagents in parallel
- **Build/Test tasks** (running builds, tests, linting): 1 subagent at a time
- **Complex reasoning** (architecture decisions, tricky bugs): Use Opus

## Ultrathink Directive

For complex problems requiring deep reasoning:
- Architecture decisions spanning multiple files
- Debugging subtle or intermittent issues
- Performance optimization trade-offs
- Security-sensitive code paths

Use extended thinking to reason through the problem before implementing.

## Important Rules

- **One issue per iteration** - Do not work on multiple issues
- **No partial work** - Either complete the issue fully or don't start it
- **No placeholders** - Implement completely. No stubs, TODOs, or "implement later" comments
- **Found bugs** - Never fix bugs inline. Always `bd create --type=bug` to track separately
- **Log completion** - Use structured comment format before closing
- **Run quality checks** - Always run verification before committing
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

**COMPLETE** — When you have successfully completed ONE issue:
```
<promise>COMPLETE</promise>
```

**BLOCKED** — When a specific issue cannot be completed due to dependencies or technical blockers. Add a comment explaining the blocker first:
```
<promise>BLOCKED</promise>
```
**Important**: Only use BLOCKED when there's an actual issue you claimed but cannot complete. Do NOT use BLOCKED when the queue is empty.

**EMPTY** — When `bd ready` returns no issues (empty queue):
```
<promise>EMPTY</promise>
```
This signals the loop to stop gracefully. Do not output BLOCKED when queue is empty.

## Dependencies

Issues may have dependencies. Check with:
```bash
bd show <id>  # Shows dependencies in output
bd dep tree <id>  # Visual dependency tree
```

Only work on issues that have no unresolved blockers (i.e., issues shown by `bd ready`).