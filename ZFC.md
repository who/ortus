# Zero-Framework Cognition (ZFC) Rubric

Use this rubric to evaluate any prompt or orchestration script in Ortus. It is the shared definition referenced by [`prd/PRD-zero-framework-cognition.md`](prd/PRD-zero-framework-cognition.md), which adapts Steve Yegge's ["Zero Framework Cognition: A Way to Build Resilient AI Applications"](https://steve-yegge.medium.com/zero-framework-cognition-a-way-to-build-resilient-ai-applications-56b090ed3e69) to this codebase.

## Definition

Zero-Framework Cognition means **reasoning lives in the model, not in the orchestrator**. Shell scripts, prompt files, and pipelines are dumb pipes: they move data, validate shapes, enforce budgets, and track lifecycle state. Decisions — "what's next?", "is this ambiguous?", "how deep should I think?", "is this plan good enough?" — are delegated to explicit model calls that return structured, schema-validated output. Client code that tries to infer meaning from unstructured input (keyword matching, heuristic ranking, hard-coded decision trees on issue type or label) is brittle: the moment the input shape shifts, the framework breaks. Delegate the judgment; keep the plumbing mechanical.

## Rubric

| Allowed (dumb pipes) | Forbidden (local intelligence) |
|---|---|
| Pure IO: reading files, calling APIs, writing state | Keyword routing: branching on words in free-text input |
| Schema-validated output (e.g. `<promise>COMPLETE</promise>`, JSON with required fields) | Heuristic ranking: scoring or sorting by model-derived intuition in client code |
| Policy budgets: "iterate up to 5 times", "max 200K context" | Decision trees on unstructured input: `if issue.type == "bug" then X else Y` |
| Mechanical templating: copier, jinja, string interpolation | Local semantic analysis: the script deciding what a description "means" |
| State/lifecycle via labels or enums with a documented vocabulary | Prompt text embedded inline in shell scripts (drift risk, no single source of truth) |
| Delegating judgment to a model call whose output is parsed by schema | Inferring reasoning depth from keywords ("ultrathink if the word 'architecture' appears") |

## Worked example

**Violation** (in an earlier Ralph prompt):

> ### Issue Type Rules
> **task** — Implement exactly what's specified. NO scope expansion.
> **bug** — Reproduce, diagnose, fix. Minimal, focused fix.
> **epic/feature** — Milestone check. If all children are closed, close the parent.

This is a client-side decision tree. The *orchestrator prompt* is classifying work and prescribing behavior per type. It will break the moment an issue doesn't fit one of the three shapes, or the moment a bug genuinely needs scope expansion.

**Fix** — delegate the judgment to a model call with a schema-validated response:

> Read the issue (title, description, type, labels, acceptance criteria). Ask the model to produce a plan appropriate to this specific issue, with shape `{steps: [...], verification: [...], closure_reason: "..."}`. Validate the shape. Execute the steps mechanically. Run the verification. If acceptance criteria are not satisfied, iterate.

The scheduler no longer encodes "what a bug deserves." The model decides, the scheduler executes. The subagent allocation table (Opus for reasoning, Sonnet for writes) stays — that's policy, not heuristic.

### Worked example: sentinel-grep → `/goal` (Phase 5 migration)

The ralph.sh orchestrator used to decide when a subprocess had finished by string-matching the model's output: `if [[ "$result" == *"<promise>COMPLETE</promise>"* ]]; then …`, with sibling cases for `<promise>EMPTY</promise>` and `<promise>BLOCKED</promise>`. That is a heuristic-shape failure even though it looks structured — the orchestrator is inferring **meaning** ("the task is done") from **unstructured text the model emitted**, and any drift in how the model phrases its closure (omitting the sentinel, emitting it inside fenced code, emitting it twice, emitting a near-miss like `<promise>complete</promise>`) silently changes loop behavior. The `<promise>X</promise>` tag is a brittle contract between the prompt's instructions and the shell's regex; it has no schema and no validator.

The fix, shipped in Phase 2–5 of [`prd/PRD-goal-directive.md`](prd/PRD-goal-directive.md), replaces the sentinel with a `/goal CONDITION` directive evaluated by a dedicated Claude Haiku judge each turn. The orchestrator stops parsing model output entirely: it spawns one long-lived `claude -p "/goal CONDITION"` session, the `/goal` evaluator reads the running transcript and answers "is the condition satisfied yet?" turn by turn, and the Stop hook blocks termination until the evaluator says yes. ralph.sh has been reduced to a one-line deprecation shim that `exec`s `goal.sh`. Decision moved from a shell-level grep into an explicit model call with a schema-validated yes/no — the textbook ZFC move.



## How to use this rubric

When writing or reviewing a prompt or script, for each decision point ask:

1. Is this deciding something based on the *shape* of the input (type, label, enum value, schema field)? → Allowed.
2. Is this deciding something based on the *meaning* of unstructured text? → Forbidden. Delegate to a model call.
3. Is this enforcing a budget, validating a schema, or tracking lifecycle state? → Allowed.
4. Would a small shift in input wording silently change the behavior? → Forbidden. The decision is a heuristic in disguise.

When in doubt, prefer "ask the model, validate the shape, execute mechanically" over any client-side branch.

## Further reading

- [`prd/PRD-zero-framework-cognition.md`](prd/PRD-zero-framework-cognition.md) — full audit, requirements, and phased rollout plan for Ortus.
- Yegge, ["Zero Framework Cognition"](https://steve-yegge.medium.com/zero-framework-cognition-a-way-to-build-resilient-ai-applications-56b090ed3e69) — the source post.
- Fowler, ["Smart Endpoints and Dumb Pipes"](https://martinfowler.com/articles/microservices.html#SmartEndpointsAndDumbPipes) — the architectural pattern Yegge adapts.
