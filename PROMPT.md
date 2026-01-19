# Ralph Wiggum Loop Prompt

Read @AGENTS.md for session rules and landing-the-plane protocol.
Read @activity.md for what was completed in the last iteration.

## Your Task

1. **Check Status**: Run `bd ready --assignee ralph` to see tasks assigned to you with no blockers
2. **Select One Task**: Pick the highest-priority task from ready work
3. **Claim It**: Run `bd update <id> --status=in_progress`
4. **Implement**: Make the code changes described in the task
5. **Verify**: Run tests, linting, or other verification appropriate to your project
6. **Complete**: Run `bd close <id> --reason="<brief summary>"`
7. **Log Progress**: Update activity.md with dated entry
8. **Commit**: Stage and commit your changes with descriptive message
9. **Push**: Run `git pull --rebase && bd sync && git push` to preserve work

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
- **Update activity.md** - Record what you did with timestamp
- **Run quality checks** - Always run linting/tests before committing
- **Descriptive commits** - Include task ID in commit message

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
