<!--
Prompt resolution precedence (loaded by core/prompts.py, FR-025):
  1. <repo>/.ortus/prompts/plan-prompt.md   (per-repo override)
  2. ~/.ortus/prompts/plan-prompt.md        (user-wide override)
  3. bundled src/ortus/prompts/plan-prompt.md  (this file — installed default)
The first existing file wins; the others are ignored.
-->

Read $prd_path. Decompose the provided PRD Markdown into a Beads issue graph using existing bd fields. Epics may summarize broad outcomes, but every non-epic issue is an executable leaf for a fast implementation worker and MUST satisfy readiness schema v1 below. Resolve architecture and product choices during planning; never leave them for the implementer to infer.

Structure hierarchically: epics for major features, decomposed into leaves via parent-child dependencies; use `blocks` for execution-order constraints and `related` for shared context. Give every issue a title, priority (0-4, 0=critical), type, labels, and estimated minutes. Emit all `bd create` and `bd dep add` commands as one sequential bash script and execute the entire script in one process. Do not fan out bd writes: Dolt enforces a single-writer lock.

## Readiness schema v1 for executable leaves

Use these exact Markdown headings inside the existing bd fields. Every section must contain concrete information. `TODO`, `TBD`, `N/A`, an empty heading, and template text are invalid. When something is intentionally absent, write `None — <why that is safe>`.

`description`:

- `## Objective` — the single outcome this leaf owns.
- `## Behavioral context` — user-visible or system behavior before and after.

`design`:

- `## Readiness schema` — exactly `v1`.
- `## Scope` — work included in this leaf.
- `## Non-goals` — explicit boundaries.
- `## Concrete locations` — candidate files plus symbols, interfaces, or commands; use CodeGraph evidence or record the grep/Read fallback.
- `## Resolved decisions` — architectural and product decisions already made, including rationale where useful.
- `## Compatibility constraints` — supported platforms, APIs, stored data, CLI behavior, or an explained absence.
- `## Ordered steps` — a numbered implementation sequence.
- `## Dependencies` — issue dependencies plus code callers/consumers, or an explained absence.
- `## Edge cases` — failures and boundary conditions the implementation must cover.
- `## Plan-gap guidance` — contradictions or missing material decisions that require the worker to stop, record `PLAN-GAP`, preserve candidate state, and route to planning/human handling instead of improvising.

`acceptance_criteria`:

- `## Observable criteria` — one observable result per stable identifier (`AC-1`, `AC-2`, ...).
- `## Criterion checks` — exactly one matching entry for every AC identifier, with an exact command or deterministic inspection in backticks.
- `## Targeted tests` — exact bounded test commands in backticks. Follow the repository's testing policy; do not make a full local matrix the worker default.

Notes may carry supplementary evidence only; never put required readiness content solely in notes.

## Complete executable-leaf example

```bash
ID_FLAG=$(bd create --silent \
  --title="Add dry-run command path" \
  --type=feature --priority=2 \
  --description='## Objective
Add a dry-run path that reports intended writes without changing state.

## Behavioral context
Today the command always writes. After this change, `--dry-run` prints the same planned operations and performs zero writes.' \
  --design='## Readiness schema
v1

## Scope
Parse the flag, thread it into execution, and suppress state-changing calls.

## Non-goals
No redesign of ordinary command output and no new interactive prompt.

## Concrete locations
Edit `src/example/commands/run.py` in `run()` and the `Executor.apply()` interface; cover `tests/test_run.py::test_dry_run`.

## Resolved decisions
Dry-run uses the existing operation renderer; it does not maintain a second simulation engine.

## Compatibility constraints
Normal invocations keep stdout and exit-code behavior unchanged on Linux and macOS.

## Ordered steps
1. Add the CLI flag to `run()`.
2. Pass the boolean to `Executor.apply()`.
3. Render operations and bypass writes when enabled.
4. Add focused tests and documentation.

## Dependencies
No issue dependency — standalone leaf. Callers are `cli.app` and `Executor.apply()` consumers.

## Edge cases
Empty plans still exit zero; render failures remain nonzero; dry-run must not create state directories.

## Plan-gap guidance
If the renderer and executor disagree about operation ordering, record `PLAN-GAP` with both symbols and route to planning; do not choose an ordering.' \
  --acceptance='## Observable criteria
- AC-1: `--dry-run` reports the ordered operations and performs no writes.
- AC-2: Invocations without `--dry-run` retain existing behavior.

## Criterion checks
- AC-1: Run `uv run pytest tests/test_run.py::test_dry_run -q`.
- AC-2: Run `uv run pytest tests/test_run.py::test_normal_run -q`.

## Targeted tests
Run `uv run pytest tests/test_run.py -q`.' \
  --notes='CodeGraph confirmed `cli.app -> run -> Executor.apply`; no out-of-scope callers.')
```

End the turn after the complete issue graph exists. The caller mechanically validates every new leaf and may run one fresh planning-profile repair pass against the exact defective IDs. Repair must update those IDs in place: never create replacements, close originals, or silently duplicate work.

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
