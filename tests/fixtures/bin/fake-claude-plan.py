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
import sys


def _bd_create(title: str, priority: str, description: str, acceptance: str) -> None:
    subprocess.run(
        [
            "bd", "create", "--silent",
            "--title", title,
            "--type", "task",
            "--priority", priority,
            "--description", description,
            "--acceptance", acceptance,
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
