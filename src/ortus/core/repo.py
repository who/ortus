"""Repo discovery + validation (FR-003).

Minimal stub for q075.2. Expanded by q075.3 with config layering, prereq
checks, etc.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

FR003_NO_BEADS_ERROR = (
    "no .beads/ in current directory; "
    "cd to your project root or pass <repo> explicitly "
    "(e.g., ortus grind ~/code/myproj)"
)


def resolve_repo(repo: Path | None) -> Path:
    """Resolve <repo> per FR-003.

    If `repo` is None, default to $PWD. Either way, the resulting path is
    used as-is — no walk-up. If the resulting path does not contain a
    `.beads/` directory, exit 1 with the PRD-mandated error string verbatim
    (single line, no rich wrapping — downstream tooling may grep for it).
    """
    target = (repo if repo is not None else Path.cwd()).resolve()
    if not (target / ".beads").is_dir():
        print(f"error: {FR003_NO_BEADS_ERROR}", file=sys.stderr)
        raise typer.Exit(code=1)
    return target
