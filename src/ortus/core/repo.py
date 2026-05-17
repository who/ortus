"""Repo discovery + validation (FR-003).

Minimal stub for q075.2. Expanded by q075.3 with config layering, prereq
checks, etc.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

# Stable substring downstream tooling (and tests) can grep for. Kept short
# and unique; the full multi-line error wraps this prefix with the actual
# path that failed the lookup and how it was resolved.
FR003_NO_BEADS_ERROR = "no .beads/ workspace at"


def resolve_repo(repo: Path | None) -> Path:
    """Resolve <repo> per FR-003.

    If `repo` is None, default to $PWD. Either way, the resulting path is
    used as-is — no walk-up. If the resulting path does not contain a
    `.beads/` directory, exit 1 with an error naming the actual path that
    was checked plus the arg it was resolved from.
    """
    target = (repo if repo is not None else Path.cwd()).resolve()
    if not (target / ".beads").is_dir():
        origin = "(defaulted to PWD)" if repo is None else f"(resolved from: {repo})"
        msg = (
            f"{FR003_NO_BEADS_ERROR} {target}\n"
            f"       {origin}\n"
            f"       cd to your project root, or pass <repo> explicitly: "
            f"ortus <verb> <repo> [<args>...]"
        )
        print(f"error: {msg}", file=sys.stderr)
        raise typer.Exit(code=1)
    return target
