"""ortus plan <repo> [<PRD>] — decompose a PRD (or freeform idea) into bd issues.

Full implementation lands in xvel.5 (Phase 2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ortus.commands._stub import not_implemented
from ortus.core.repo import resolve_repo


def plan(
    repo: Optional[Path] = typer.Argument(
        None, help="Target repo directory. Defaults to $PWD; no walk-up."
    ),
    prd: Optional[Path] = typer.Argument(
        None, help="Optional PRD path. If omitted, runs the idea-interview flow."
    ),
) -> None:
    """Decompose a PRD into bd issues, or interview-then-PRD-then-decompose."""
    resolve_repo(repo)
    not_implemented("plan", "xvel.5")
