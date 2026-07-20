#!/usr/bin/env python3
"""Fake claude shim for tests/test_plan.py.

When invoked, materializes 3 bd issues in the current working directory
(which ClaudeRunner sets to the test repo). Real claude would do the
decomposition itself; the test cares only that plan reaches the runner
and that issues land in the right .beads/.

Rewritten in Python for Windows compat. See ortus-f4bu.
"""

from __future__ import annotations

import subprocess


def _bd_create(title: str, priority: str, objective: str, criterion: str) -> None:
    description = f"""## Objective
{objective}

## Behavioral context
The fixture behavior changes observably while unrelated behavior stays stable."""
    design = """## Readiness schema
v1

## Scope
Implement only the behavior named by this fixture leaf.

## Non-goals
No unrelated refactor or API redesign.

## Concrete locations
Edit `src/example.py` in `run()` and cover `tests/test_example.py::test_run`.

## Resolved decisions
Reuse the existing command path and preserve its interface.

## Compatibility constraints
Keep existing CLI output and exit codes unchanged.

## Ordered steps
1. Update `run()`.
2. Add the focused assertion.

## Dependencies
None — this fixture leaf is standalone; caller is `cli.run()`.

## Edge cases
Empty input remains supported and failures stay nonzero.

## Plan-gap guidance
If `run()` has no stable interface, record PLAN-GAP with the conflicting symbol."""
    acceptance = f"""## Observable criteria
- AC-1: {criterion}

## Criterion checks
- AC-1: Run `uv run pytest tests/test_example.py -q`.

## Targeted tests
Run `uv run pytest tests/test_example.py -q`."""
    subprocess.run(
        [
            "bd",
            "create",
            "--silent",
            "--title",
            title,
            "--type",
            "task",
            "--priority",
            priority,
            "--description",
            description,
            "--design",
            design,
            "--acceptance",
            acceptance,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def main() -> int:
    _bd_create(
        "Add CLI flag --dry-run",
        "2",
        "Print what would happen without doing it.",
        "Test: --dry-run produces no side effects.",
    )
    _bd_create(
        "Write integration test for --dry-run",
        "2",
        "Assert no side effects when --dry-run is passed.",
        "Test passes.",
    )
    _bd_create(
        "Document --dry-run in README",
        "3",
        "Add a Flags section with examples.",
        "README contains --dry-run example.",
    )
    print("fake-claude-plan: created 3 issues", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
