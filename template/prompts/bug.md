# Bug Workflow

Bugs require a debug-first approach with mandatory regression testing.

## Steps

1. **Reproduce** the bug first
   - Follow the reproduction steps in the issue description
   - If you cannot reproduce, add a comment requesting more information and mark as BLOCKED
   - Document the exact conditions where the bug occurs

2. **Diagnose** the root cause
   - Identify the problematic code path
   - Understand why the bug occurs
   - Document your findings in a comment

3. **Fix** the issue
   - Implement the minimal fix that addresses the root cause
   - Avoid unrelated changes or over-engineering

4. **Regression Test** (REQUIRED)
   - Write a test that would have caught this bug
   - The test must fail before the fix and pass after
   - If the project has no test framework, document manual verification steps

5. **Verify** the fix
   - Run the full test suite
   - Manually verify the original reproduction steps no longer trigger the bug

## Reproduction Failure

If you cannot reproduce the bug:
1. Add a comment: `bd comments add <id> "Cannot reproduce: <what you tried>"`
2. Request more information from the reporter
3. Output `<promise>BLOCKED</promise>` and stop

## Completion Criteria

A bug is complete ONLY when:
- The bug is verified fixed
- A regression test exists that covers the bug
- All other tests still pass
- The fix is committed with descriptive message

**IMPORTANT**: You cannot close a bug without a regression test. This is a hard requirement.
