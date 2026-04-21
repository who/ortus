# Label State Machine

This doc defines the label vocabulary used by the interview/PRD workflow. The labels form an **explicit state schema**: a small, closed set of string values that scripts may read to track where a feature is in its lifecycle. They are **not** a semantic-routing trigger. Scripts may filter on these labels (e.g. `interview.sh`'s `find_pending_features()` skips features that already carry any of them), but no script may infer judgment from a label's *meaning* — that would collapse this schema into local intelligence, which [`ZFC.md`](ZFC.md) forbids. The distinction is audited in [`prd/PRD-zero-framework-cognition.md`](../../prd/PRD-zero-framework-cognition.md) under the `ortus/interview.sh` row (line 60): state-tracking via labels is *allowed*; treating labels as a basis for semantic decisions is *forbidden*.

## Labels

| Label | Meaning | Entry criteria | Exit criteria | Valid next labels |
|---|---|---|---|---|
| *(none)* | Feature has been filed but not yet picked up for interview. | Feature is created (e.g. by `idea.sh` or hand-filed with `bd create --type=feature`) and assigned to the interview owner (ralph). | `interview.sh` selects the feature and the model enters Phase 1 of `interview-prompt.md.jinja`. | `prd:interviewing` |
| `prd:interviewing` | A live PRD interview is in progress. Multi-step flows may use this label to mark the feature as claimed so concurrent runs don't re-select it. | Set at the start of the interview when the flow is long-running enough to need a reservation. | Interview finishes and the feature advances, or is released back (label removed) if the interview is abandoned. | `interviewed` or *(none)* on release |
| `interviewed` | Phase 1 interview complete; summary saved as a bd comment. The feature is ready for PRD generation but not yet converted into implementation tasks. | Added by the model at the end of interview Phase 1 (see `interview-prompt.md.jinja` Step 4, which runs `bd label add {{FEATURE_ID}} interviewed`). | The PRD document is drafted and accepted by the user in the review loop. | `prd:ready` |
| `prd:ready` | PRD has been generated and is awaiting user approval. Used when the PRD-generation step is decoupled from the approval step. | Set when the PRD draft is written to `prd/PRD-<name>.md` and presented for review. | User approves the PRD. | `approved` |
| `approved` | PRD approved; implementation tasks may now be created. Terminal state for this workflow — once approved, the feature moves out of the interview queue and into normal Ralph scheduling on the child tasks. | Added by the model after user approval (see `interview-prompt.md.jinja` Step 6, which runs `bd label add {{FEATURE_ID}} approved`). | None — this is terminal. The feature is closed when its implementation children are all closed. | *(terminal)* |

## State transitions

```
(none) → prd:interviewing → interviewed → prd:ready → approved
                │              │              │
                └──── (released on abandon) ──┘
```

`prd:interviewing` and `prd:ready` are intermediate states for flows that separate claim/generation/approval into distinct steps. A single-pass interview may go directly `(none) → interviewed → approved` without using them; the filter in `interview.sh` treats any of the four as "already in flight, skip."

## Rules

1. **Labels are a schema, not a trigger.** Scripts may `bd list --label=interviewed` or filter on label presence/absence to track lifecycle. Scripts must not branch on a label's *meaning* (e.g. "if the label starts with `prd:` then handle specially"). Schema checks (exact string match against a known set) are allowed; substring or pattern inference is not.
2. **No new label values without updating this doc.** If a workflow needs a new state (e.g. `prd:draft`, `archived`), add a row to the table above first, defining its meaning, entry/exit criteria, and valid next labels. Reviewers should reject code that introduces an undocumented label.
3. **State changes are model-driven, not script-driven.** The model running the interview prompt adds labels via `bd label add` at well-defined points. Shell scripts read labels but do not set them based on unstructured judgment.
4. **Labels are cumulative, not mutually exclusive.** Advancing from `interviewed` to `approved` typically keeps `interviewed` set — the filter in `interview.sh` excludes features with *any* of the lifecycle labels, so leaving earlier labels in place is harmless and preserves history.

## Worked example: is `prd:draft` a needed label?

Suppose a contributor wants to add `prd:draft` for PRDs that have been sketched but not yet reviewed.

- **Is there a real state gap?** Today, between `interviewed` and `prd:ready` the feature is "PRD exists but not yet presented for approval." If the interview flow produces the PRD and presents it in one step, no gap exists and `prd:draft` is redundant with `prd:ready`. If PRD drafting and presentation are decoupled — for instance, a batch job generates drafts overnight and a human reviews them later — then `prd:draft` fills a real slot between `interviewed` and `prd:ready`.
- **What transitions would it imply?** `interviewed → prd:draft → prd:ready → approved`. The filter in `interview.sh` would need to include `prd:draft` in its skip list.
- **If yes, add a row above** with meaning, entry/exit, and valid next labels. If no, use an existing label instead.

The same test applies to any new label: is there a lifecycle state that no existing label captures, and can the transitions be written down unambiguously? If yes, document it here first; if no, reuse what exists.
