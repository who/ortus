#!/bin/bash
# ralph.sh - Unified automation loop for feature refinement and task implementation
#
# Usage: ./ralph.sh [--tasks N] [--iterations N] [--poll-interval N] [--idle-sleep N]
#                   [--implementation-only] [--refinement-only]
#
# Options:
#   --tasks N            Max tasks to complete (default: unlimited, runs until queue empty)
#   --iterations N       Max iterations per task (default: 10)
#   --poll-interval N    Seconds between refinement checks (default: 30)
#   --idle-sleep N       Seconds to sleep when no work available (default: 60)
#   --implementation-only   Only process implementation tasks, skip refinement
#   --refinement-only       Only process refinement work (interviews, PRDs), skip implementation
#
# Examples:
#   ./ralph.sh                    # Run full loop: refinement + implementation
#   ./ralph.sh --tasks 1          # Complete exactly 1 implementation task then exit
#   ./ralph.sh --tasks 5          # Complete up to 5 implementation tasks then exit
#   ./ralph.sh --refinement-only  # Only process features (interviews, PRDs)
#   ./ralph.sh --iterations 20    # Allow 20 iterations per task
#
# The refinement pipeline uses labels to track state:
#   (none)           - New feature, run ./interview.sh to conduct interview
#   interviewed      - Interview complete, generate PRD from comments
#   prd:ready        - PRD generated, awaiting human approval
#   approved         - Human approved, create implementation tasks
#
# Logs are written to logs/ralph-<timestamp>.log
# Watch live with:
#   Human-readable: ./tail.sh                         (auto-follows all logs)
#   Raw output:     tail -f logs/ralph-<timestamp>.log
#
# Exit codes:
#   0 - Task(s) completed successfully
#   1 - Error occurred
#   2 - No ready work available

set -e

# Defaults
MAX_TASKS=""  # Empty = unlimited (mega mode)
ITERATIONS_PER_TASK=10
POLL_INTERVAL=30
IDLE_SLEEP=60
TASK_DELAY=5  # Brief pause between tasks
IMPLEMENTATION_ONLY=false
REFINEMENT_ONLY=false

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --tasks)
      MAX_TASKS="$2"
      shift 2
      ;;
    --iterations)
      ITERATIONS_PER_TASK="$2"
      shift 2
      ;;
    --poll-interval)
      POLL_INTERVAL="$2"
      shift 2
      ;;
    --idle-sleep)
      IDLE_SLEEP="$2"
      shift 2
      ;;
    --implementation-only)
      IMPLEMENTATION_ONLY=true
      shift
      ;;
    --refinement-only)
      REFINEMENT_ONLY=true
      shift
      ;;
    -h|--help)
      head -n 36 "$0" | tail -n +2 | sed 's/^# //' | sed 's/^#//'
      exit 0
      ;;
    *)
      # Legacy support: first positional arg is iterations
      if [[ "$1" =~ ^[0-9]+$ ]]; then
        ITERATIONS_PER_TASK="$1"
        shift
      else
        echo "Unknown option: $1" >&2
        exit 1
      fi
      ;;
  esac
done

# Setup logging
mkdir -p logs
LOG_FILE="logs/ralph-$(date '+%Y%m%d-%H%M%S').log"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# =============================================================================
# Feature Refinement Functions (PRD Pipeline)
# =============================================================================

# Handle new feature without interview - prompt user to run interview.sh
handle_new_feature() {
  local feature_id="$1"
  local feature_title="$2"

  log "  Feature needs interview. Run: ./interview.sh $feature_id"
  log "  Or just run: ./interview.sh  (auto-detects pending features)"
  return 0  # Not an error, just waiting for human to run interview
}

# Handle feature that has been interviewed - generate PRD from comments
handle_interviewed() {
  local feature_id="$1"
  local feature_title="$2"

  log "  Interview complete. Generating PRD..."

  # Get feature details
  local feature_json
  feature_json=$(bd show "$feature_id" --json 2>/dev/null) || {
    log "  ERROR: Failed to get feature details"
    return 1
  }

  local feature_description
  feature_description=$(echo "$feature_json" | jq -r '.[0].description // "No description provided"')

  # Collect interview answers from comments
  local answers
  answers=$(collect_interview_answers "$feature_id")

  # Generate the PRD document
  local prd_file
  prd_file=$(generate_prd_document "$feature_id" "$feature_title" "$feature_description" "$answers") || {
    log "  ERROR: Failed to generate PRD document"
    return 1
  }

  log "  PRD generated: $prd_file"

  # Transition labels: remove interviewed, add prd:ready
  bd label remove "$feature_id" "interviewed" >/dev/null 2>&1 || {
    log "  Warning: Failed to remove interviewed label"
  }

  bd label add "$feature_id" "prd:ready" >/dev/null 2>&1 || {
    log "  ERROR: Failed to add prd:ready label"
    return 1
  }

  log "  PRD ready for review!"
  log "  Review PRD at: $prd_file"
  log "  Add 'approved' label when ready: bd label add $feature_id approved"
  return 0
}

# Collect interview answers from feature comments
collect_interview_answers() {
  local feature_id="$1"
  local answers=""

  # Get comments on this feature
  local comments_json
  comments_json=$(bd comments "$feature_id" --json 2>/dev/null || echo "[]")

  # Format comments as interview answers
  local comment_count
  comment_count=$(echo "$comments_json" | jq 'length' 2>/dev/null || echo "0")

  if [ "$comment_count" = "0" ]; then
    log "    No interview comments found"
    echo "(No interview answers recorded)"
    return 0
  fi

  # Extract all comments and format them
  answers=$(echo "$comments_json" | jq -r '.[] | "### Comment\n\n\(.text)\n"' 2>/dev/null)

  log "    Collected $comment_count interview comment(s)"
  echo "$answers"
}

# Slugify a title for use in filenames
slugify() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/--*/-/g' | sed 's/^-//' | sed 's/-$//'
}

# Generate PRD document from feature and interview answers using Claude
generate_prd_document() {
  local feature_id="$1"
  local feature_title="$2"
  local feature_description="$3"
  local answers="$4"

  # Create the PRD filename
  local slug
  slug=$(slugify "$feature_title")
  local prd_file="prd/PRD-${slug}.md"

  log "    Generating PRD: $prd_file"

  # Build the prompt for Claude
  local prompt
  prompt=$(cat <<'PROMPT_EOF'
You are a senior product manager. Generate a comprehensive PRD document based on the feature and interview answers below.

## Feature Details
**ID**: {{FEATURE_ID}}
**Title**: {{FEATURE_TITLE}}
**Description**:
{{FEATURE_DESCRIPTION}}

## Interview Answers
{{ANSWERS}}

## Your Task

Generate a well-structured PRD document in markdown format. The PRD should follow this structure:

```markdown
# PRD: [Title]

## Metadata
- Source Feature: <feature-id>
- Generated: <date>
- Status: Draft (awaiting approval)

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
Numbered list using format: `[P0] FR-001: The system shall...`
- P0 = must have, P1 = should have, P2 = nice to have

### Non-Functional Requirements
Performance, security, scalability, etc.
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

## Open Questions
Unresolved decisions that need stakeholder input.

## Out of Scope
Explicitly list what this PRD does NOT cover.
```

Output ONLY the markdown document, starting with the # PRD: line. Do not include any preamble or explanation.
PROMPT_EOF
)

  # Substitute variables
  prompt="${prompt//\{\{FEATURE_ID\}\}/$feature_id}"
  prompt="${prompt//\{\{FEATURE_TITLE\}\}/$feature_title}"
  prompt="${prompt//\{\{FEATURE_DESCRIPTION\}\}/$feature_description}"
  prompt="${prompt//\{\{ANSWERS\}\}/$answers}"

  # Call Claude and capture output
  local prd_content
  prd_content=$(echo "$prompt" | claude -p --output-format text 2>/dev/null) || {
    log "    ERROR: Claude call failed"
    return 1
  }

  # Validate we got a PRD (should start with # PRD:)
  if [[ ! "$prd_content" =~ ^#[[:space:]]*PRD: ]]; then
    log "    ERROR: Generated content doesn't look like a PRD"
    log "    First 100 chars: ${prd_content:0:100}"
    return 1
  fi

  # Ensure prd directory exists
  mkdir -p prd

  # Write the PRD file
  echo "$prd_content" > "$prd_file"

  log "    PRD written to $prd_file"
  echo "$prd_file"
  return 0
}

# Handle PRD ready state - waiting for human approval
handle_prd_ready() {
  local feature_id="$1"
  local feature_title="$2"

  # Check if PRD file exists
  local slug
  slug=$(slugify "$feature_title")
  local prd_file="prd/PRD-${slug}.md"

  if [ -f "$prd_file" ]; then
    log "  PRD available at: $prd_file"
    log "  Waiting for human to approve. Run: bd label add $feature_id approved"
  else
    log "  WARNING: PRD file not found at $prd_file"
    log "  This may indicate PRD generation failed. Check logs."
  fi

  # PRD exists, waiting for human review
  return 0  # Nothing to do, human action required
}

# Generate implementation tasks from PRD using Claude
generate_tasks_from_prd() {
  local feature_id="$1"
  local feature_title="$2"
  local prd_content="$3"

  # Build the prompt for Claude
  local prompt
  prompt=$(cat <<'PROMPT_EOF'
You are a senior software engineer. Analyze the PRD below and break it into implementation tasks.

## PRD Content
{{PRD_CONTENT}}

## Your Task

Generate 3-10 implementation tasks that will fully implement this PRD. Each task should be:
- Atomic and completable in a single work session
- Clearly defined with specific acceptance criteria
- Ordered with dependencies where needed

Consider logical groupings:
- Setup/infrastructure tasks first
- Core feature implementation
- Tests and validation
- Documentation (if needed)

## Output Format

For EACH task, output exactly this format (I will parse this programmatically):

<task>
<title>Short task title (max 80 chars)</title>
<priority>1-4 (1=critical, 2=high, 3=medium, 4=low)</priority>
<depends_on>comma-separated task numbers if this task depends on others, or "none"</depends_on>
<description>
Full task description with:
- What needs to be implemented
- Key requirements from PRD
- Acceptance criteria
</description>
</task>

Output 3-10 task blocks, numbered in order. Nothing else.
PROMPT_EOF
)

  # Substitute variables
  prompt="${prompt//\{\{PRD_CONTENT\}\}/$prd_content}"

  # Call Claude and capture output
  local claude_output
  claude_output=$(echo "$prompt" | claude -p --output-format text 2>/dev/null) || {
    log "    ERROR: Claude call failed"
    return 1
  }

  # Parse tasks and create beads
  local task_count=0
  local -a created_ids=()
  local -a task_numbers=()

  # Parse task blocks line by line
  local in_task=false in_description=false
  local title="" priority="" depends_on="" description=""
  local task_num=0

  while IFS= read -r line; do
    case "$line" in
      *"<task>"*)
        in_task=true
        task_num=$((task_num + 1))
        title=""
        priority="2"
        depends_on=""
        description=""
        in_description=false
        ;;
      *"<title>"*)
        title=$(echo "$line" | sed 's/.*<title>\(.*\)<\/title>.*/\1/')
        ;;
      *"<priority>"*)
        priority=$(echo "$line" | sed 's/.*<priority>\(.*\)<\/priority>.*/\1/')
        # Validate priority is 1-4
        if ! [[ "$priority" =~ ^[1-4]$ ]]; then
          priority="2"
        fi
        ;;
      *"<depends_on>"*)
        depends_on=$(echo "$line" | sed 's/.*<depends_on>\(.*\)<\/depends_on>.*/\1/')
        ;;
      *"<description>"*)
        in_description=true
        description=""
        ;;
      *"</description>"*)
        in_description=false
        ;;
      *"</task>"*)
        if [ -n "$title" ]; then
          # Add PRD reference to description
          local full_description="${description}

## Reference
- Source PRD feature: ${feature_id}
- Parent feature: ${feature_title}"

          # Create the task bead
          local new_id
          new_id=$(bd create --title="$title" \
            --type=task \
            --priority="$priority" \
            --assignee=ralph \
            --body-file - <<< "$full_description" 2>/dev/null | grep -oE '[a-z]+-[a-z0-9]+' | head -1 || echo "")

          if [ -n "$new_id" ]; then
            log "    Created task $task_num: $new_id - $title"
            created_ids+=("$new_id")
            task_numbers+=("$task_num")
            task_count=$((task_count + 1))

            # Handle dependencies on previous tasks
            if [ -n "$depends_on" ] && [ "$depends_on" != "none" ]; then
              # Parse comma-separated dependency numbers
              IFS=',' read -ra dep_nums <<< "$depends_on"
              for dep_num in "${dep_nums[@]}"; do
                dep_num=$(echo "$dep_num" | tr -d ' ')
                # Find the task ID for this number
                for idx in "${!task_numbers[@]}"; do
                  if [ "${task_numbers[$idx]}" = "$dep_num" ]; then
                    local dep_id="${created_ids[$idx]}"
                    bd dep add "$new_id" "$dep_id" >/dev/null 2>&1 || {
                      log "    Warning: Failed to add dependency $new_id -> $dep_id"
                    }
                    log "      Depends on task $dep_num ($dep_id)"
                    break
                  fi
                done
              done
            fi
          else
            log "    Warning: Failed to create task: $title"
          fi
        fi
        in_task=false
        ;;
      *)
        if [ "$in_description" = true ]; then
          description="${description}${line}
"
        fi
        ;;
    esac
  done <<< "$claude_output"

  if [ "$task_count" -eq 0 ]; then
    log "    ERROR: No tasks were created"
    return 1
  fi

  log "  Created $task_count implementation tasks"
  echo "${created_ids[*]}"
  return 0
}

# Handle approved PRD - create implementation tasks
handle_approved() {
  local feature_id="$1"
  local feature_title="$2"

  log "  PRD approved! Creating implementation tasks..."

  # Find the PRD file
  local slug
  slug=$(slugify "$feature_title")
  local prd_file="prd/PRD-${slug}.md"

  if [ ! -f "$prd_file" ]; then
    log "  ERROR: PRD file not found at $prd_file"
    return 1
  fi

  # Read the PRD content
  local prd_content
  prd_content=$(cat "$prd_file")

  log "    Reading PRD from $prd_file"

  # Generate tasks from PRD
  local task_ids
  task_ids=$(generate_tasks_from_prd "$feature_id" "$feature_title" "$prd_content") || {
    log "  ERROR: Failed to generate tasks from PRD"
    return 1
  }

  # Count tasks created
  local task_count
  task_count=$(echo "$task_ids" | wc -w)

  # Remove labels
  bd label remove "$feature_id" "prd:ready" >/dev/null 2>&1 || true
  bd label remove "$feature_id" "approved" >/dev/null 2>&1 || true

  # Close the feature with summary
  local close_reason="PRD complete. Created $task_count implementation tasks. Tasks: $task_ids"
  bd close "$feature_id" --reason="$close_reason" >/dev/null 2>&1 || {
    log "  Warning: Failed to close feature $feature_id"
  }

  log "  Feature $feature_id closed. $task_count tasks created."
  log "  View ready tasks: bd ready --assignee ralph"
  return 0
}

# Process a single feature based on its state (labels)
process_feature() {
  local feature_id="$1"
  local feature_title="$2"
  local labels="$3"

  log "Processing feature: $feature_id - $feature_title"
  log "  Labels: ${labels:-none}"

  # Route based on state labels (order matters - check most advanced state first)
  if [[ "$labels" == *"approved"* ]]; then
    handle_approved "$feature_id" "$feature_title"
  elif [[ "$labels" == *"prd:ready"* ]]; then
    handle_prd_ready "$feature_id" "$feature_title"
  elif [[ "$labels" == *"interviewed"* ]]; then
    handle_interviewed "$feature_id" "$feature_title"
  else
    # New feature - no interview yet
    handle_new_feature "$feature_id" "$feature_title"
  fi
}

# Check for refinement work (features needing PRD pipeline)
check_refinement_work() {
  log ""
  log "--- Checking for refinement work (features) ---"

  # Get features assigned to ralph (type=feature)
  local features_json
  features_json=$(bd list --assignee ralph --type feature --status open --json 2>/dev/null || echo "[]")
  local feature_count
  feature_count=$(echo "$features_json" | jq -r 'length' 2>/dev/null || echo "0")

  if [ "$feature_count" = "0" ] || [ -z "$feature_count" ]; then
    log "No features to refine"
    return 1  # No work found
  fi

  log "Found $feature_count feature(s) to process"

  local did_work=false

  # Process each feature
  for i in $(seq 0 $((feature_count - 1))); do
    local feature_id feature_title feature_labels
    feature_id=$(echo "$features_json" | jq -r ".[$i].id")
    feature_title=$(echo "$features_json" | jq -r ".[$i].title")
    feature_labels=$(echo "$features_json" | jq -r ".[$i].labels // [] | join(\",\")")

    process_feature "$feature_id" "$feature_title" "$feature_labels" && did_work=true || true
  done

  if [ "$did_work" = true ]; then
    return 0  # Did some work
  fi
  return 1  # No actionable work
}

# =============================================================================
# Task Implementation Functions
# =============================================================================

# Run a single task to completion
run_single_task() {
  local task_num=$1

  for ((i=1; i<=ITERATIONS_PER_TASK; i++)); do
    log ""
    log "=== Task #$task_num - Iteration $i/$ITERATIONS_PER_TASK ==="
    log "$(date '+%Y-%m-%dT%H:%M:%S%z')"
    log "--------------------------------"

    result=$(claude -p "$(cat PROMPT.md)" --output-format stream-json --verbose --dangerously-skip-permissions 2>&1 | tee -a "$LOG_FILE") || true

    if [[ "$result" == *"<promise>COMPLETE</promise>"* ]] || [[ "$result" == *"COMPLETE"* ]]; then
      log ""
      log "Task #$task_num completed after $i iteration(s)."
      return 0
    fi

    if [[ "$result" == *"<promise>BLOCKED</promise>"* ]] || [[ "$result" == *"BLOCKED"* ]]; then
      log ""
      log "Task blocked. Check activity.md for details."
      return 1
    fi

    log ""
    log "--- End of iteration $i ---"
  done

  log ""
  log "Reached max iterations ($ITERATIONS_PER_TASK) for task #$task_num without completion signal."
  return 1
}

# =============================================================================
# Main Execution
# =============================================================================

log "=== Ralph Started ==="
log "Mode: $(
  if [ "$REFINEMENT_ONLY" = true ]; then
    echo "Refinement only (PRD pipeline)"
  elif [ "$IMPLEMENTATION_ONLY" = true ]; then
    echo "Implementation only"
  else
    echo "Full loop (refinement + implementation)"
  fi
)"
if [ -z "$MAX_TASKS" ]; then
  log "Task limit: Unlimited (run until queue empty)"
else
  log "Task limit: $MAX_TASKS task(s)"
fi
log "Iterations per task: $ITERATIONS_PER_TASK"
log "Poll interval: ${POLL_INTERVAL}s"
log "Idle sleep: ${IDLE_SLEEP}s"
log "Log file: $LOG_FILE"
log "Watch live:"
log "  Human-readable: ./tail.sh              (auto-follows all logs)"
log "  Raw output:     tail -f $LOG_FILE"
log ""

tasks_completed=0

while true; do
  # Check if we've hit our task limit (only applies to implementation tasks)
  if [ -n "$MAX_TASKS" ] && [ "$tasks_completed" -ge "$MAX_TASKS" ] && [ "$REFINEMENT_ONLY" = false ]; then
    log ""
    log "========================================"
    log "Completed $tasks_completed task(s). Limit reached."
    log "========================================"
    exit 0
  fi

  did_any_work=false

  # Phase 1: Check for refinement work (features)
  if [ "$IMPLEMENTATION_ONLY" = false ]; then
    if check_refinement_work; then
      did_any_work=true
    fi
  fi

  # Phase 2: Check for implementation work (tasks)
  if [ "$REFINEMENT_ONLY" = false ]; then
    log ""
    log "--- Checking for implementation work (tasks) ---"

    # Check for ready tasks
    ready_count=$(bd ready --assignee ralph --json 2>/dev/null | jq -r '[.[] | select(.issue_type == "task")] | length' 2>/dev/null || echo "0")

    if [ "$ready_count" != "0" ]; then
      log "Found $ready_count ready task(s)"

      log ""
      log "========================================"
      log "Starting implementation task #$((tasks_completed + 1))..."
      log "========================================"

      if run_single_task $((tasks_completed + 1)); then
        tasks_completed=$((tasks_completed + 1))
        did_any_work=true
        log ""
        log "Pausing ${TASK_DELAY}s before checking for more work..."
        sleep "$TASK_DELAY"
      else
        # Task failed or blocked
        log ""
        log "========================================"
        log "Task failed. Tasks completed before failure: $tasks_completed"
        log "========================================"
        exit 1
      fi
    else
      log "No ready tasks for implementation"
    fi
  fi

  # If no work was done in either phase
  if [ "$did_any_work" = false ]; then
    if [ "$tasks_completed" -gt 0 ] && [ "$REFINEMENT_ONLY" = false ]; then
      # We completed some implementation work, queue is now empty
      log ""
      log "========================================"
      log "No more ready work. Tasks completed: $tasks_completed"
      log "========================================"
      exit 0
    elif [ -z "$MAX_TASKS" ] || [ "$REFINEMENT_ONLY" = true ]; then
      # Continuous mode with no work - sleep and retry
      log ""
      log "No work found. Sleeping ${IDLE_SLEEP}s before retry... (Ctrl+C to stop)"
      sleep "$IDLE_SLEEP"
    else
      # Limited mode with no work - exit
      log "No ready work found"
      exit 2
    fi
  fi

  # Brief pause between cycles
  sleep "$POLL_INTERVAL"
done
