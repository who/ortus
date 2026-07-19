<!--
Prompt resolution precedence (loaded by core/prompts.py, FR-025):
  1. <repo>/.ortus/prompts/plan-prompt.md   (per-repo override)
  2. ~/.ortus/prompts/plan-prompt.md        (user-wide override)
  3. bundled src/ortus/prompts/plan-prompt.md  (this file — installed default)
The first existing file wins; the others are ignored.
-->

Read $prd_path . Decompose the provided PRD Markdown into a Beads issue graph using bd. For each work item, create an issue with: title, description (scope/context), acceptance_criteria (REQUIRED: must include testable conditions that define 'done' AND specific testing instructions for verification), design notes (technical approach), priority (0-4, 0=critical), type (epic/feature/task/bug/chore), labels, and estimated_minutes. Every implementation-ready leaf issue must also name concrete candidate files and symbols, dependencies and callers, and an explicit unresolved-references field (`none` when empty); validate these through the injected CodeGraph phase contract or record grep/Read fallback evidence. CRITICAL: Every task MUST have acceptance_criteria with: (1) testable conditions - what must be true when done, (2) testing instructions - how to verify each condition. Structure hierarchically: epics for major features, decomposed into tasks via parent-child dependencies; use blocks for execution order constraints and related for shared context. Output all bd create and bd dep add commands to construct the complete graph with proper dependencies reflecting the PRD's requirements and sequence. Emit them as a single sequential bash script and execute the entire script in one process. Do NOT fan out bd writes across parallel subagents — Dolt enforces an exclusive single-writer database lock, so concurrent bd create / bd dep add calls will collide and fail. End the turn. The /goal evaluator will terminate the session when every PRD work item has a bd issue.

## Issue ID handling (PREFIX-AGNOSTIC — read before generating any script)

This workspace's bd issue prefix is configurable per `bd init --prefix=<name>` and defaults to the repo basename. **Do NOT assume `bd-` as the prefix anywhere in your generated script.** Common prefixes you may encounter: `ortus-`, `repo-`, `myapp-`, etc. The prefix is not always `bd-`.

Follow these rules in your generated bash script:

1. **Capture new IDs from `bd create` stdout directly** — do not regex for any `bd-XXX` pattern. Use `bd create --silent ...` (prints just the ID, e.g. `repo-k6r`), and bind to a shell variable:

   ```bash
   ID_FEATURE_A=$(bd create --silent --title="..." --description="..." --type=feature --priority=2)
   ID_TEST_A=$(bd create --silent --title="..." --description="..." --type=task --priority=2)
   bd dep add "$ID_TEST_A" "$ID_FEATURE_A"   # tests depend on feature
   ```

2. **Discover existing issue IDs via JSON, not regex.** When the script must reference issues that already exist (e.g., re-running plan on a workspace that has a partial graph), parse `bd list --json` with `jq`:

   ```bash
   # Find an existing issue by title substring; works for any prefix.
   EXISTING_ID=$(bd list --status=open --json | jq -r '.[] | select(.title | contains("Implement add")) | .id' | head -1)
   ```

   Do **not** write `grep -oE 'bd-[a-z0-9]+'` or any regex that assumes the prefix shape. The `id` field in `bd list --json` output is the source of truth.

3. **Idempotency check (re-run safety).** Before creating each issue, check whether one with the same title already exists; if so, reuse its ID instead of creating a duplicate:

   ```bash
   maybe_create() {
     local title="$1"; shift
     local existing
     existing=$(bd list --status=open --json | jq -r --arg t "$title" '.[] | select(.title == $t) | .id' | head -1)
     if [ -n "$existing" ]; then
       echo "$existing"
     else
       bd create --silent --title="$title" "$@"
     fi
   }
   ID_FEATURE_A=$(maybe_create "Implement add(a, b)" --description="..." --type=feature --priority=2)
   ```

Following these rules means the script works identically against `ortus-`, `bd-`, `repo-`, or any other prefix — and re-runs are safe (no duplicate issues).
