# Chore Workflow

Chores are maintenance tasks: refactoring, dependency updates, configuration changes, cleanup, etc.

## Priority

Chores are low-priority by nature. Only work on a chore when:
- No tasks, bugs, or features are ready
- The chore has no blockers

If higher-priority work becomes available, the chore can be deferred.

## Steps

1. **Understand** the chore requirements
2. **Implement** the changes described
3. **Verify** nothing is broken:
   - Run tests
   - Run linting
   - For dependency updates: verify build still works
4. **Close** with summary of changes made

## Types of Chores

- **Refactoring**: Improve code structure without changing behavior
- **Dependency updates**: Upgrade packages/libraries
- **Configuration**: Update build configs, CI/CD, etc.
- **Cleanup**: Remove dead code, fix warnings, organize files
- **Documentation**: Update internal docs (not user-facing)

## Completion Criteria

A chore is complete when:
- The described maintenance is done
- All tests still pass
- No regressions introduced
- Code is committed with descriptive message
