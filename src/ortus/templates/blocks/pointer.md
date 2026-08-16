## Ortus session rules

Managed by Ortus {CLI_VERSION}. Edit outside the markers freely — `ortus init`
rewrites only what sits between them, and `ortus check` reports drift.

`AGENTS.md` in this repo is the session contract: read it first, and follow its
issue-authoring, orchestrator, and session-close sections rather than restating
them here.

The short version: claim with `{BD_CLAIM_COMMAND}`, do exactly that one issue,
close it with `bd close <id> --reason "..."`, and push before calling the
session done.

{CODEGRAPH_SECTION}
