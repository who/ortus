# PRD Generation Prompt

> **Note:** For automated PRD generation, use `./ortus/interview.sh` instead of this manual process.
> interview.sh handles the full idea-to-PRD-to-tasks pipeline, then ralph.sh implements the tasks.

This document provides a manual prompt for interactive PRD generation with Claude.

---

## Manual PRD Prompt

```
I want to create a Product Requirements Document (PRD) for the following topic:

**Topic**: [INSERT YOUR SEED PROMPT HERE]

---

## Your Role

You are a senior product manager and technical architect. Your job is to help me think through this idea comprehensively and produce a well-structured PRD.

## Process

### Phase 1: Discovery Interview

Before writing anything, interview me to understand:

1. **Problem Space** - What problem are we solving? Who has this problem? How painful is it?
2. **Users** - Who are the target users? What are their goals and constraints?
3. **Scope** - What's in scope for v1? What's explicitly out of scope?
4. **Success Criteria** - How will we know this succeeded? What metrics matter?
5. **Constraints** - Technical limitations, timeline, budget, team size?
6. **Existing Solutions** - What exists today? Why is it insufficient?
7. **Risks** - What could go wrong? What are the unknowns?

Ask me 3-5 questions at a time. Dig deeper on ambiguous answers. Challenge assumptions. Don't proceed until you have clarity.

### Phase 2: PRD Generation

After the interview, generate a PRD with this structure:

```markdown
# PRD: [Project Name]

## Overview
- **Problem Statement**: One paragraph describing the problem
- **Proposed Solution**: One paragraph describing the solution
- **Success Metrics**: Bulleted list of measurable outcomes

## Background & Context
- Why now? What's the motivation?
- Prior art and alternatives considered

## Users & Personas
- Primary user persona(s)
- User goals and jobs-to-be-done

## Requirements

### Functional Requirements
Numbered list of what the system must do. Each requirement should be:
- Atomic (one thing per requirement)
- Testable (can verify it works)
- Prioritized (P0 = must have, P1 = should have, P2 = nice to have)

Format: `[P0] FR-001: The system shall...`

### Non-Functional Requirements
Performance, security, scalability, accessibility, etc.

Format: `[P1] NFR-001: The system shall...`

## System Architecture
- High-level components and their responsibilities
- Key technical decisions and rationale
- Data flow overview

## Milestones & Phases
Break the work into logical phases, each with:
- **Milestone Name**
- **Goal**: What this milestone achieves
- **Key Deliverables**: Concrete outputs
- **Dependencies**: What must come before

## Epic Breakdown
For each milestone, list epics:

### Epic: [Name]
- **Description**: What this epic accomplishes
- **Requirements Covered**: FR-001, FR-002, etc.
- **Tasks** (high-level):
  - [ ] Task 1
  - [ ] Task 2
  - [ ] Task 3

## Open Questions
Unresolved decisions that need stakeholder input.

## Out of Scope
Explicitly list what this PRD does NOT cover.

## Appendix
- Glossary of terms
- Reference links
- Interview notes summary
```

### Phase 3: Review & Refinement

After generating the PRD:
1. Summarize key decisions made
2. Highlight areas that need stakeholder review
3. Identify ambiguities or gaps

**Iterate up to 5 times**: Ask me if I want to refine, expand, or clarify any section. Each iteration should improve clarity, completeness, and implementability.

---

## Output

Save the final PRD to: `prd/PRD-[project-name].md`
```

---

## Manual Beads Conversion Prompt

After the PRD is finalized, use this prompt to convert it into beads issues.

> **Note:** If you used `./ortus/interview.sh`, it handles this conversion automatically when you approve the PRD.

```
Read the PRD at @prd/PRD-[project-name].md

## Your Role

You are a technical project manager converting this PRD into an executable work breakdown structure using beads issue tracking.

## Process

### Step 1: Phased Plan Design

Ingest the PRD and create a phased implementation plan:

1. **Identify Phases** - Group work into logical phases (e.g., Foundation, Core Features, Polish)
2. **Map Dependencies** - Determine what must come before what
3. **Find Parallelization** - Identify work streams that can run concurrently
4. **Detailed Design** - For each epic, outline the technical approach

### Step 2: Beads Structure

Convert the plan into beads using this hierarchy:

**Priority Labels** (use `--label` flag):
- `P0` - Epics and critical path items
- `P1` - High-priority features
- `P2` - Medium-priority features
- `P3` - Low-priority improvements
- `P4` - Polish, nice-to-haves, future ideas

**Issue Types**:
- `epic` - Large feature areas (parents)
- `feature` - Distinct capabilities within an epic
- `task` - Concrete implementation work (children)
- `bug` - Issues discovered during planning

**Dependency Types**:
- `bd dep add <child> <parent>` - Child belongs to parent epic
- `bd dep add <task> <blocker>` - Task is blocked by another task

### Step 3: Generate Beads Commands

Output a shell script that creates all issues with proper:
- Parent/child relationships via dependencies
- Blocking dependencies for sequential work
- Labels for priority organization
- Detailed descriptions for workers
- Tasks will be picked up by `bd ready` which shows issues with no blockers

Format:
```bash
#!/bin/bash
# Beads setup for [Project Name]
# Generated from PRD

# Phase 1: Foundation
epic1=$(bd create "Epic: [Name]" -t epic -p 0 --label P0 -d "Description" --json | jq -r '.id')
task1=$(bd create "Task: [Name]" -t task -p 1 --label P1 -d "Detailed description with acceptance criteria" --json | jq -r '.id')
bd dep add $task1 $epic1

# Tasks that can run in parallel
task2=$(bd create "Task: [Name]" -t task -p 1 --label P1 -d "Description" --json | jq -r '.id')
task3=$(bd create "Task: [Name]" -t task -p 1 --label P1 -d "Description" --json | jq -r '.id')
bd dep add $task2 $epic1
bd dep add $task3 $epic1

# Sequential dependency
task4=$(bd create "Task: [Name]" -t task -p 2 --label P2 -d "Description" --json | jq -r '.id')
bd dep add $task4 $epic1
bd dep add $task4 $task2  # Blocked by task2

# Phase 2: Core Features (blocked by Phase 1 epic)
epic2=$(bd create "Epic: [Name]" -t epic -p 0 --label P0 -d "Description" --json | jq -r '.id')
bd dep add $epic2 $epic1  # Phase 2 blocked by Phase 1
# ... continue pattern
```

### Step 4: Review & Polish

Before finalizing:
1. **Verify Dependencies** - No circular dependencies, correct blocking order
2. **Check Parallelization** - Independent tasks should NOT block each other
3. **Review Descriptions** - Each task has enough detail for a worker to implement without questions
4. **Proofread** - Clear titles, consistent formatting, no typos
5. **Acceptance Criteria** - Each task describes what "done" looks like

**Iterate up to 5 times**: Ask me if I want to refine the beads structure. Each iteration should improve:
- Dependency accuracy
- Task granularity (not too big, not too small)
- Description clarity
- Parallelization opportunities

### Step 5: Output

1. Save the shell script to: `prd/beads-setup-[project-name].sh`
2. Provide a summary showing:
   - Total epics, features, tasks
   - Critical path (longest dependency chain)
   - Parallel work streams
   - Estimated complexity distribution (P0-P4)
```

---

## Usage

### Recommended: Automated Approach (ralph.sh)

```bash
# Submit an idea using the quick helper
./ortus/idea.sh "Your idea description"

# Or create manually
bd create --title="Your idea description" --type=feature

# Run the interactive interview (generates PRD and creates tasks)
./ortus/interview.sh

# Start Ralph to implement the tasks
./ortus/ralph.sh

# interview.sh will:
# 1. Conduct an interactive interview
# 2. Generate PRD from your answers
# 3. Ask for your approval inline
# 4. Create implementation tasks for ralph
#
# ralph.sh will:
# 5. Implement the tasks automatically
```

### Manual: Creating the PRD

1. Copy the Manual PRD Prompt above
2. Replace `[INSERT YOUR SEED PROMPT HERE]` with your idea
3. Run with Claude (recommend using `claude` CLI or Claude.ai with extended thinking)
4. Answer the interview questions
5. Iterate up to 5 times to refine the PRD

### Manual: Converting to Beads

1. Save the finalized PRD to `prd/PRD-[project-name].md`
2. Copy the Manual Beads Conversion Prompt above
3. Run with Claude, referencing your PRD
4. Iterate up to 5 times to refine the beads structure
5. Review the generated shell script
6. Run the script to create all issues:
   ```bash
   chmod +x prd/beads-setup-[project-name].sh
   ./prd/beads-setup-[project-name].sh
   ```
7. Verify with `bd list` and `bd dep tree <epic-id>`
