# Feature Workflow

Features may be small (implement directly) or large (decompose first).

## Size Assessment

First, assess the feature size:

**Small features** (implement directly):
- Single file changes
- Under ~100 lines of new code
- Clear, well-defined scope
- No architectural decisions needed

**Large features** (decompose first):
- Multiple files affected
- Over ~100 lines of new code
- Unclear implementation path
- Requires architectural decisions
- Has multiple distinct components

## Small Feature Workflow

1. **Implement** the feature directly
2. **Verify** using project quality checks
3. **Close** with summary of what was delivered

## Large Feature Workflow

1. **Analyze** the feature to identify components
2. **Decompose** into atomic tasks:
   ```bash
   bd create --title="<component 1>" --type=task --assignee=ralph --priority=2
   bd dep add <new-task-id> <feature-id>

   bd create --title="<component 2>" --type=task --assignee=ralph --priority=2
   bd dep add <new-task-id> <feature-id>
   ```
3. **Document** the decomposition in a comment:
   ```bash
   bd comments add <id> "Decomposed into N tasks: <task-ids>"
   ```
4. **Continue** working on the first task in this session
   - The feature will close automatically when all child tasks complete (via Epic Ceremony)

## Decomposition Guidelines

When breaking down a large feature:
- Each task should be completable in a single session
- Tasks should have clear acceptance criteria
- Order tasks by dependencies (prerequisite work first)
- Aim for 3-7 tasks for most features

## Completion

- Small features: Close after implementation
- Large features: DO NOT close manually. The Epic Closure Ceremony handles this when all child tasks complete.
