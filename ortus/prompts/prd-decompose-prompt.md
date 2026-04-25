Read $prd_path . Decompose the provided PRD Markdown into a Beads issue graph using bd. For each work item, create an issue with: title, description (scope/context), acceptance_criteria (REQUIRED: must include testable conditions that define 'done' AND specific testing instructions for verification), design notes (technical approach), priority (0-4, 0=critical), type (epic/feature/task/bug/chore), labels, and estimated_minutes. CRITICAL: Every task MUST have acceptance_criteria with: (1) testable conditions - what must be true when done, (2) testing instructions - how to verify each condition. Structure hierarchically: epics for major features, decomposed into tasks via parent-child dependencies; use blocks for execution order constraints and related for shared context. Output all bd create and bd dep add commands to construct the complete graph with proper dependencies reflecting the PRD's requirements and sequence. Where atomically possible, run the bd tasks in parallel with 10 sub-agents.  When done, tell the user to type /exit to continue.

---

## CodeGraph reference validation

When `codegraph_available` (the `.codegraph/` directory exists at the project root *and* at least one `mcp__codegraph__*` tool is registered), validate code-shaped references in each proposed work item before issuing `bd create`. When CodeGraph isn't available, skip this block silently — never error.

**Reference extraction (FR-301).** Scan each work item's title, description, and acceptance_criteria for:

- CamelCase identifiers — `[A-Z][A-Za-z0-9_]*` (e.g., `AuthMiddleware`).
- dotted methods — `[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*` (e.g., `auth.refreshToken`).
- file paths containing `/` and a recognized source extension — `.ts`, `.tsx`, `.js`, `.py`, `.rs`, `.go`, `.java`, `.rb`.

**Per-reference validation (FR-302).** For each extracted reference, call `codegraph_search <ref>` and partition the results:

- *Resolved* — the search returns at least one graph-known symbol; record `<symbol>@<file>` per match.
- *Unresolved* — the search returns nothing; the reference is graph-absent.

Use only `codegraph_search` for this step (cheap, main-context-safe). Do not call `codegraph_explore` or `codegraph_context` from the main session. Downstream rendering of these resolved/unresolved sets is covered by FR-303 (annotation comment) and FR-304 (Likely files shortlist).

**Annotation comment (FR-303).** When at least one extracted reference falls into either bucket, after `bd create` of the work item's issue, attach the resolved/unresolved sets as a `bd comments add <new-id> "..."` matching this Appendix F template verbatim:

```
**CodeGraph references** (decompose-time check):
- Unresolved: <ref-1>, <ref-2>, ...    # if any
- Resolved:   <sym-1>@<file>, <sym-2>@<file>, ...  # if any
```

This annotation is **advisory only**: it never blocks issue creation, never alters the issue body / description / acceptance_criteria, and never changes `--priority` or `--type` from what the decomposer would otherwise set. It is a hint for the eventual Investigate step (Ralph step 4) to confirm or correct, not a gating signal. If `codegraph_search` errors or the graph is partial, omit the comment silently — same posture as the rest of this block.
