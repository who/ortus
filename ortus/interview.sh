#!/bin/bash
# interview.sh - Interactive Claude-powered interviews for feature refinement
#
# Usage: ./ortus/interview.sh [feature-id]
#
# Options:
#   <feature-id>    Optional: Interview a specific feature
#   -h, --help      Show this help message
#
# This script conducts dynamic interviews using Claude's AskUserQuestion tool
# to gather requirements for features before PRD generation.
#
# Workflow:
#   1. Finds features assigned to ralph without 'interviewed' label
#   2. Lets user select which feature to interview (if multiple)
#   3. Claude conducts an interactive interview asking targeted questions
#   4. Answers are saved as comments on the feature bead
#   5. 'interviewed' label is added to trigger PRD generation
#
# After interview completion, run ortus/ralph.sh to implement the tasks.
#
# Exit codes:
#   0 - Interview completed successfully
#   1 - Error occurred
#   2 - No features need interviewing

set -e

# Parse arguments
FEATURE_ID=""
while [[ $# -gt 0 ]]; do
  case $1 in
    -h|--help)
      head -n 23 "$0" | tail -n +2 | sed 's/^# //' | sed 's/^#//'
      exit 0
      ;;
    *)
      FEATURE_ID="$1"
      shift
      ;;
  esac
done

# Colors for output (respect NO_COLOR)
if [ -z "${NO_COLOR:-}" ] && [ -t 1 ]; then
  BOLD="\033[1m"
  DIM="\033[2m"
  RESET="\033[0m"
  GREEN="\033[32m"
  YELLOW="\033[33m"
  CYAN="\033[36m"
else
  BOLD=""
  DIM=""
  RESET=""
  GREEN=""
  YELLOW=""
  CYAN=""
fi

echo_info() {
  echo -e "${CYAN}ℹ${RESET} $*"
}

echo_success() {
  echo -e "${GREEN}✓${RESET} $*"
}

echo_warn() {
  echo -e "${YELLOW}!${RESET} $*"
}

echo_error() {
  echo -e "\033[31m✗${RESET} $*" >&2
}

# Find features that need interviewing
find_pending_features() {
  # Get all features assigned to ralph
  local features_json
  features_json=$(bd list --type feature --status open --json 2>/dev/null || echo "[]")

  # Filter out features that already have 'interviewed' label
  echo "$features_json" | jq -c '[.[] | select(.labels | (. == null) or (index("interviewed") | not) and (index("prd:interviewing") | not) and (index("prd:ready") | not) and (index("approved") | not))]'
}

# Display feature selection menu
select_feature() {
  local features_json="$1"
  local count
  count=$(echo "$features_json" | jq 'length')

  if [ "$count" = "0" ]; then
    echo_info "No features need interviewing."
    echo ""
    echo "Features get interviewed when they:"
    echo "  1. Have type 'feature'"
    echo "  2. Don't have 'interviewed', 'prd:interviewing', 'prd:ready', or 'approved' labels"
    echo ""
    echo "Create a new feature with:"
    echo "  bd create --title=\"My feature\" --type=feature"
    exit 2
  fi

  if [ "$count" = "1" ]; then
    # Only one feature, select it automatically
    echo "$features_json" | jq -r '.[0].id'
    return 0
  fi

  # Multiple features - show menu
  echo ""
  echo -e "${BOLD}Select a feature to interview:${RESET}"
  echo ""

  local i=1
  while IFS= read -r feature; do
    local id title
    id=$(echo "$feature" | jq -r '.id')
    title=$(echo "$feature" | jq -r '.title')
    echo "  $i) $id - $title"
    i=$((i + 1))
  done < <(echo "$features_json" | jq -c '.[]')

  echo ""
  read -p "Enter number (1-$count): " selection

  if ! [[ "$selection" =~ ^[0-9]+$ ]] || [ "$selection" -lt 1 ] || [ "$selection" -gt "$count" ]; then
    echo_error "Invalid selection"
    exit 1
  fi

  echo "$features_json" | jq -r ".[$((selection - 1))].id"
}

# Validate that a feature exists and is appropriate for interviewing
validate_feature() {
  local feature_id="$1"

  local feature_json
  feature_json=$(bd show "$feature_id" --json 2>/dev/null) || {
    echo_error "Feature '$feature_id' not found"
    exit 1
  }

  # Check if already interviewed
  local labels
  labels=$(echo "$feature_json" | jq -r '.[0].labels // [] | join(",")')

  if [[ "$labels" == *"interviewed"* ]]; then
    echo_warn "Feature $feature_id has already been interviewed."
    echo "Labels: $labels"
    read -p "Re-interview anyway? (y/N): " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
      exit 0
    fi
  fi

  echo "$feature_json"
}

# Run the interview session with Claude
run_interview() {
  local feature_id="$1"
  local feature_title="$2"
  local feature_description="$3"

  echo ""
  echo -e "${BOLD}Starting interview for: ${feature_title}${RESET}"
  echo -e "${DIM}Feature ID: ${feature_id}${RESET}"
  echo ""

  # Create a temporary file for the system prompt
  local prompt_tmpfile
  prompt_tmpfile=$(mktemp)
  trap "rm -f '$prompt_tmpfile'" EXIT

  # Build the system prompt with feature context
  cat > "$prompt_tmpfile" <<EOF
You are conducting a product requirements interview for the following feature:

## Feature Details
**ID**: ${feature_id}
**Title**: ${feature_title}
**Description**:
${feature_description}

## Your Task

Conduct a dynamic, conversational interview to gather the information needed to write a comprehensive PRD. Ask 5-8 targeted questions covering:

1. **Problem Space** - What problem does this solve? Who experiences it? How painful is it?
2. **Users & Personas** - Who are the primary users? What are their goals?
3. **Scope** - What's in scope for v1? What should be explicitly out of scope?
4. **Success Criteria** - How will we measure if this succeeded?
5. **Technical Constraints** - Are there specific technologies, integrations, or limitations?
6. **Timeline & Priority** - Any deadlines? How does this compare to other work?

## Interview Guidelines

- Use the AskUserQuestion tool to ask each question
- Adapt follow-up questions based on previous answers
- Don't ask about topics already clear from the description
- Keep questions focused and specific
- After each answer, save it as a comment on the feature bead using: bd comments add ${feature_id} "<answer summary>"

## Completing the Interview

When you have gathered sufficient information (usually 5-8 questions):
1. Save a final summary comment with key insights
2. Add the 'interviewed' label: bd label add ${feature_id} interviewed
3. Thank the user, explain next steps (PRD and tasks will be generated), and prompt them to exit:
   - Tell the user: "The interview is complete! Please type /exit or press Ctrl+C to exit this Claude session."
   - IMPORTANT: Always end with a clear prompt telling the user to exit the session

## CRITICAL: Start Immediately with AskUserQuestion

**Your FIRST action MUST be to call the AskUserQuestion tool.** Do NOT output any text before calling AskUserQuestion. Your very first action must be a tool call containing your greeting AND first question together.

Example first AskUserQuestion call:
- question: "Hi! I'm here to help clarify requirements for your feature. What specific problem are you trying to solve?"
- header: "Problem"
- options: User pain point, Missing capability, Process improvement

Do NOT greet the user in a text response first. Immediately call AskUserQuestion as your first action.
EOF

  # Check if interview-prompt.md exists and use it instead
  # (Use script-relative path so script works from any directory)
  local prompt_file="$(dirname "$0")/prompts/interview-prompt.md"
  if [ -f "$prompt_file" ]; then
    # Read the prompt file and substitute variables using awk for safety
    # (handles special characters in descriptions)
    awk -v id="$feature_id" -v title="$feature_title" -v desc="$feature_description" '
      {
        gsub(/\{\{FEATURE_ID\}\}/, id)
        gsub(/\{\{FEATURE_TITLE\}\}/, title)
        gsub(/\{\{FEATURE_DESCRIPTION\}\}/, desc)
        print
      }
    ' "$prompt_file" > "$prompt_tmpfile"
  fi

  # Initial prompt message to kick off the conversation
  # CRITICAL: This must instruct Claude to use AskUserQuestion as its FIRST action
  local initial_prompt="Your FIRST action must be to call AskUserQuestion. Do not output any text first. Immediately call AskUserQuestion with your greeting and first interview question for feature ${feature_id}."

  # Run Claude by piping the full prompt to stdin
  # This approach ensures Claude immediately processes the prompt and executes tool calls
  #
  # The prompt is structured as:
  # 1. System instructions (from INTERVIEW-PROMPT.md or fallback)
  # 2. Explicit instruction to start immediately with AskUserQuestion
  #
  # We pipe to stdin because this triggers immediate execution vs positional args
  # which may not execute tool calls properly

  local full_prompt
  full_prompt=$(cat "$prompt_tmpfile")
  full_prompt="${full_prompt}

---

${initial_prompt}"

  echo "$full_prompt" | claude --allowedTools "AskUserQuestion,Bash(bd:*),Read"
  local exit_code=$?

  if [ $exit_code -ne 0 ]; then
    if [ $exit_code -eq 130 ]; then
      # User interrupted with Ctrl+C
      echo ""
      echo_warn "Interview interrupted. Progress may be partially saved."
      exit 0
    fi
    echo_error "Interview session ended with error"
    exit 1
  fi

  echo ""
  echo_success "Interview complete!"
  echo ""
  local project_dir
  project_dir=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
  echo "Next steps (from $project_dir):"
  echo "  ./ortus/ralph.sh   # Start implementing tasks"
}

# Main execution
echo ""
echo -e "${BOLD}=== Feature Interview ===${RESET}"
echo ""

if [ -n "$FEATURE_ID" ]; then
  # Specific feature provided
  feature_json=$(validate_feature "$FEATURE_ID")
  feature_title=$(echo "$feature_json" | jq -r '.[0].title')
  feature_description=$(echo "$feature_json" | jq -r '.[0].description // "No description provided"')
else
  # Find features that need interviewing
  echo_info "Searching for features that need interviewing..."

  pending_json=$(find_pending_features)
  pending_count=$(echo "$pending_json" | jq 'length')

  echo_info "Found $pending_count feature(s) awaiting interview"

  FEATURE_ID=$(select_feature "$pending_json")

  feature_json=$(bd show "$FEATURE_ID" --json 2>/dev/null)
  feature_title=$(echo "$feature_json" | jq -r '.[0].title')
  feature_description=$(echo "$feature_json" | jq -r '.[0].description // "No description provided"')
fi

run_interview "$FEATURE_ID" "$feature_title" "$feature_description"
