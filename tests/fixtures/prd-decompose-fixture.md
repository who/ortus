# PRD: PRD-decompose CodeGraph reference validation fixture

This fixture exercises the FR-301..304 CodeGraph reference validation block
in `ortus/prompts/prd-decompose-prompt.md`. Test substrate for T5.6 (FR-303
phantom annotation) and T5.7 (FR-304 Likely files shortlist).

## Goal

Decompose this PRD into bd issues so the CodeGraph reference validator can
exercise both branches of FR-302 partition: one *resolved* match and one
*unresolved* phantom.

## Work item 1 — phantom symbol (unresolved)

**Title**: Implement AuthMiddleware.refreshToken

**Description**: Refresh the access token via `AuthMiddleware.refreshToken`
when the upstream cache returns a 401. The new flow rotates the token in
place without re-issuing the session cookie.

**Acceptance criteria**: When `AuthMiddleware.refreshToken` succeeds, the
session continues without a re-login round-trip.

> The `AuthMiddleware.refreshToken` reference is intentionally a phantom —
> it does not exist in this codebase's CodeGraph. FR-302 partitions it into
> the *unresolved* set; FR-303 surfaces it as a `**CodeGraph references**`
> annotation comment on the resulting bd issue.

## Work item 2 — real symbol (resolves)

**Title**: Update GitConfigContext default fallbacks

**Description**: The `GitConfigContext` class in `extensions/context.py`
returns `Developer` and `dev@example.com` when git config lookups fail.
Tighten the fallback to surface a clearer placeholder so downstream Copier
prompts don't ship those defaults silently into generated projects.

**Acceptance criteria**: `GitConfigContext.hook` returns the new placeholder
strings; `extensions/context.py` reflects the change.

> The `GitConfigContext` CamelCase identifier and the `extensions/context.py`
> source path resolve in this project's CodeGraph. FR-302 partitions both
> into the *resolved* set; FR-304 emits a `**Likely files**: extensions/context.py`
> shortlist into the resulting bd issue's description.
