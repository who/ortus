#!/bin/bash
# lisa.sh - PRD generation and idea-to-implementation pipeline
#
# Usage: ./lisa.sh [--poll-interval N] [--idle-sleep N]
#
# Options:
#   --poll-interval N  Seconds between state checks (default: 30)
#   --idle-sleep N     Seconds to sleep when no work (default: 60)
#
# The pipeline uses labels to track state:
#   (none)           - New idea, needs interview questions generated
#   prd:interviewing - Interview questions created, waiting for human answers
#   prd:ready        - Interview complete, PRD generated, awaiting approval
#   prd:approved     - Human approved, tasks being created for ralph
#
# Workflow:
#   1. User: bd create --title="My idea" --type=idea --assignee=lisa
#   2. Lisa: Generates interview questions as child beads (blocking)
#   3. Human: Answers questions via comments, closes question beads
#   4. Lisa: When all questions closed, generates PRD (prd/PRD-<name>.md)
#   5. Human: Reviews PRD, adds 'approved' label when ready
#   6. Lisa: Creates implementation tasks with --assignee=ralph
#   7. Lisa: Closes the idea
#
# Logs are written to logs/lisa-<timestamp>.log
# Watch live with:
#   Human-readable: ./tail.sh                         (auto-follows all logs)
#   Raw output:     tail -f logs/lisa-<timestamp>.log
#
# Exit codes:
#   0 - Normal exit (Ctrl+C)
#   1 - Error occurred

set -e

# Defaults
POLL_INTERVAL=30
IDLE_SLEEP=60

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --poll-interval)
      POLL_INTERVAL="$2"
      shift 2
      ;;
    --idle-sleep)
      IDLE_SLEEP="$2"
      shift 2
      ;;
    -h|--help)
      head -n 32 "$0" | tail -n +2 | sed 's/^# //' | sed 's/^#//'
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

# Setup logging
mkdir -p logs
LOG_FILE="logs/lisa-$(date '+%Y%m%d-%H%M%S').log"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# Generate interview questions for an idea using Claude
generate_interview_questions() {
  local idea_id="$1"
  local idea_title="$2"
  local idea_description="$3"

  # Build the prompt for Claude to generate questions
  local prompt
  prompt=$(cat <<'PROMPT_EOF'
You are a senior product manager preparing to write a PRD. Analyze the idea below and generate discovery questions.

## Idea Details
**ID**: {{IDEA_ID}}
**Title**: {{IDEA_TITLE}}
**Description**:
{{IDEA_DESCRIPTION}}

## Your Task

Generate 3-7 focused discovery questions that will help write a complete PRD. Questions should cover:
- Problem Space - What problem are we solving? Who has this problem?
- Users - Who are the target users? What are their goals?
- Scope - What's in scope for v1? What's out of scope?
- Success Criteria - How will we know this succeeded?
- Constraints - Technical limitations, timeline?

Not all categories need questions - focus on what's genuinely unclear from the description.

## Output Format

For EACH question, output exactly this format (I will parse this programmatically):

<question>
<short>Short version for title (max 60 chars)</short>
<full>Full question text with context</full>
</question>

Output 3-7 question blocks, nothing else.
PROMPT_EOF
)

  # Substitute variables
  prompt="${prompt//\{\{IDEA_ID\}\}/$idea_id}"
  prompt="${prompt//\{\{IDEA_TITLE\}\}/$idea_title}"
  prompt="${prompt//\{\{IDEA_DESCRIPTION\}\}/$idea_description}"

  # Call Claude and capture output
  local claude_output
  claude_output=$(echo "$prompt" | claude -p --output-format text 2>/dev/null) || {
    log "  ERROR: Claude call failed"
    return 1
  }

  # Parse questions from output and create beads
  local question_count=0
  local created_ids=()

  # Extract all question blocks
  while IFS= read -r -d '' question_block; do
    # Extract short and full from the block
    local short full
    short=$(echo "$question_block" | grep -oP '(?<=<short>).*(?=</short>)' | head -1)
    full=$(echo "$question_block" | grep -oP '(?<=<full>).*(?=</full>)' | head -1)

    if [ -n "$short" ] && [ -n "$full" ]; then
      # Create the question bead
      local description
      description="## Question

$full

## Context

This question relates to the idea: $idea_title (ID: $idea_id)

## How to Answer

1. Add a comment with your answer
2. Close this task when answered"

      # Create the bead
      local new_id
      new_id=$(bd create --title="Q: $short" \
        --type=task \
        --priority=3 \
        --assignee=human \
        --body-file - <<< "$description" 2>/dev/null | grep -oP 'Created issue: \K\S+' || echo "")

      if [ -n "$new_id" ]; then
        log "    Created question: $new_id - Q: $short"
        created_ids+=("$new_id")
        question_count=$((question_count + 1))

        # Add blocking dependency: idea depends on this question
        bd dep add "$idea_id" "$new_id" >/dev/null 2>&1 || {
          log "    Warning: Failed to add dependency $idea_id -> $new_id"
        }
      else
        log "    Warning: Failed to create question bead"
      fi
    fi
  done < <(echo "$claude_output" | grep -oP '<question>.*?</question>' | tr '\n' '\0' || true)

  # Alternative parsing if grep -P doesn't work (more portable)
  if [ "$question_count" -eq 0 ]; then
    # Try simpler parsing
    local in_question=false short="" full=""
    while IFS= read -r line; do
      case "$line" in
        *"<question>"*) in_question=true; short=""; full="" ;;
        *"<short>"*) short=$(echo "$line" | sed 's/.*<short>\(.*\)<\/short>.*/\1/') ;;
        *"<full>"*) full=$(echo "$line" | sed 's/.*<full>\(.*\)<\/full>.*/\1/') ;;
        *"</question>"*)
          if [ -n "$short" ] && [ -n "$full" ]; then
            local description
            description="## Question

$full

## Context

This question relates to the idea: $idea_title (ID: $idea_id)

## How to Answer

1. Add a comment with your answer
2. Close this task when answered"

            local new_id
            new_id=$(bd create --title="Q: $short" \
              --type=task \
              --priority=3 \
              --assignee=human \
              --body-file - <<< "$description" 2>/dev/null | grep -oE '[a-z]+-[a-z0-9]+' | head -1 || echo "")

            if [ -n "$new_id" ]; then
              log "    Created question: $new_id - Q: $short"
              created_ids+=("$new_id")
              question_count=$((question_count + 1))
              bd dep add "$idea_id" "$new_id" >/dev/null 2>&1 || true
            fi
          fi
          in_question=false
          ;;
      esac
    done <<< "$claude_output"
  fi

  if [ "$question_count" -eq 0 ]; then
    log "  ERROR: No questions were created"
    return 1
  fi

  log "  Created $question_count interview questions"
  echo "${created_ids[*]}"
  return 0
}

# State handlers
handle_new_idea() {
  local idea_id="$1"
  local idea_title="$2"

  log "  Generating interview questions..."

  # Get full idea details
  local idea_json
  idea_json=$(bd show "$idea_id" --json 2>/dev/null) || {
    log "  ERROR: Failed to get idea details"
    return 1
  }

  local idea_description
  idea_description=$(echo "$idea_json" | jq -r '.description // "No description provided"')

  # Generate interview questions
  local question_ids
  question_ids=$(generate_interview_questions "$idea_id" "$idea_title" "$idea_description") || {
    log "  ERROR: Failed to generate interview questions"
    return 1
  }

  # Add the interviewing label
  bd label add "$idea_id" "prd:interviewing" >/dev/null 2>&1 || {
    log "  Warning: Failed to add prd:interviewing label"
  }

  log "  Interview started. Questions assigned to 'human'."
  log "  View questions: bd list --assignee human"
  return 0
}

# Collect answers from closed question beads
# Output format: question title + answer text, one per bead
collect_answers() {
  local idea_id="$1"
  local answers=""

  # Get dependencies (question beads) for this idea
  local idea_json
  idea_json=$(bd show "$idea_id" --json 2>/dev/null) || return 1

  # Extract dependency IDs
  local dep_ids
  dep_ids=$(echo "$idea_json" | jq -r '.[0].dependencies[]?.id // empty' 2>/dev/null)

  if [ -z "$dep_ids" ]; then
    log "    No question beads found"
    return 0
  fi

  # Collect answers from each question bead
  while IFS= read -r dep_id; do
    [ -z "$dep_id" ] && continue

    local dep_json
    dep_json=$(bd show "$dep_id" --json 2>/dev/null) || continue

    local dep_title
    dep_title=$(echo "$dep_json" | jq -r '.[0].title // "Unknown question"')

    # Get comments on this question bead
    local comments_json
    comments_json=$(bd comments "$dep_id" --json 2>/dev/null || echo "[]")

    local comment_texts
    comment_texts=$(echo "$comments_json" | jq -r '.[].text // empty' 2>/dev/null | tr '\n' ' ')

    if [ -n "$comment_texts" ]; then
      answers="${answers}### ${dep_title}

${comment_texts}

"
    else
      answers="${answers}### ${dep_title}

(No answer provided)

"
    fi
  done <<< "$dep_ids"

  echo "$answers"
}

handle_interviewing() {
  local idea_id="$1"
  local idea_title="$2"

  log "  Checking if interview is complete..."

  # Get idea details including dependencies (question beads)
  local idea_json
  idea_json=$(bd show "$idea_id" --json 2>/dev/null) || {
    log "  ERROR: Failed to get idea details"
    return 1
  }

  # Count total dependencies and open dependencies
  local total_deps open_deps
  total_deps=$(echo "$idea_json" | jq -r '.[0].dependencies | length' 2>/dev/null || echo "0")

  if [ "$total_deps" = "0" ] || [ -z "$total_deps" ]; then
    log "  No question beads found - skipping interview phase"
    # Transition directly to ready (edge case: no questions were generated)
    bd label remove "$idea_id" "prd:interviewing" >/dev/null 2>&1 || true
    bd label add "$idea_id" "prd:ready" >/dev/null 2>&1 || {
      log "  ERROR: Failed to add prd:ready label"
      return 1
    }
    log "  Transitioned to prd:ready (no interview questions)"
    return 0
  fi

  # Count how many dependencies are still open (not closed)
  open_deps=$(echo "$idea_json" | jq -r '[.[0].dependencies[] | select(.status != "closed")] | length' 2>/dev/null || echo "0")

  log "    Questions: $total_deps total, $open_deps still open"

  if [ "$open_deps" != "0" ]; then
    log "  Interview still in progress - waiting for human to close question beads"
    return 0  # Not an error, just waiting
  fi

  # All questions are answered!
  log "  All questions answered - collecting responses..."

  # Collect answers from question bead comments
  local answers
  answers=$(collect_answers "$idea_id")

  # Store answers in a temp file for the next phase (PRD generation)
  local answers_file="logs/.lisa-answers-${idea_id}.tmp"
  echo "$answers" > "$answers_file"
  log "    Answers saved to $answers_file"

  # Transition labels: remove prd:interviewing, add prd:ready
  bd label remove "$idea_id" "prd:interviewing" >/dev/null 2>&1 || {
    log "  Warning: Failed to remove prd:interviewing label"
  }

  bd label add "$idea_id" "prd:ready" >/dev/null 2>&1 || {
    log "  ERROR: Failed to add prd:ready label"
    return 1
  }

  log "  Interview complete! Transitioned to prd:ready"
  log "  Human review required: add 'approved' label when PRD is approved"
  return 0
}

handle_ready() {
  local idea_id="$1"
  local idea_title="$2"
  log "  Waiting for human to add 'approved' label"
  # PRD exists, waiting for human review
  return 0  # Nothing to do, human action required
}

handle_approved() {
  local idea_id="$1"
  local idea_title="$2"
  log "  TODO: Create implementation tasks for ralph"
  # Will create tasks from PRD and close the idea
  return 1  # Not implemented yet
}

# Process a single idea based on its state (labels)
process_idea() {
  local idea_id="$1"
  local idea_title="$2"
  local labels="$3"

  log "Processing: $idea_id - $idea_title"
  log "  Labels: ${labels:-none}"

  # Route based on state labels
  if [[ "$labels" == *"prd:approved"* ]]; then
    handle_approved "$idea_id" "$idea_title"
  elif [[ "$labels" == *"prd:ready"* ]]; then
    handle_ready "$idea_id" "$idea_title"
  elif [[ "$labels" == *"prd:interviewing"* ]]; then
    handle_interviewing "$idea_id" "$idea_title"
  else
    # New idea - no prd: label yet
    handle_new_idea "$idea_id" "$idea_title"
  fi
}

# Main execution
log "=== Lisa Started ==="
log "Poll interval: ${POLL_INTERVAL}s"
log "Idle sleep: ${IDLE_SLEEP}s"
log "Log file: $LOG_FILE"
log "Watch live:"
log "  Human-readable: ./tail.sh              (auto-follows all logs)"
log "  Raw output:     tail -f $LOG_FILE"
log ""

# Main loop
while true; do
  log ""
  log "--- Checking for ideas ---"

  # Get ideas assigned to lisa
  ideas_json=$(bd ready --assignee lisa --json 2>/dev/null || echo "[]")
  idea_count=$(echo "$ideas_json" | jq -r 'length' 2>/dev/null || echo "0")

  if [ "$idea_count" = "0" ] || [ -z "$idea_count" ]; then
    log "No ideas ready. Sleeping ${IDLE_SLEEP}s... (Ctrl+C to stop)"
    sleep "$IDLE_SLEEP"
    continue
  fi

  log "Found $idea_count idea(s) to process"

  # Process each idea
  for i in $(seq 0 $((idea_count - 1))); do
    idea_id=$(echo "$ideas_json" | jq -r ".[$i].id")
    idea_title=$(echo "$ideas_json" | jq -r ".[$i].title")
    idea_labels=$(echo "$ideas_json" | jq -r ".[$i].labels // [] | join(\",\")")

    process_idea "$idea_id" "$idea_title" "$idea_labels" || {
      log "  Handler returned non-zero (feature not implemented or error)"
    }
  done

  log "Sleeping ${POLL_INTERVAL}s before next check..."
  sleep "$POLL_INTERVAL"
done
