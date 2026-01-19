# Ralph Wiggum Loop Prompt

Read @AGENTS.md for session rules and landing-the-plane protocol.
Read @activity.md for what was completed in the last iteration.

## Your Task

1. **Check Status**: Run `bd ready --assignee ralph` to see tasks assigned to you with no blockers
2. **Select One Task**: Pick the highest-priority task from ready work
3. **Claim It**: Run `bd update <id> --status=in_progress`
4. **Log Start**: Add comment with your implementation plan: `bd comments add <id> "Starting work. Plan: <brief strategy>"`
5. **Implement**: Make the code changes described in the task
6. **Log Progress**: After key milestones, update ticket: `bd comments add <id> "Progress: <what you completed>"`
7. **Verify**: Run tests, linting, or other verification appropriate to your project
8. **Log Verification**: Record verification results: `bd comments add <id> "Verification: <test results summary>"`
9. **Complete**: Run `bd close <id> --reason="<brief summary>"`
10. **Log Activity**: Update activity.md with dated entry
11. **Commit**: Stage and commit your changes with descriptive message
12. **Push**: Run `git pull --rebase && bd sync && git push` to preserve work

## Verification Methods

### For Tests
```bash
bun test
```

### For Linting
```bash
bun run lint
```

### For Type Checking
```bash
npx tsc --noEmit
```

## Important Rules

- **One task per iteration** - Do not work on multiple tasks
- **No partial work** - Either complete the task fully or don't start it
- **Update tickets AND activity.md** - Keep both updated as you progress
- **Run quality checks** - Always run linting/tests before committing
- **Descriptive commits** - Include task ID in commit message

## Ticket Update Guidelines

Keep ticket updates **succinct but informative**. Each comment should be 1-2 sentences max.

**Good examples:**
- `bd comments add api-123 "Starting work. Plan: implement auth middleware, add tests, update docs"`
- `bd comments add api-123 "Progress: auth middleware complete, 5/8 tests passing"`
- `bd comments add api-123 "Verification: all 12 tests pass, ruff clean, mypy clean"`

**Bad examples:**
- ❌ "Working on it" (not informative)
- ❌ "Starting work. I'm going to first read through the codebase to understand..." (too verbose)
- ❌ Long multi-paragraph explanations (use activity.md for detailed logs)

## Completion Signal

When you have completed ONE task successfully, output:

```
<promise>COMPLETE</promise>
```

If you encounter a blocker that prevents completion, document it in activity.md and output:

```
<promise>BLOCKED</promise>
```

## Dependencies

Tasks may have dependencies. Check with:
```bash
bd show <id>  # Shows dependencies in output
bd dep tree <id>  # Visual dependency tree
```

Only work on tasks that have no unresolved blockers (i.e., tasks shown by `bd ready --assignee ralph`).

## Project-Specific Notes

<!-- TODO: Add any additional project-specific notes here -->
