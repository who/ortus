<!--
Prompt resolution precedence (loaded by core/prompts.py, FR-025):
  1. <repo>/.ortus/prompts/plan-prompt.md   (per-repo override)
  2. ~/.ortus/prompts/plan-prompt.md        (user-wide override)
  3. bundled src/ortus/prompts/plan-prompt.md  (this file — installed default)
The first existing file wins; the others are ignored.
-->

Read $prd_path . Decompose the provided PRD Markdown into a Beads issue graph using bd. For each work item, create an issue with: title, description (scope/context), acceptance_criteria (REQUIRED: must include testable conditions that define 'done' AND specific testing instructions for verification), design notes (technical approach), priority (0-4, 0=critical), type (epic/feature/task/bug/chore), labels, and estimated_minutes. CRITICAL: Every task MUST have acceptance_criteria with: (1) testable conditions - what must be true when done, (2) testing instructions - how to verify each condition. Structure hierarchically: epics for major features, decomposed into tasks via parent-child dependencies; use blocks for execution order constraints and related for shared context. Output all bd create and bd dep add commands to construct the complete graph with proper dependencies reflecting the PRD's requirements and sequence. Emit them as a single sequential bash script and execute the entire script in one process. Do NOT fan out bd writes across parallel subagents — Dolt enforces an exclusive single-writer database lock, so concurrent bd create / bd dep add calls will collide and fail. End the turn. The /goal evaluator will terminate the session when every PRD work item has a bd issue.
