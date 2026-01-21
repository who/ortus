# Epic Workflow

Epics are containers for related work. They should NOT be worked on directly - only closed via ceremony.

## When You Pick Up an Epic

If you pick up an epic from `bd ready`, it means all child tasks are complete. Your job is to run the Epic Closure Ceremony.

## Epic Closure Ceremony

### 1. Verify All Children Complete

```bash
bd show <epic-id> --json
```

Check the `blocks` array - all child issues should be closed. If any are open, this epic should not be ready.

### 2. Quality Gate Audit

Before closing the epic, verify all acceptance criteria are met.

#### 2a. Extract Acceptance Criteria

From the epic's description, identify all acceptance criteria:
- Look for "Acceptance criteria" section
- Check for numbered requirements or bullet points
- Each criterion should be testable/verifiable

#### 2b. Map Criteria to Completed Tasks

```
| Criterion | Covered By | Status |
|-----------|------------|--------|
| "Feature X works" | task-abc | Verified |
| "Tests pass" | task-def | Verified |
| "Docs updated" | ??? | MISSING |
```

#### 2c. Handle Uncovered Criteria

If ANY acceptance criteria lack coverage:

```bash
bd create --title="<missing work>" --type=task --assignee=ralph --priority=2
bd dep add <new-task-id> <epic-id>
```

**STOP HERE** if you create new tasks. Do NOT proceed to retrospective or close the epic.

#### 2d. Verify Evidence

Confirm each covered criterion has verification evidence (test results, manual testing documented).

### 3. Write Retrospective

Add an epic closure entry to activity.md:

```markdown
## <ISO-8601 timestamp> - Epic Complete: <epic title>

**Epic**: <epic-id> - <epic title>
**Status**: Completed
**Scope**: <number of child tasks completed>

### Key Decisions
- <Decision 1>: <why this approach was chosen>

### Learnings
- **What worked well**: <positive outcomes>
- **Challenges**: <difficulties and resolutions>

### Follow-up
- <Tech debt or improvements deferred>

---
```

### 4. Close Epic

```bash
bd close <epic-id> --reason="All acceptance criteria met: <summary>"
```

## Important

- Never implement code directly for an epic
- Epics close only after quality gate passes
- If quality gate fails, create tasks and stop
